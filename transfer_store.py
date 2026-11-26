"""SQL transactions for the explicit prepared-data transfer command."""

from contextlib import closing

from account_redirects import account_identity_rows
from crm_store import connect, insert_rows, table_exists
from suppression_projection import account_projection_available, refresh_account_suppression
from transfer_company_roster import SqlCompanyRosterTransfer
from transfer_source_contacts import SqlContactTransfer, read_source_contacts
from workflow_store import audit, transaction


ACCOUNT_FIELDS = ("identity_key", "name", "domain", "organization_type", "name_source", "name_source_ref", "root_domain")
EVIDENCE_FIELDS = ("source", "source_id", "source_url", "source_file", "review_reason", "details_json", "raw_json")
QUALIFICATION_FIELDS = ("score", "signals", "observed_at", "override")


def read_snapshot(path, *, companies_only=False):
    with closing(connect(path, read_only=True)) as source, source:
        source.execute("BEGIN")
        if source.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("The local CRM has broken references. Repair them before transferring data.")
        roster = SqlCompanyRosterTransfer(source).snapshot() if companies_only else None
        roster_account_ids = sorted({row["account_id"] for row in roster[0]}) if roster is not None else []
        placeholders = ", ".join("?" for _ in roster_account_ids)
        roster_accounts = f"OR a.id IN ({placeholders})" if roster_account_ids else ""
        accounts = source.execute(f"""
            SELECT a.*, COALESCE(w.stage, 'discovered') AS stage
            FROM accounts a LEFT JOIN account_work w ON w.account_id = a.id
            WHERE EXISTS (SELECT 1 FROM source_records r WHERE r.account_id = a.id)
            {roster_accounts}
            ORDER BY a.id
        """, tuple(roster_account_ids)).fetchall()
        evidence = source.execute("SELECT * FROM source_records ORDER BY id").fetchall()
        qualifications = {row["account_id"]: row for row in source.execute("SELECT * FROM qualifications")}
        if companies_only:
            return accounts, evidence, qualifications, None, roster
        contacts = read_source_contacts(source)
    return accounts, evidence, qualifications, contacts


def transfer_transaction(connection, *, apply, postgres, require_apply_ready=False):
    if not all(table_exists(connection, table) for table in
               ("users", "accounts", "account_work", "source_records", "qualifications", "people")):
        raise ValueError("Start the hosted CRM on the destination before transferring records.")
    if (apply or require_apply_ready) and not account_projection_available(connection):
        raise ValueError("Start the updated CRM on the destination before transferring records.")
    if apply:
        return transaction(connection)
    if postgres:
        connection.execute("SET default_transaction_isolation = 'repeatable read'")
    connection.execute("BEGIN")
    return connection


class SqlSnapshotTransfer:
    def __init__(self, connection):
        self.connection = connection
        self.contacts = SqlContactTransfer(connection)
        self.roster = SqlCompanyRosterTransfer(connection)

    def accounts(self):
        return account_identity_rows(self.connection)

    def evidence(self):
        return self.connection.execute("SELECT id, source, source_id, account_id FROM source_records").fetchall()

    def add_accounts(self, rows):
        rows = list(rows)
        insert_rows(self.connection, "accounts", ACCOUNT_FIELDS, (tuple(row[field] for field in ACCOUNT_FIELDS) for row in rows))
        account_ids = dict(self.connection.execute("SELECT identity_key, id FROM accounts").fetchall())
        refresh_account_suppression(self.connection, (account_ids[row["identity_key"]] for row in rows))
        return account_ids

    def add_work(self, rows):
        insert_rows(self.connection, "account_work", ("account_id", "stage"), rows)

    def add_qualifications(self, rows):
        insert_rows(self.connection, "qualifications", ("account_id", *QUALIFICATION_FIELDS), (
            (account_id, *(row[field] for field in QUALIFICATION_FIELDS)) for account_id, row in rows
        ))

    def add_evidence(self, rows):
        insert_rows(self.connection, "source_records", ("account_id", *EVIDENCE_FIELDS), (
            (account_id, *(row[field] for field in EVIDENCE_FIELDS)) for row, account_id in rows
        ))
        return {(row["source"], row["source_id"]): row["id"] for row in self.evidence()}

    def destination_admin(self, email):
        return self.connection.execute("""
            SELECT id, password_hash FROM users WHERE email = ? AND role = 'admin' AND active = 1
        """, (email,)).fetchone()

    def record_transfer(self, actor_id, counts):
        audit(self.connection, actor_id, None, "saved_sources_transferred", counts)
