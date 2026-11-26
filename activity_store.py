"""Persist an activity and its related work on one SQL transaction."""

from crm_core.activity import ActivityChange
from workflow_store import save_suppression_rule, suppression_reason, timestamp


class SqlActivityStore:
    def __init__(self, connection):
        self.connection = connection

    def previous_activity(self, actor_id, key):
        return self.connection.execute(
            "SELECT request_hash FROM activities WHERE actor_id = ? AND request_key = ?", (actor_id, key),
        ).fetchone()

    def person_for(self, account_id, person_id):
        return self.connection.execute(
            "SELECT id, name, email, do_not_contact FROM people WHERE id = ? AND account_id = ?",
            (person_id, account_id),
        ).fetchone()

    def restriction_for(self, account_id, person_id):
        return suppression_reason(self.connection, account_id, person_id)

    def save(self, change: ActivityChange):
        data = change.submission
        activity_id = self.connection.execute("""
            INSERT INTO activities(account_id, person_id, actor_id, channel, outcome, direction, note,
                                   occurred_at, request_key, request_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (change.account_id, change.person_id, data.actor_id, data.channel, data.outcome, change.direction,
              data.note, timestamp(change.saved_at), data.key, change.request_hash)).lastrowid
        if change.due_on:
            self.connection.execute("""
                INSERT INTO tasks(account_id, person_id, owner_id, activity_id, title, due_on)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (change.account_id, change.person_id, change.task_owner_id, activity_id,
                  change.task_title, change.due_on))
        if change.person_status:
            self.connection.execute("""
                UPDATE people SET status = ?, do_not_contact = CASE WHEN ? = 1 THEN 1 ELSE do_not_contact END
                WHERE id = ?
            """, (change.person_status, int(change.do_not_contact), change.person_id))
        if change.opt_out_email:
            save_suppression_rule(self.connection, "email", change.opt_out_email, "opt_out", data.actor_id,
                                     change.saved_at, source="outreach")
        if change.stage:
            self.connection.execute("UPDATE account_work SET stage = ? WHERE account_id = ?",
                                    (change.stage, change.account_id))
