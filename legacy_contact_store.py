"""SQL adapter for saved Cvent contact evidence and Apollo lead imports."""

import json

from account_redirects import account_aliases, account_identity_rows
from crm_core.legacy_contacts import ORIGIN
from crm_store import table_exists
from workflow_store import audit, suppression_rules, timestamp


CVENT_CONTACT_EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cvent_contact_evidence (
    person_id INTEGER NOT NULL REFERENCES people(id),
    source_record_id INTEGER NOT NULL REFERENCES source_records(id),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (person_id, source_record_id)
)
"""
APOLLO_LEADS_SCHEMA = """
CREATE TABLE IF NOT EXISTS cvent_apollo_leads (
    apollo_id TEXT PRIMARY KEY,
    person_id INTEGER NOT NULL UNIQUE REFERENCES people(id),
    legacy_company_id INTEGER NOT NULL,
    company_domain TEXT NOT NULL,
    discovered_at TEXT,
    unlocked_at TEXT,
    imported_at TEXT NOT NULL
)
"""


def initialize_cvent_evidence(connection):
    connection.execute(CVENT_CONTACT_EVIDENCE_SCHEMA)


def evidence_for_account(connection, account_id):
    """Return provenance for use after an account or contact access check."""
    if not table_exists(connection, "cvent_contact_evidence"):
        return []
    return [
        dict(row)
        for row in connection.execute("""
            SELECT p.id AS person_id, s.source_id, s.source_url, e.observed_at,
                   json_extract(s.details_json, '$.event_name') AS event_name
            FROM cvent_contact_evidence e
            JOIN people p ON p.id = e.person_id
            JOIN source_records s ON s.id = e.source_record_id
            WHERE p.account_id = ? AND s.account_id = p.account_id AND s.source = 'cvent'
            ORDER BY e.observed_at DESC, s.id
        """, (account_id,))
    ]


class SqlLegacyContactStore:
    def __init__(self, connection):
        self.connection = connection
        self._has_cvent_evidence = None

    def active_admin(self, actor_id):
        return self.connection.execute(
            "SELECT 1 FROM users WHERE id = ? AND role = 'admin' AND active = 1", (actor_id,),
        ).fetchone() is not None

    def admin_credentials(self, email):
        return self.connection.execute(
            "SELECT id, password_hash FROM users WHERE email = ? AND role = 'admin' AND active = 1", (email,),
        ).fetchone()

    def prepare_cvent(self):
        initialize_cvent_evidence(self.connection)
        self._has_cvent_evidence = True

    def prepare_apollo(self):
        self.connection.execute(APOLLO_LEADS_SCHEMA)

    def linked_events(self):
        return self.connection.execute("""
            SELECT s.id, s.source_id, s.source_url, s.account_id, a.domain,
                   json_extract(s.details_json, '$.organizer_domain') AS organizer_domain,
                   json_extract(s.details_json, '$.identity_evidence') AS identity_evidence
            FROM source_records s JOIN accounts a ON a.id = s.account_id
            WHERE s.source = 'cvent'
        """)

    def rules(self):
        return suppression_rules(self.connection)

    def aliases(self, account_ids):
        return account_aliases(self.connection, account_ids)

    def contact_restrictions(self):
        return self.connection.execute("SELECT email, do_not_contact, status FROM people WHERE email <> ''")

    def existing_people(self):
        return self.connection.execute("SELECT id, account_id, email, linkedin_url, do_not_contact, status FROM people")

    def person_by_email(self, account_id, email):
        return self.connection.execute("SELECT id FROM people WHERE account_id = ? AND email = ?",
                                       (account_id, email)).fetchone()

    def link_exists(self, person_id, source_record_id):
        if self._has_cvent_evidence is None:
            self._has_cvent_evidence = table_exists(self.connection, "cvent_contact_evidence")
        return self._has_cvent_evidence and self.connection.execute("""
            SELECT 1 FROM cvent_contact_evidence WHERE person_id = ? AND source_record_id = ?
        """, (person_id, source_record_id)).fetchone() is not None

    def create_cvent_contact(self, candidate):
        return self.connection.execute("""
            INSERT INTO people(account_id, name, email, origin) VALUES (?, ?, ?, ?)
        """, (candidate.account_id, candidate.name, candidate.email, ORIGIN)).lastrowid

    def link_event(self, person_id, source_record_id):
        self.connection.execute("""
            INSERT INTO cvent_contact_evidence(person_id, source_record_id, observed_at) VALUES (?, ?, ?)
        """, (person_id, source_record_id, timestamp()))

    def accounts(self):
        return (dict(id=row["canonical_id"], domain=row["domain"]) for row in account_identity_rows(self.connection))

    def saved_organizers(self):
        return ((row["account_id"], json.loads(row["details_json"])) for row in self.connection.execute("""
            SELECT account_id, details_json FROM source_records WHERE source = 'cvent' AND account_id IS NOT NULL
        """))

    def mapped_leads(self):
        if not table_exists(self.connection, "cvent_apollo_leads"):
            return {}
        return {row["apollo_id"]: row for row in self.connection.execute("""
            SELECT m.*, p.account_id, p.email, p.linkedin_url, p.unlocked, p.email_verified, p.do_not_contact, p.status
            FROM cvent_apollo_leads m JOIN people p ON p.id = m.person_id
        """)}

    def create_apollo_lead(self, lead):
        person_id = self.connection.execute("""
            INSERT INTO people(account_id, name, title, email, persona, origin, unlocked, email_verified, linkedin_url)
            VALUES (:account_id, :name, :title, :email, :persona, 'apollo', :unlocked, :email_verified, :linkedin_url)
        """, lead).lastrowid
        self.connection.execute("""
            INSERT INTO cvent_apollo_leads
                (apollo_id, person_id, legacy_company_id, company_domain, discovered_at, unlocked_at, imported_at)
            VALUES (:apollo_id, :person_id, :legacy_company_id, :company_domain, :discovered_at, :unlocked_at, :imported_at)
        """, dict(lead, person_id=person_id, imported_at=timestamp()))

    def refresh_apollo_contact(self, person_id, fields):
        self.connection.execute("""
            UPDATE people SET email=:email, linkedin_url=:linkedin_url, unlocked=:unlocked,
                              email_verified=:email_verified, details_revision=details_revision+1
            WHERE id=:person_id
        """, dict(fields, person_id=person_id))

    def record_unlock_time(self, apollo_id, unlocked_at):
        self.connection.execute("UPDATE cvent_apollo_leads SET unlocked_at=? WHERE apollo_id=?", (unlocked_at, apollo_id))

    def record_summary(self, actor_id, event, counts):
        audit(self.connection, actor_id, None, event, counts)
