"""SQL adapter for the follow-up, contact and work-queue ports."""

import json
import sqlite3

from crm_core.contacts import ContactConflict
from legacy_contact_store import evidence_for_account
from workflow_store import active_coverage, audit, save_suppression_rule, suppression_reason, timestamp


class SqlWorkStore:
    def __init__(self, connection):
        self.connection = connection

    def previous_task(self, actor_id, key):
        return self.connection.execute("""
            SELECT person_id, request_hash FROM tasks WHERE created_by = ? AND request_key = ?
        """, (actor_id, key)).fetchone()

    def person_for(self, account_id, person_id):
        return self.connection.execute(
            "SELECT * FROM people WHERE account_id = ? AND id = ?", (account_id, person_id),
        ).fetchone()

    def restriction_for(self, account_id, person_id=None, *, candidate_email=None):
        return suppression_reason(self.connection, account_id, person_id, candidate_email=candidate_email)

    def save_task(self, task, occurred_at):
        task_id = self.connection.execute("""
            INSERT INTO tasks(account_id, person_id, owner_id, title, due_on, created_by, request_key, request_hash)
            VALUES (:account_id, :person_id, :owner_id, :title, :due_on, :actor_id, :key, :request_hash)
        """, task).lastrowid
        audit(self.connection, task["actor_id"], task["account_id"], "task_created", {
            "task_id": task_id, "person_id": task["person_id"], "owner_id": task["owner_id"], "due_on": task["due_on"],
        }, occurred_at)

    def task(self, task_id):
        return self.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def complete_task(self, task_id):
        self.connection.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))

    def snooze_task(self, task_id, due_on):
        self.connection.execute("UPDATE tasks SET status = 'snoozed', due_on = ? WHERE id = ? AND status != 'done'",
                                (due_on, task_id))

    def activity(self, activity_id):
        return self.connection.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()

    def acknowledge_reply(self, activity_id, actor_id, occurred_at):
        self.connection.execute("""
            UPDATE activities SET acknowledged_at = ?, acknowledged_by = ?
            WHERE id = ? AND acknowledged_at IS NULL
        """, (timestamp(occurred_at), actor_id, activity_id))

    def _contact_write(self, sql, params):
        try:
            return self.connection.execute(sql, params)
        except sqlite3.IntegrityError as error:
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                raise ContactConflict("This email or LinkedIn profile already belongs to a contact on the account.") from error
            raise

    def add_contact(self, account_id, fields, actor_id, occurred_at):
        person_id = self._contact_write("""
            INSERT INTO people(account_id, name, title, email, phone, linkedin_url, persona)
            VALUES (:account_id, :name, :title, :email, :phone, :linkedin_url, :persona)
        """, dict(fields, account_id=account_id)).lastrowid
        audit(self.connection, actor_id, account_id, "contact_add", {"person_id": person_id}, occurred_at)
        return person_id

    def edit_contact(self, account_id, person_id, fields, changed, actor_id, occurred_at):
        self._contact_write("""
            UPDATE people SET name = :name, title = :title, email = :email, phone = :phone,
                              linkedin_url = :linkedin_url, persona = :persona, details_revision = details_revision + 1
            WHERE account_id = :account_id AND id = :person_id
        """, dict(fields, account_id=account_id, person_id=person_id))
        audit(self.connection, actor_id, account_id, "contact_edit", {"person_id": person_id, "fields": changed}, occurred_at)

    def save_status(self, account_id, contact, status, blocked, actor_id, occurred_at):
        person_id = contact["id"]
        self.connection.execute("""
            UPDATE people SET status = ?, do_not_contact = ?, details_revision = details_revision + 1
            WHERE account_id = ? AND id = ?
        """, (status, int(blocked), account_id, person_id))
        if blocked and contact["email"]:
            save_suppression_rule(self.connection, "email", contact["email"], "opt_out", actor_id, occurred_at,
                                     source="outreach", reason_note="Marked do not contact")
        audit(self.connection, actor_id, account_id, "contact_status",
              {"person_id": person_id, "previous_status": contact["status"], "status": status}, occurred_at)

    def due_tasks(self, account_id, cutoff):
        return self.connection.execute("""
            SELECT * FROM tasks WHERE account_id = ? AND status IN ('open', 'snoozed') AND due_on <= ?
        """, (account_id, cutoff.isoformat())).fetchall()

    def open_tasks(self, owner_id):
        owner_filter = " AND t.owner_id = ?" if owner_id is not None else ""
        parameters = (owner_id,) if owner_id is not None else ()
        return self.connection.execute(f"""
            SELECT t.id, t.account_id, t.person_id, t.title, t.due_on, t.status, u.name AS owner_name
            FROM tasks t JOIN users u ON u.id = t.owner_id
            WHERE u.role = 'sdr' AND t.status IN ('open', 'snoozed'){owner_filter}
            ORDER BY t.due_on, t.id
        """, parameters).fetchall()

    def sdr_queue_options(self):
        return self.connection.execute("""
            SELECT id, name, active FROM users WHERE role = 'sdr' ORDER BY name COLLATE NOCASE, id
        """).fetchall()

    def coverage_for(self, account, current_time):
        return active_coverage(self.connection, account, current_time)

    def pending_replies(self, account_id, outcomes):
        outcomes = tuple(sorted(outcomes))
        if not outcomes:
            return []
        placeholders = ", ".join("?" for _ in outcomes)
        return self.connection.execute(f"""
            SELECT a.id, a.person_id, a.outcome, p.name FROM activities a LEFT JOIN people p ON p.id = a.person_id
            WHERE a.account_id = ? AND a.direction = 'inbound' AND a.acknowledged_at IS NULL
              AND a.outcome IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM activities response
                  WHERE response.account_id = a.account_id AND response.person_id = a.person_id
                    AND response.direction = 'outbound'
                    AND (response.occurred_at, response.id) > (a.occurred_at, a.id)
              )
        """, (account_id, *outcomes)).fetchall()

    def activity_since(self, account_id, assigned_at):
        return self.connection.execute("SELECT 1 FROM activities WHERE account_id = ? AND occurred_at >= ? LIMIT 1",
                                       (account_id, assigned_at)).fetchone() is not None

    def people_on_account(self, account_id):
        return self.connection.execute("SELECT * FROM people WHERE account_id = ? ORDER BY name COLLATE NOCASE",
                                       (account_id,)).fetchall()

    def people_on_accounts(self, account_ids):
        return self.connection.execute("""
            SELECT id, account_id, name, title, email, persona, status, origin, email_verified,
                   unlocked, linkedin_url FROM people
            WHERE account_id IN (SELECT value FROM json_each(?))
        """, (json.dumps(list(account_ids)),)).fetchall()

    def account_timeline(self, account_id):
        return self.connection.execute("""
            SELECT a.*, u.name AS actor_name, p.name AS contact_name
            FROM activities a JOIN users u ON u.id = a.actor_id LEFT JOIN people p ON p.id = a.person_id
            WHERE a.account_id = ? ORDER BY a.occurred_at DESC, a.id DESC
        """, (account_id,)).fetchall()

    def contact_timeline(self, account_id, person_id):
        return self.connection.execute("""
            SELECT a.*, u.name AS actor_name FROM activities a JOIN users u ON u.id = a.actor_id
            WHERE a.account_id = ? AND a.person_id = ?
            ORDER BY a.occurred_at DESC, a.id DESC
        """, (account_id, person_id)).fetchall()

    def account_tasks(self, account_id):
        return self.connection.execute("""
            SELECT t.*, u.name AS owner_name, p.name AS contact_name
            FROM tasks t JOIN users u ON u.id = t.owner_id
            LEFT JOIN people p ON p.id = t.person_id
            WHERE t.account_id = ? ORDER BY t.due_on, t.id
        """, (account_id,)).fetchall()

    def contact_tasks(self, account_id, person_id):
        return self.connection.execute("""
            SELECT t.*, u.name AS owner_name FROM tasks t JOIN users u ON u.id = t.owner_id
            WHERE t.account_id = ? AND t.person_id = ? AND t.status IN ('open', 'snoozed')
            ORDER BY t.due_on, t.id
        """, (account_id, person_id)).fetchall()

    def active_ae_exists(self):
        return self.connection.execute("SELECT 1 FROM users WHERE role = 'ae' AND active = 1 LIMIT 1").fetchone() is not None

    def contact_evidence(self, account_id, person_id):
        return [item for item in evidence_for_account(self.connection, account_id) if item["person_id"] == person_id]

    def qualification_scores(self):
        return dict(self.connection.execute("SELECT account_id, score FROM qualifications").fetchall())
