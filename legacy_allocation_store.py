"""SQL adapter for matching legacy Cvent allocations to current CRM owners."""

from account_redirects import canonical_account_id
from assignment_store import SqlAssignmentStore
from workflow_store import audit


class SqlLegacyAllocationStore(SqlAssignmentStore):
    def legacy_company(self, company_id):
        row = self.connection.execute("""
            SELECT account_id FROM imported_source_evidence
            WHERE source_slug = 'cvent' AND external_id = ?
        """, (f"company:{company_id}",)).fetchone()
        if row is None:
            return None
        account_id = canonical_account_id(self.connection, row["account_id"])
        account = self.connection.execute("""
            SELECT a.id, a.name, w.owner_id, COALESCE(w.stage, 'discovered') AS stage,
                   COALESCE(w.revision, 0) AS revision
            FROM accounts a LEFT JOIN account_work w ON a.id = w.account_id WHERE a.id = ?
        """, (account_id,)).fetchone()
        return dict(account) if account else None

    def sdr_id(self, email):
        row = self.connection.execute("""
            SELECT id FROM users WHERE email = ? AND role = 'sdr' AND active = 1
        """, (email,)).fetchone()
        return row["id"] if row else None

    def active_admin(self, email):
        return self.connection.execute("""
            SELECT id, role, active, password_hash FROM users
            WHERE email = ? AND role = 'admin' AND active = 1
        """, (email,)).fetchone()

    def record_import(self, actor_id, allocations, version, observed):
        audit(self.connection, actor_id, None, "legacy_cvent_allocations",
              {"fingerprint": version, "allocations": allocations}, observed)
