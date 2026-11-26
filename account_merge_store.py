"""SQL snapshot and transactional persistence for account merges."""

import json

from account_identity_schema import refresh_account_roots
from crm_store import table_exists
from suppression_projection import account_projection_available, refresh_account_suppression
from workflow_store import audit, timestamp


REPOINT_TABLES = (
    "source_records", "imported_source_evidence", "source_review_decisions", "people", "activities", "tasks",
    "audit_events", "coverages", "handoffs", "credit_jobs", "credit_ledger",
)
HISTORY_TABLES = (*REPOINT_TABLES, "account_work", "qualifications", "qualification_job_accounts")


class SqlAccountMergeStore:
    def __init__(self, connection):
        self.connection = connection

    def accounts(self, ids=None):
        conditions = []
        parameters = ()
        if table_exists(self.connection, "account_merges"):
            conditions.append("NOT EXISTS (SELECT 1 FROM account_merges WHERE from_id = a.id)")
        if ids is not None:
            conditions.append("a.id IN (SELECT value FROM json_each(?))")
            parameters = (json.dumps(ids),)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return [dict(row) for row in self.connection.execute(
            "SELECT a.id, a.identity_key, a.name, a.domain, a.organization_type FROM accounts a" + where + " ORDER BY a.id",
            parameters,
        )]

    def history(self, ids):
        parameters = (json.dumps(ids),)
        history = {}
        for table in HISTORY_TABLES:
            if not table_exists(self.connection, table):
                history[table] = []
                continue
            source = table + " r"
            fields = "r.*"
            if table == "qualification_job_accounts":
                source += " JOIN qualification_jobs j ON j.id = r.job_id"
                fields += ", j.state"
            rows = self.connection.execute(
                f"SELECT {fields} FROM {source} WHERE r.account_id IN (SELECT value FROM json_each(?))", parameters,
            )
            history[table] = sorted(map(dict, rows), key=lambda row: json.dumps(row, sort_keys=True))
        return history

    def actor(self, actor_id):
        return self.connection.execute("SELECT role, active FROM users WHERE id = ?", (actor_id,)).fetchone()

    def merge(self, plan, *, actor_id, observed):
        winner = plan["survivor"]["id"]
        losers = [row["id"] for row in plan["duplicates"]]
        witness = self.history([winner, *losers])
        for table in REPOINT_TABLES:
            if plan["counts"][table]:
                self.connection.execute(
                    f"UPDATE {table} SET account_id = ? WHERE account_id IN (SELECT value FROM json_each(?))",
                    (winner, json.dumps(losers)),
                )
        self._merge_allocations(winner, plan["allocation"])
        self._merge_qualifications(winner, plan["qualification"])
        if plan["counts"]["qualification_job_accounts"]:
            self._repoint_completed_batches(winner, losers)
        self.connection.executemany(
            "INSERT INTO account_merges(from_id, to_id, merged_at) VALUES (?, ?, ?)",
            [(loser, winner, timestamp(observed)) for loser in losers],
        )
        refresh_account_roots(self.connection)
        if account_projection_available(self.connection):
            refresh_account_suppression(self.connection, (winner,))
        audit(self.connection, actor_id, winner, "account_merge", {
            "from_ids": losers, "to_id": winner, "counts": plan["counts"],
            "allocations_before": witness["account_work"], "qualifications_before": witness["qualifications"],
        }, observed)
        return winner

    def _merge_allocations(self, winner, allocation):
        if allocation is None:
            return
        self.connection.execute("""
            INSERT INTO account_work (account_id, owner_id, stage, assigned_at, revision)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (account_id) DO UPDATE SET owner_id = excluded.owner_id, stage = excluded.stage,
                assigned_at = excluded.assigned_at, revision = excluded.revision
        """, (winner, allocation["owner_id"], allocation["stage"], allocation["assigned_at"], allocation["revision"]))

    def _merge_qualifications(self, winner, qualification):
        if qualification is None:
            return
        self.connection.execute("""
            INSERT INTO qualifications (account_id, score, signals, observed_at, override)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (account_id) DO UPDATE SET score = excluded.score, signals = excluded.signals,
                observed_at = excluded.observed_at, override = excluded.override
        """, (winner, qualification["score"], qualification["signals"], qualification["observed_at"],
              qualification["override"]))

    def _repoint_completed_batches(self, winner, losers):
        # Keep duplicate historical targets on their retired rows when the batch already has a survivor target.
        for loser in losers:
            self.connection.execute("""
                UPDATE qualification_job_accounts AS target SET account_id = ? WHERE target.account_id = ?
                AND NOT EXISTS (SELECT 1 FROM qualification_job_accounts saved
                                WHERE saved.job_id = target.job_id AND saved.account_id = ?)
            """, (winner, loser, winner))
