"""SQL crosswalk and ledger adapter for reviewed historical charges."""

import json
from functools import cached_property

from account_redirects import canonical_account_id
from crm_core.credit_history import credit_amount_text
from crm_store import table_exists
from workflow_store import audit


IMPORT_ACTION = "cvent_credit_history_import"


class SqlCreditHistoryStore:
    def __init__(self, connection):
        self.connection = connection

    @cached_property
    def _has_apollo_leads(self):
        return table_exists(self.connection, "cvent_apollo_leads")

    def admin_credentials(self, email):
        return self.connection.execute("""
            SELECT id, password_hash FROM users WHERE email = ? AND role = 'admin' AND active = 1
        """, (email,)).fetchone()

    def history_import(self):
        rows = self.connection.execute("""
            SELECT details FROM audit_events WHERE action = ? ORDER BY id LIMIT 2
        """, (IMPORT_ACTION,)).fetchall()
        if len(rows) > 1:
            raise ValueError("The history contains more than one Cvent opening statement audit.")
        return json.loads(rows[0]["details"]) if rows else None

    def historical_postings(self):
        return [dict(row, credits=credit_amount_text(row["credits"])) for row in self.connection.execute("""
            SELECT user_id, account_id, person_id, action, credits, at, on_day, source, reference
            FROM credit_ledger WHERE source IN ('historical', 'adjustment') ORDER BY id
        """)]

    def mapped_user(self, email):
        if email is None:
            return None
        return self.connection.execute("SELECT id, timezone FROM users WHERE email = ?", (email,)).fetchone()

    def mapped_company(self, legacy_company_id):
        if legacy_company_id is None:
            return None
        row = self.connection.execute("""
            SELECT account_id FROM imported_source_evidence WHERE source_slug = 'cvent' AND external_id = ?
        """, (f"company:{legacy_company_id}",)).fetchone()
        return canonical_account_id(self.connection, row["account_id"]) if row else None

    def mapped_person(self, apollo_id):
        if apollo_id is None or not self._has_apollo_leads:
            return None
        row = self.connection.execute("""
            SELECT p.id, p.account_id, l.legacy_company_id FROM cvent_apollo_leads l
            JOIN people p ON p.id = l.person_id WHERE l.apollo_id = ?
        """, (apollo_id,)).fetchone()
        return dict(row, account_id=canonical_account_id(self.connection, row["account_id"])) if row else None

    def post(self, posting):
        self.connection.execute("""
            INSERT INTO credit_ledger(user_id, account_id, person_id, action, credits, on_day, at, source, reference)
            VALUES (:user_id, :account_id, :person_id, :action, :credits, :on_day, :at, :source, :reference)
        """, posting)

    def record_summary(self, actor_id, proof, observed):
        audit(self.connection, actor_id, None, IMPORT_ACTION, proof, observed)
