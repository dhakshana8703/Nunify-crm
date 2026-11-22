"""Temporary rep coverage and durable AE handoffs."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from workflow_store import audit, is_blocked, timestamp, transaction, utcnow
from workflow_web import account_required, db, now as request_now, staff_required


bp = Blueprint("sales_extensions", __name__)
MANAGEMENT = {"admin", "manager"}
POSITIVE_OUTCOMES = {
    "demo_requested": "Demo requested",
    "pricing_requested": "Pricing requested",
    "meeting_booked": "Meeting booked",
}

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


def _users(connection, role):
    return connection.execute("""
        SELECT id, name FROM users WHERE role = ? AND active = 1 ORDER BY name, id
    """, (role,)).fetchall()


def _display_local(value, viewer):
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(ZoneInfo(viewer["timezone"])).strftime("%b %d, %Y %H:%M %Z")


def _parse_local_form(field, viewer, *, required=True):
    value = request.form.get(field, "")
    if not value and not required:
        return None
    try:
        local = datetime.fromisoformat(value)
        if local.tzinfo is not None or "T" not in value:
            raise ValueError
        zone = ZoneInfo(viewer["timezone"])
        earlier = local.replace(tzinfo=zone, fold=0)
        later = local.replace(tzinfo=zone, fold=1)
        if earlier.utcoffset() != later.utcoffset():
            abort(400, description="This local time changes with daylight saving. Select another time.")
        return timestamp(earlier.astimezone(timezone.utc))
    except ValueError:
        abort(400, description="Enter a valid local date and time.")


def _manage_user():
    user = staff_required()
    if user["role"] not in MANAGEMENT:
        abort(403)
    return user


def _handoff_user():
    user = g.get("crm_user")
    if user is None:
        abort(401)
    return user


def _revision_matches(account):
    if request.form.get("revision", type=int) != account["revision"]:
        abort(409, description="This account changed while the form was open. Reload and try again.")


def _can_access_handoff(handoff, user):
    if user["role"] == "ae":
        if handoff["ae_id"] != user["id"] or handoff["status"] != "delivered":
            abort(403)
    else:
        account_required(handoff["account_id"])


def account_context(connection, account_id, user):
    at = timestamp(request_now())
    coverages = connection.execute("""
        SELECT c.*, cover.name AS rep_name, owner.name AS owner_name
        FROM coverages c JOIN account_work work ON work.account_id = c.account_id
        JOIN users cover ON cover.id = c.rep_id JOIN users owner ON owner.id = c.owner_id
        WHERE c.account_id = ? AND c.cancelled_at IS NULL AND c.ends_at > ?
          AND work.owner_id = c.owner_id
        ORDER BY c.starts_at
    """, (account_id, at)).fetchall()
    handoffs = connection.execute("""
        SELECT h.id, h.status, h.outcome, p.name AS person_name, ae.name AS ae_name
        FROM handoffs h JOIN people p ON p.id = h.person_id JOIN users ae ON ae.id = h.ae_id
        WHERE h.account_id = ? ORDER BY h.id DESC
    """, (account_id,)).fetchall()
    blocked = is_blocked(connection, account_id)
    contacts = connection.execute("""
        SELECT id, name, persona FROM people WHERE account_id = ? AND do_not_contact = 0 ORDER BY name, id
    """, (account_id,)).fetchall()
    return {
        "manage": user["role"] in MANAGEMENT,
        "rep_options": _users(connection, "sdr"),
        "ae_options": _users(connection, "ae"),
        "contacts": [contact for contact in contacts if not blocked and not is_blocked(connection, account_id, contact["id"])],
        "coverages": [dict(row, active=row["starts_at"] <= at, starts_local=_display_local(row["starts_at"], user),
                           ends_local=_display_local(row["ends_at"], user)) for row in coverages],
        "handoffs": handoffs,
        "positive_outcomes": POSITIVE_OUTCOMES,
    }


@bp.post("/accounts/<int:account_id>/coverages")
def create_coverage(account_id):
    user = _manage_user()
    connection = db()
    at = request_now()
    starts_at = _parse_local_form("starts_at", user)
    ends_at = _parse_local_form("ends_at", user)
    if ends_at <= starts_at or ends_at <= timestamp(at):
        abort(400, description="Coverage must end after its start and be scheduled into the future.")
    rep_id = request.form.get("rep_id", type=int)
    with transaction(connection):
        account = account_required(account_id, write=True)
        _revision_matches(account)
        owner_id = account["owner_id"]
        if owner_id is None or owner_id == rep_id:
            abort(400, description="Choose an assigned account and a different rep to cover it.")
        if not any(rep["id"] == rep_id for rep in _users(connection, "sdr")):
            abort(400, description="Choose an active SDR for coverage.")
        overlap = connection.execute("""
            SELECT id FROM coverages
            WHERE account_id = ? AND owner_id = ? AND cancelled_at IS NULL
              AND starts_at < ? AND ends_at > ?
        """, (account_id, owner_id, ends_at, starts_at)).fetchone()
        if overlap is not None:
            abort(409, description="This account already has coverage during that window.")
        coverage = connection.execute("""
            INSERT INTO coverages (account_id, owner_id, rep_id, starts_at, ends_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, owner_id, rep_id, starts_at, ends_at, user["id"]))
        audit(connection, user["id"], account_id, "coverage_created", {
            "coverage_id": coverage.lastrowid, "owner_id": owner_id, "rep_id": rep_id,
            "starts_at": starts_at, "ends_at": ends_at,
        }, now=at)
    flash("Coverage scheduled.")
    return redirect(url_for("account_detail", account_id=account_id), code=303)


