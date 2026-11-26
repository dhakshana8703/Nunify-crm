"""SQL persistence for coverages and durable AE handoffs."""

import json

from workflow_store import audit, is_blocked, timestamp, transaction


SCHEMA = """
CREATE TABLE IF NOT EXISTS coverages (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    owner_id INTEGER NOT NULL REFERENCES users(id),
    rep_id INTEGER NOT NULL REFERENCES users(id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    cancelled_at TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id),
    CHECK (owner_id != rep_id),
    CHECK (starts_at < ends_at)
);
CREATE INDEX IF NOT EXISTS coverages_scope ON coverages(account_id, rep_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    person_id INTEGER NOT NULL REFERENCES people(id),
    ae_id INTEGER NOT NULL REFERENCES users(id),
    requested_by INTEGER NOT NULL REFERENCES users(id),
    outcome TEXT NOT NULL CHECK (outcome IN ('demo_requested', 'pricing_requested', 'meeting_booked')),
    context TEXT NOT NULL,
    next_step TEXT NOT NULL,
    meeting_at TEXT,
    payload TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivering', 'delivered', 'failed')),
    lease_token TEXT,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    receipt TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE (account_id, person_id)
);
CREATE INDEX IF NOT EXISTS handoffs_work ON handoffs(status, lease_until, id);
CREATE INDEX IF NOT EXISTS handoffs_ae ON handoffs(ae_id, status, id);

CREATE TABLE IF NOT EXISTS offline_handoff_receipts (
    idempotency_key TEXT PRIMARY KEY REFERENCES handoffs(idempotency_key),
    receipt TEXT NOT NULL UNIQUE,
    accepted_at TEXT NOT NULL
);
"""


def initialize(connection):
    connection.executescript(SCHEMA)


