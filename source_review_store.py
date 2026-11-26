"""Persistence and read queries for the staged source review workflow."""

import json

from crm_core.source_review import replay_result as replay_decision
from workflow_store import audit, is_blocked, timestamp


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_review_decisions (
    decision_key TEXT PRIMARY KEY,
    run_id INTEGER NOT NULL,
    row_number INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held', 'excluded', 'linked')),
    reason TEXT NOT NULL,
    evidence_url TEXT NOT NULL DEFAULT '',
    account_id INTEGER REFERENCES accounts(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    decided_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (run_id, row_number) REFERENCES source_run_rows(run_id, row_number),
    CHECK ((state = 'linked') = (account_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS source_review_queue_rows ON source_run_rows(result, run_id, row_number);
"""
REVIEW_ROWS = """
WITH staged AS (
    SELECT sr.run_id, sr.row_number, sr.payload_json, sr.result, sr.detail, run.source_slug,
           COALESCE(json_extract(sr.payload_json, '$.source_id'), '') AS source_id,
           COALESCE(json_extract(sr.payload_json, '$.details.event_name'),
                    json_extract(sr.payload_json, '$.details.name'), '') AS title,
           COALESCE(json_extract(sr.payload_json, '$.details.organizer_company'), '') AS organizer,
           CASE WHEN sr.result = 'review' AND COALESCE(json_extract(sr.payload_json, '$.source_id'), '') <> ''
                     AND COALESCE(CAST(json_extract(sr.payload_json, '$.duplicate') AS TEXT), '0') IN ('0', 'false')
                     AND COALESCE(CAST(json_extract(sr.payload_json, '$.duplicate_id') AS TEXT), '0') IN ('0', 'false')
                THEN run.source_slug || ':' || json_extract(sr.payload_json, '$.source_id')
                ELSE 'row:' || sr.run_id || ':' || sr.row_number END AS decision_key
    FROM source_run_rows sr JOIN source_runs run ON run.id = sr.run_id
    WHERE run.source_slug IN ('cvent', 'directory') AND run.state IN ('ready', 'complete', 'partial')
      AND sr.result IN ('review', 'invalid')
), reviewed AS (
    SELECT staged.*,
           CASE WHEN d.state = 'linked' AND staged.payload_json <> original.payload_json THEN NULL
                ELSE d.state END AS decision,
           d.reason, d.evidence_url, d.account_id, d.decided_at,
           COALESCE(d.revision, 0) AS revision
    FROM staged LEFT JOIN source_review_decisions d USING (decision_key)
    LEFT JOIN source_run_rows original ON original.run_id = d.run_id AND original.row_number = d.row_number
)
"""


def initialize(connection):
    connection.executescript(SCHEMA)



def replay_result(connection, source, payload):
    return replay_decision(ReviewStore(connection), source, payload)


class ReviewStore:
    def __init__(self, connection):
        self.connection = connection

    def review_row(self, run_id, row_number):
        return self.connection.execute(REVIEW_ROWS + """
            SELECT * FROM reviewed WHERE run_id = ? AND row_number = ?
        """, (run_id, row_number)).fetchone()

    def active_run(self, source):
        return self.connection.execute("""
            SELECT 1 FROM source_runs WHERE source_slug = ? AND state IN ('queued', 'running') LIMIT 1
        """, (source,)).fetchone() is not None

    def has_newer_snapshot(self, source, run_id, source_id, payload_json):
        return self.connection.execute("""
            SELECT 1 FROM source_run_rows sr JOIN source_runs r ON r.id = sr.run_id
            WHERE r.source_slug = ? AND r.state IN ('ready', 'complete', 'partial') AND r.id > ?
              AND json_extract(sr.payload_json, '$.source_id') = ? AND sr.payload_json <> ? LIMIT 1
        """, (source, run_id, source_id, payload_json)).fetchone() is not None

    def existing_link(self, source, source_id):
        return self.connection.execute("""
            SELECT a.id, a.name FROM source_records sr JOIN accounts a ON a.id = sr.account_id
            WHERE sr.source = ? AND sr.source_id = ?
        """, (source, source_id)).fetchone()

    def account(self, account_id):
        return self.connection.execute("""
            SELECT a.id, a.name, a.domain FROM accounts a WHERE a.id = ?
            AND NOT EXISTS (SELECT 1 FROM account_merges m WHERE m.from_id = a.id)
        """, (account_id,)).fetchone()

    def is_blocked(self, account_id):
        return is_blocked(self.connection, account_id)

    def save_link(self, source, source_id, account_id, details, payload):
        self.connection.execute("""
            INSERT INTO source_records
                (source, source_id, source_url, source_file, account_id, review_reason, details_json, raw_json)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT (source, source_id) DO UPDATE SET
                source_url = excluded.source_url, source_file = excluded.source_file,
                account_id = excluded.account_id, review_reason = NULL,
                details_json = excluded.details_json, raw_json = excluded.raw_json
            WHERE source_records.account_id IS NULL
        """, (source, source_id, payload["source_url"], payload["source_file"], account_id,
              json.dumps(details, sort_keys=True), json.dumps(payload["raw"], sort_keys=True)))

    def save_decision(self, row, state, reason, evidence_url, account_id, actor_id, observed):
        self.connection.execute("""
            INSERT INTO source_review_decisions
                (decision_key, run_id, row_number, state, reason, evidence_url, account_id, actor_id, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (decision_key) DO UPDATE SET
                run_id = excluded.run_id, row_number = excluded.row_number, state = excluded.state,
                reason = excluded.reason, evidence_url = excluded.evidence_url, account_id = excluded.account_id,
                actor_id = excluded.actor_id, decided_at = excluded.decided_at,
                revision = source_review_decisions.revision + 1
        """, (row["decision_key"], row["run_id"], row["row_number"], state, reason, evidence_url, account_id, actor_id,
              timestamp(observed)))
        audit(self.connection, actor_id, account_id, "source_review_decision", {
            "source": row["source_slug"], "source_id": row["source_id"], "run_id": row["run_id"],
            "row_number": row["row_number"], "action": state, "reason": reason, "evidence_url": evidence_url,
        }, observed)

    def replay_decision(self, source, source_id):
        return self.connection.execute("""
            SELECT d.state, d.reason, d.account_id, sr.payload_json, linked.account_id AS linked_account_id
            FROM source_review_decisions d
            JOIN source_run_rows sr ON sr.run_id = d.run_id AND sr.row_number = d.row_number
            LEFT JOIN source_records linked ON linked.source = ? AND linked.source_id = ?
            WHERE d.decision_key = ? AND d.state IN ('excluded', 'linked')
        """, (source, source_id, f"{source}:{source_id}")).fetchone()

    def page(self, source, scope, result, search, run_id, sort, size, page):
        orders = {"newest": "run_id DESC, row_number", "oldest": "run_id, row_number",
                  "name": "title COLLATE NOCASE, run_id DESC, row_number"}
        filters = ["(? = '' OR source_slug = ?)", "(? = '' OR result = ?)",
                   "INSTR(LOWER(title || ' ' || organizer || ' ' || source_id || ' ' || COALESCE(detail, '')), ?) > 0"]
        parameters = [source, source, result, result, search]
        if run_id is not None:
            filters.append("run_id = ?")
            parameters.append(run_id)
        else:
            filters.append("latest = 1")
        if scope == "open":
            filters.append("(decision IS NULL OR decision = 'held')")
        elif scope != "all":
            filters.append("decision = ?")
            parameters.append(scope)
        rows = REVIEW_ROWS + """
            , latest_rows AS (
                SELECT reviewed.*, ROW_NUMBER() OVER (PARTITION BY decision_key ORDER BY run_id DESC, row_number) AS latest
                FROM reviewed
            )
        """
        where = " AND ".join(filters)
        total = self.connection.execute(rows + f"SELECT COUNT(*) FROM latest_rows WHERE {where}", parameters).fetchone()[0]
        pages = max(1, (total + size - 1) // size)
        page = min(pages, max(1, page))
        items = self.connection.execute(rows + f"""
            SELECT * FROM latest_rows WHERE {where} ORDER BY {orders[sort]} LIMIT ? OFFSET ?
        """, (*parameters, size, (page - 1) * size)).fetchall()
        return items, dict(size=size, page=page, pages=pages, total=total)

    def candidates(self, search, host, organizer):
        if search:
            return self.connection.execute("""
                SELECT a.id, a.name, a.domain FROM accounts a WHERE INSTR(LOWER(a.name || ' ' || a.domain), LOWER(?)) > 0
                AND NOT EXISTS (SELECT 1 FROM account_merges m WHERE m.from_id = a.id)
                ORDER BY name COLLATE NOCASE LIMIT 30
            """, (search,)).fetchall()
        return self.connection.execute("""
            SELECT a.id, a.name, a.domain FROM accounts a
            WHERE (LOWER(a.domain) IN (?, ?) OR LOWER(a.name) = LOWER(?))
            AND NOT EXISTS (SELECT 1 FROM account_merges m WHERE m.from_id = a.id)
            ORDER BY name COLLATE NOCASE LIMIT 30
        """, (host, "www." + host, organizer)).fetchall()
