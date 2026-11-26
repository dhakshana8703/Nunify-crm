"""SQL reads and writes for the saved Cvent company roster transfer."""

import json

from crm_store import insert_rows, table_exists


COMPANY_SOURCE = "cvent"
ROSTER_SOURCE = "cvent-company-roster"
COMPANY_ID_PREFIX = "company:%"
COMPANY_FIELDS = ("external_id", "source_url", "observed_at", "payload_json")
RUN_FIELDS = ("input_name", "trigger", "state", "queued_at", "started_at", "finished_at",
              "attempts", "request_key", "request_hash")
ROW_FIELDS = ("row_number", "payload_json", "result", "detail")


def _decoded_rows(rows):
    return [dict(row, payload=json.loads(row["payload_json"])) for row in rows]


class SqlCompanyRosterTransfer:
    def __init__(self, connection):
        self.connection = connection

    def evidence(self):
        return _decoded_rows(self.connection.execute(f"""
            SELECT account_id, {', '.join(COMPANY_FIELDS)} FROM imported_source_evidence
            WHERE source_slug = ? AND external_id LIKE ?
            ORDER BY external_id
        """, (COMPANY_SOURCE, COMPANY_ID_PREFIX)))

    def runs(self, *, finished_only=False):
        completed = "AND state IN ('complete', 'partial')" if finished_only else ""
        runs = self.connection.execute(f"""
            SELECT id, {', '.join(RUN_FIELDS)} FROM source_runs WHERE source_slug = ? {completed} ORDER BY id
        """, (ROSTER_SOURCE,)).fetchall()
        return [(run, _decoded_rows(self.connection.execute(f"""
            SELECT {', '.join(ROW_FIELDS)} FROM source_run_rows WHERE run_id = ? ORDER BY row_number
        """, (run["id"],)))) for run in runs]

    def add_evidence(self, rows):
        insert_rows(self.connection, "imported_source_evidence", ("source_slug", "account_id", *COMPANY_FIELDS),
                    ((COMPANY_SOURCE, account_id, *(row[field] for field in COMPANY_FIELDS)) for row, account_id in rows))

    def add_runs(self, runs):
        self.connection.execute("""
            INSERT INTO ingest_sources(slug, name, adapter) VALUES (?, 'Cvent Company Roster', 'external')
            ON CONFLICT (slug) DO NOTHING
        """, (ROSTER_SOURCE,))
        placeholders = ", ".join("?" for _ in range(len(RUN_FIELDS) + 1))
        for run, rows in runs:
            run_id = self.connection.execute(f"""
                INSERT INTO source_runs (source_slug, {', '.join(RUN_FIELDS)}) VALUES ({placeholders})
            """, (ROSTER_SOURCE, *(run[field] for field in RUN_FIELDS))).lastrowid
            insert_rows(self.connection, "source_run_rows", ("run_id", *ROW_FIELDS),
                        ((run_id, *(row[field] for field in ROW_FIELDS)) for row in rows))

    def snapshot(self):
        if not all(table_exists(self.connection, table) for table in
                   ("imported_source_evidence", "source_runs", "source_run_rows")):
            raise ValueError("Open the local CRM on the current release before transferring the saved company roster.")
        return self.evidence(), self.runs(finished_only=True)