class SqlHandoffStore:
    def __init__(self, connection):
        self.connection = connection

    def atomic(self):
        return transaction(self.connection)

    def active_user(self, user_id, role):
        return self.connection.execute(
            "SELECT 1 FROM users WHERE id = ? AND role = ? AND active = 1", (user_id, role),
        ).fetchone() is not None

    def users(self, role):
        return self.connection.execute("""
            SELECT id, name FROM users WHERE role = ? AND active = 1 ORDER BY name, id
        """, (role,)).fetchall()

    def coverage_overlaps(self, account_id, owner_id, starts_at, ends_at):
        return self.connection.execute("""
            SELECT id FROM coverages
            WHERE account_id = ? AND owner_id = ? AND cancelled_at IS NULL
              AND starts_at < ? AND ends_at > ?
        """, (account_id, owner_id, ends_at, starts_at)).fetchone() is not None

    def add_coverage(self, *, account_id, owner_id, rep_id, starts_at, ends_at, actor_id):
        return self.connection.execute("""
            INSERT INTO coverages (account_id, owner_id, rep_id, starts_at, ends_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, owner_id, rep_id, starts_at, ends_at, actor_id)).lastrowid

    def end_coverage(self, coverage_id, account_id, owner_id, observed):
        at = timestamp(observed)
        return self.connection.execute("""
            UPDATE coverages SET cancelled_at = ?
            WHERE id = ? AND account_id = ? AND owner_id = ? AND cancelled_at IS NULL AND ends_at > ?
        """, (at, coverage_id, account_id, owner_id, at)).rowcount == 1

    def existing_handoff(self, account_id, person_id):
        return self.connection.execute("""
            SELECT id, ae_id, outcome, context, next_step, meeting_at
            FROM handoffs WHERE account_id = ? AND person_id = ?
        """, (account_id, person_id)).fetchone()

    def contact(self, account_id, person_id):
        return self.connection.execute("""
            SELECT id, name, title, email, persona FROM people WHERE id = ? AND account_id = ?
        """, (person_id, account_id)).fetchone()

    def conversation(self, account_id, person_id):
        return self.connection.execute("""
            SELECT a.channel, a.outcome, a.direction, a.note, a.occurred_at, u.name AS actor
            FROM activities a JOIN users u ON u.id = a.actor_id
            WHERE a.account_id = ? AND (a.person_id = ? OR a.person_id IS NULL)
            ORDER BY a.occurred_at DESC, a.id DESC LIMIT 10
        """, (account_id, person_id)).fetchall()

    def add_handoff(self, account_id, actor_id, request, payload, key, observed):
        return self.connection.execute("""
            INSERT INTO handoffs (account_id, person_id, ae_id, requested_by, outcome, context,
                                  next_step, meeting_at, payload, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (account_id, request.person_id, request.ae_id, actor_id, request.outcome, request.context,
              request.next_step, request.meeting_at, json.dumps(payload, sort_keys=True), key, timestamp(observed))).lastrowid

    def mark_positive(self, account_id, person_id, stage):
        self.connection.execute("UPDATE people SET status = 'positive' WHERE id = ?", (person_id,))
        if stage is not None:
            self.connection.execute("""
                INSERT INTO account_work (account_id, stage) VALUES (?, ?)
                ON CONFLICT (account_id) DO UPDATE SET stage = excluded.stage
            """, (account_id, stage))

    def handoff_status(self, handoff_id):
        return self.connection.execute("SELECT account_id, status FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()

    def requeue_handoff(self, handoff_id):
        self.connection.execute("UPDATE handoffs SET status = 'pending', last_error = NULL WHERE id = ?", (handoff_id,))

    def pending_handoff(self, at):
        return self.connection.execute("""
            SELECT * FROM handoffs WHERE status = 'pending'
              OR (status = 'delivering' AND lease_until <= ?) ORDER BY id LIMIT 1
        """, (timestamp(at),)).fetchone()

    def blocked(self, account_id, person_id=None):
        return is_blocked(self.connection, account_id, person_id)

    def fail_before_delivery(self, handoff_id, reason):
        self.connection.execute("""
            UPDATE handoffs SET status = 'failed', last_error = ?, lease_token = NULL, lease_until = NULL
            WHERE id = ?
        """, (reason, handoff_id))

    def claim_handoff(self, handoff_id, lease_id, lease_until):
        self.connection.execute("""
            UPDATE handoffs SET status = 'delivering', attempts = attempts + 1, lease_token = ?, lease_until = ?
            WHERE id = ?
        """, (lease_id, timestamp(lease_until), handoff_id))

    def complete_delivery(self, handoff_id, lease_id, status, receipt, error, at):
        return self.connection.execute("""
            UPDATE handoffs SET status = ?, receipt = ?, last_error = ?, delivered_at = ?,
                                lease_token = NULL, lease_until = NULL
            WHERE id = ? AND lease_token = ?
        """, (status, receipt, error, timestamp(at) if at is not None else None, handoff_id, lease_id)).rowcount == 1

    def transition_delivery(self, account_id, person_id, previous, target):
        self.connection.execute("UPDATE people SET status = ? WHERE id = ? AND status = ?",
                                (target, person_id, previous))
        self.connection.execute("""
            UPDATE account_work SET stage = ? WHERE account_id = ? AND stage = ?
        """, (target, account_id, previous))

    def audit(self, actor_id, account_id, action, details, observed):
        audit(self.connection, actor_id, account_id, action, details, observed)

    def account_coverages(self, account_id, at):
        return self.connection.execute("""
            SELECT c.*, cover.name AS rep_name, owner.name AS owner_name
            FROM coverages c JOIN account_work work ON work.account_id = c.account_id
            JOIN users cover ON cover.id = c.rep_id JOIN users owner ON owner.id = c.owner_id
            WHERE c.account_id = ? AND c.cancelled_at IS NULL AND c.ends_at > ?
              AND work.owner_id = c.owner_id
            ORDER BY c.starts_at
        """, (account_id, at)).fetchall()

    def account_handoffs(self, account_id):
        return self.connection.execute("""
            SELECT h.id, h.status, h.outcome, p.name AS person_name, ae.name AS ae_name
            FROM handoffs h JOIN people p ON p.id = h.person_id JOIN users ae ON ae.id = h.ae_id
            WHERE h.account_id = ? ORDER BY h.id DESC
        """, (account_id,)).fetchall()

    def account_contacts(self, account_id):
        return self.connection.execute("""
            SELECT id, name, persona FROM people WHERE account_id = ? AND do_not_contact = 0 ORDER BY name, id
        """, (account_id,)).fetchall()

    def list_handoffs(self, role, user_id, observed, filters, page):
        clauses = []
        params = []
        if role == "ae":
            clauses.append("h.ae_id = ? AND h.status = 'delivered'")
            params.append(user_id)
        elif role == "sdr":
            clauses.append("""(work.owner_id = ? OR EXISTS (
                SELECT 1 FROM coverages c WHERE c.account_id = h.account_id AND c.owner_id = work.owner_id
                  AND c.rep_id = ? AND c.cancelled_at IS NULL AND c.starts_at <= ? AND c.ends_at > ?
            ))""")
            at = timestamp(observed)
            params.extend((user_id, user_id, at, at))
        if filters["search"]:
            clauses.append("(a.name LIKE ? OR p.name LIKE ? OR ae.name LIKE ?)")
            params.extend([f"%{filters['search']}%"] * 3)
        if filters["status"] in {"pending", "delivered", "failed"}:
            clauses.append("h.status IN ('pending', 'delivering')" if filters["status"] == "pending" else "h.status = ?")
            if filters["status"] != "pending":
                params.append(filters["status"])
        source = """FROM handoffs h JOIN accounts a ON a.id = h.account_id
                    JOIN people p ON p.id = h.person_id JOIN users ae ON ae.id = h.ae_id
                    JOIN account_work work ON work.account_id = h.account_id"""
        if clauses:
            source += " WHERE " + " AND ".join(clauses)
        total = self.connection.execute("SELECT COUNT(*) " + source, params).fetchone()[0]
        size = filters["size"]
        pages = max(1, (total + size - 1) // size)
        page = min(pages, max(1, page))
        direction = "ASC" if filters["sort"] == "oldest" else "DESC"
        records = self.connection.execute(
            "SELECT h.*, a.name AS account_name, p.name AS person_name, ae.name AS ae_name "
            + source + f" ORDER BY h.id {direction} LIMIT ? OFFSET ?",
            (*params, size, (page - 1) * size),
        ).fetchall()
        return records, page, pages

    def handoff_detail(self, handoff_id):
        return self.connection.execute("""
            SELECT h.*, a.name AS account_name, ae.name AS ae_name, actor.name AS requester_name
            FROM handoffs h JOIN accounts a ON a.id = h.account_id
            JOIN users ae ON ae.id = h.ae_id JOIN users actor ON actor.id = h.requested_by
            WHERE h.id = ?
        """, (handoff_id,)).fetchone()


class OfflineAEProvider:
    """Idempotent demo delivery backed by a local inbox receipt."""

    def __init__(self, connection):
        self.connection = connection

    def deliver(self, key, _payload):
        with transaction(self.connection):
            self.connection.execute("""
                INSERT INTO offline_handoff_receipts (idempotency_key, receipt, accepted_at)
                VALUES (?, ?, ?) ON CONFLICT (idempotency_key) DO NOTHING
            """, (key, f"demo-{key}", timestamp()))
            return self.connection.execute("""
                SELECT receipt FROM offline_handoff_receipts WHERE idempotency_key = ?
            """, (key,)).fetchone()["receipt"]
