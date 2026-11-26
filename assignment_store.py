"""Persist a CRM assignment on the caller's SQL transaction."""

from datetime import timedelta

from crm_core.assignment import AssignmentChange
from workflow_store import audit, suppression_reason, timestamp


class SqlAssignmentStore:
    def __init__(self, connection):
        self.connection = connection

    def restriction_for(self, account_id):
        return suppression_reason(self.connection, account_id)

    def active_sdr(self, owner_id):
        return self.connection.execute(
            "SELECT id FROM users WHERE id = ? AND role = 'sdr' AND active = 1", (owner_id,),
        ).fetchone() is not None

    def rep_workloads(self, observed_at):
        return self.connection.execute("""
            SELECT u.id, u.name,
                   (SELECT COUNT(*) FROM account_work w WHERE w.owner_id = u.id
                    AND NOT EXISTS (SELECT 1 FROM account_merges WHERE from_id = w.account_id)) AS account_count,
                   (SELECT COUNT(*) FROM tasks WHERE owner_id = u.id AND status != 'done') AS open_tasks,
                   (SELECT COUNT(*) FROM activities WHERE actor_id = u.id AND direction = 'outbound'
                     AND occurred_at BETWEEN ? AND ?) AS touches
            FROM users u WHERE u.role = 'sdr' AND u.active = 1 ORDER BY u.name COLLATE NOCASE, u.id
        """, (timestamp(observed_at - timedelta(days=7)), timestamp(observed_at))).fetchall()

    def save(self, change: AssignmentChange):
        at = timestamp(change.changed_at)
        self.connection.execute("""
            INSERT INTO account_work(account_id, owner_id, stage, revision, assigned_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET owner_id = excluded.owner_id, stage = excluded.stage,
                revision = excluded.revision, assigned_at = excluded.assigned_at
        """, (change.account_id, change.owner_id, change.stage, change.revision, at))
        self.connection.execute(
            "UPDATE tasks SET owner_id = ? WHERE account_id = ? AND status != 'done'",
            (change.owner_id, change.account_id),
        )
        self.connection.execute("""
            UPDATE coverages SET cancelled_at = ?
            WHERE account_id = ? AND cancelled_at IS NULL AND ends_at > ?
        """, (at, change.account_id, at))
        audit(self.connection, change.actor_id, change.account_id, "assignment", {
            "previous_owner_id": change.previous_owner_id, "owner_id": change.owner_id, "method": change.method,
        }, change.changed_at)