@bp.post("/accounts/<int:account_id>/coverages/<int:coverage_id>/cancel")
def cancel_coverage(account_id, coverage_id):
    user = _manage_user()
    connection = db()
    at = request_now()
    with transaction(connection):
        account = account_required(account_id, write=True)
        _revision_matches(account)
        now = timestamp(at)
        updated = connection.execute("""
            UPDATE coverages SET cancelled_at = ?
            WHERE id = ? AND account_id = ? AND owner_id = ? AND cancelled_at IS NULL AND ends_at > ?
        """, (now, coverage_id, account_id, account["owner_id"], now))
        if updated.rowcount != 1:
            abort(409, description="Coverage is no longer active or scheduled for this account.")
        audit(connection, user["id"], account_id, "coverage_cancelled", {"coverage_id": coverage_id}, now=at)
    flash("Coverage ended.")
    return redirect(url_for("account_detail", account_id=account_id), code=303)


@bp.post("/accounts/<int:account_id>/handoffs")
def create_handoff(account_id):
    user = _handoff_user()
    if user["role"] not in MANAGEMENT | {"sdr"}:
        abort(403)
    connection = db()
    at = request_now()
    person_id = request.form.get("person_id", type=int)
    ae_id = request.form.get("ae_id", type=int)
    outcome = request.form.get("outcome", "")
    context = request.form.get("context", "").strip()
    next_step = request.form.get("next_step", "").strip()
    if outcome not in POSITIVE_OUTCOMES or not (1 <= len(context) <= 4000 and 1 <= len(next_step) <= 2000):
        abort(400, description="Enter a qualifying outcome, conversation context, and the agreed next step.")
    meeting_at = _parse_local_form("meeting_at", user, required=outcome == "meeting_booked")
    with transaction(connection):
        account = account_required(account_id, write=True)
        existing = connection.execute("""
            SELECT id, ae_id, outcome, context, next_step, meeting_at
            FROM handoffs WHERE account_id = ? AND person_id = ?
        """, (account_id, person_id)).fetchone()
        if existing is not None:
            if (existing["ae_id"], existing["outcome"], existing["context"], existing["next_step"], existing["meeting_at"]) != (
                    ae_id, outcome, context, next_step, meeting_at):
                flash("A handoff already exists for this contact. Review its saved next step.")
            return redirect(url_for("sales_extensions.handoff_detail", handoff_id=existing["id"]), code=303)
        _revision_matches(account)
        person = connection.execute("""
            SELECT id, name, title, email, persona FROM people WHERE id = ? AND account_id = ?
        """, (person_id, account_id)).fetchone()
        if person is None or not any(ae["id"] == ae_id for ae in _users(connection, "ae")):
            abort(400, description="Choose a contact from this account and an active receiving AE.")
        if outcome == "meeting_booked" and person["persona"].strip().upper() not in {"P1", "P2"}:
            abort(400, description="A booked meeting qualifies for handoff with a P1 or P2 contact.")
        if is_blocked(connection, account_id, person_id):
            abort(403, description="Outreach restrictions prevent handing off this contact.")
        history = connection.execute("""
            SELECT a.channel, a.outcome, a.direction, a.note, a.occurred_at, u.name AS actor
            FROM activities a JOIN users u ON u.id = a.actor_id
            WHERE a.account_id = ? AND (a.person_id = ? OR a.person_id IS NULL)
            ORDER BY a.occurred_at DESC, a.id DESC LIMIT 10
        """, (account_id, person_id)).fetchall()
        payload = {
            "account": {"id": account_id, "name": account["name"]}, "contact": dict(person),
            "outcome": outcome, "ae_id": ae_id, "meeting_at": meeting_at, "context": context,
            "next_step": next_step, "conversation": [dict(row) for row in history],
        }
        handoff = connection.execute("""
            INSERT INTO handoffs (account_id, person_id, ae_id, requested_by, outcome, context,
                                  next_step, meeting_at, payload, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (account_id, person_id, ae_id, user["id"], outcome, context, next_step, meeting_at,
              json.dumps(payload, sort_keys=True), uuid4().hex, timestamp(at)))
        connection.execute("UPDATE people SET status = 'positive' WHERE id = ?", (person_id,))
        connection.execute("""
            INSERT INTO account_work (account_id, stage) VALUES (?, 'positive')
            ON CONFLICT (account_id) DO UPDATE SET stage = 'positive'
            WHERE account_work.stage != 'handed_off'
        """, (account_id,))
        audit(connection, user["id"], account_id, "handoff_created", {
            "handoff_id": handoff.lastrowid, "person_id": person_id, "ae_id": ae_id, "outcome": outcome,
        }, now=at)
    return redirect(url_for("sales_extensions.handoff_detail", handoff_id=handoff.lastrowid), code=303)


@bp.get("/handoffs")
def handoffs():
    user = _handoff_user()
    filters = {"search": request.args.get("search", "").strip(), "status": request.args.get("status", ""),
               "sort": request.args.get("sort", "recent"), "size": request.args.get("size", 25, type=int)}
    if filters["size"] not in {25, 50, 100}:
        filters["size"] = 25
    clauses = []
    params = []
    if user["role"] == "ae":
        clauses.append("h.ae_id = ? AND h.status = 'delivered'")
        params.append(user["id"])
    elif user["role"] == "sdr":
        clauses.append("""(work.owner_id = ? OR EXISTS (
            SELECT 1 FROM coverages c WHERE c.account_id = h.account_id AND c.owner_id = work.owner_id
              AND c.rep_id = ? AND c.cancelled_at IS NULL AND c.starts_at <= ? AND c.ends_at > ?
        ))""")
        now = timestamp(request_now())
        params.extend((user["id"], user["id"], now, now))
    elif user["role"] not in MANAGEMENT:
        abort(403)
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
    total = db().execute("SELECT COUNT(*) " + source, params).fetchone()[0]
    pages = max(1, (total + filters["size"] - 1) // filters["size"])
    page = min(pages, max(1, request.args.get("page", 1, type=int)))
    direction = "ASC" if filters["sort"] == "oldest" else "DESC"
    records = db().execute("SELECT h.*, a.name AS account_name, p.name AS person_name, ae.name AS ae_name "
                           + source + f" ORDER BY h.id {direction} LIMIT ? OFFSET ?",
                           (*params, filters["size"], (page - 1) * filters["size"])).fetchall()
    return render_template("sales_handoffs.html", handoffs=records, filters=filters, page=page, pages=pages,
                           outcomes=POSITIVE_OUTCOMES)


@bp.get("/handoffs/<int:handoff_id>")
def handoff_detail(handoff_id):
    user = _handoff_user()
    handoff = db().execute("""
        SELECT h.*, a.name AS account_name, ae.name AS ae_name, actor.name AS requester_name
        FROM handoffs h JOIN accounts a ON a.id = h.account_id
        JOIN users ae ON ae.id = h.ae_id JOIN users actor ON actor.id = h.requested_by
        WHERE h.id = ?
    """, (handoff_id,)).fetchone()
    if handoff is None:
        abort(404)
    _can_access_handoff(handoff, user)
    payload = json.loads(handoff["payload"])
    for activity in payload["conversation"]:
        activity["when_local"] = _display_local(activity["occurred_at"], user)
    return render_template("sales_handoff.html", handoff=handoff, payload=payload,
                           meeting_local=_display_local(handoff["meeting_at"], user),
                           managed_by_user=user["role"] in MANAGEMENT, outcomes=POSITIVE_OUTCOMES,
                           outreach_restricted=is_blocked(db(), handoff["account_id"], handoff["person_id"]))


@bp.post("/handoffs/<int:handoff_id>/retry")
def retry_handoff(handoff_id):
    user = _manage_user()
    connection = db()
    with transaction(connection):
        handoff = connection.execute("SELECT account_id, status FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        if handoff is None:
            abort(404)
        if handoff["status"] == "failed":
            connection.execute("UPDATE handoffs SET status = 'pending', last_error = NULL WHERE id = ?", (handoff_id,))
            audit(connection, user["id"], handoff["account_id"], "handoff_requeued", {"handoff_id": handoff_id},
                  now=request_now())
            flash("Handoff queued for delivery.")
    return redirect(url_for("sales_extensions.handoff_detail", handoff_id=handoff_id), code=303)


class DeliveryFailure(Exception):
    """An adapter failure safe for the manager to see."""


def deliver_one(connection, provider, *, clock=utcnow, lease_seconds=60):
    """Claim one handoff. The provider must deduplicate accepts by the stable key."""
    lease_id = uuid4().hex
    claim_at = clock()
    with transaction(connection):
        handoff = connection.execute("""
            SELECT * FROM handoffs WHERE status = 'pending'
              OR (status = 'delivering' AND lease_until <= ?) ORDER BY id LIMIT 1
        """, (timestamp(claim_at),)).fetchone()
        if handoff is None:
            return None
        if is_blocked(connection, handoff["account_id"], handoff["person_id"]):
            connection.execute("""
                UPDATE handoffs SET status = 'failed', last_error = ?, lease_token = NULL, lease_until = NULL
                WHERE id = ?
            """, ("Outreach became restricted. Review the account before retrying.", handoff["id"]))
            return {"id": handoff["id"], "status": "failed"}
        connection.execute("""
            UPDATE handoffs SET status = 'delivering', attempts = attempts + 1, lease_token = ?, lease_until = ?
            WHERE id = ?
        """, (lease_id, timestamp(claim_at + timedelta(seconds=lease_seconds)), handoff["id"]))

    receipt = None
    error = None
    try:
        receipt = provider.deliver(handoff["idempotency_key"], json.loads(handoff["payload"]))
        if not isinstance(receipt, str) or not receipt.strip():
            raise DeliveryFailure("Delivery provider did not return a receipt.")
    except DeliveryFailure as exc:
        error = str(exc)[:500]
    except Exception:
        error = "Delivery failed. Check the configured integration and retry."

    status = "failed" if error is not None else "delivered"
    with transaction(connection):
        changed = connection.execute("""
            UPDATE handoffs SET status = ?, receipt = ?, last_error = ?, delivered_at = ?,
                                lease_token = NULL, lease_until = NULL
            WHERE id = ? AND lease_token = ?
        """, (status, receipt if error is None else None, error, timestamp(clock()) if error is None else None,
              handoff["id"], lease_id))
        if changed.rowcount != 1:
            return {"id": handoff["id"], "status": "lease_lost"}
        if status == "delivered":
            connection.execute("UPDATE people SET status = 'handed_off' WHERE id = ? AND status = 'positive'",
                               (handoff["person_id"],))
            connection.execute("""
                UPDATE account_work SET stage = 'handed_off' WHERE account_id = ? AND stage = 'positive'
            """, (handoff["account_id"],))
    return {"id": handoff["id"], "status": status}


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
