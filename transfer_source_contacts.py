"""Copy provenance-backed contacts as part of the one-time source transfer."""

from collections import defaultdict

from account_redirects import account_aliases, account_identity_rows
from crm_core.transfer import select_source_contacts, transfer_contact_data
from crm_store import insert_rows, table_exists
from cvent_apollo_import import SCHEMA as APOLLO_SCHEMA
from cvent_contacts import SCHEMA as EVIDENCE_SCHEMA
from workflow_store import suppression_rules


PERSON_FIELDS = (
    "name", "title", "email", "status", "do_not_contact", "persona",
    "origin", "unlocked", "email_verified", "phone", "linkedin_url",
)
APOLLO_FIELDS = ("legacy_company_id", "company_domain", "discovered_at", "unlocked_at", "imported_at")


def _apollo_links(connection):
    if not table_exists(connection, "cvent_apollo_leads"):
        return {}
    return {row["person_id"]: row for row in connection.execute("SELECT * FROM cvent_apollo_leads")}


def read_source_contacts(connection):
    """Collect contacts backed by a Cvent event or a saved Apollo mapping."""
    apollo = _apollo_links(connection)
    evidence = defaultdict(list)
    if table_exists(connection, "cvent_contact_evidence"):
        for row in connection.execute("""
            SELECT e.person_id, e.source_record_id, e.observed_at, r.account_id, r.source
            FROM cvent_contact_evidence e JOIN source_records r ON r.id = e.source_record_id
            ORDER BY e.person_id, e.source_record_id
        """):
            evidence[row["person_id"]].append(row)
    people = connection.execute("""
        SELECT p.*, a.domain AS account_domain
        FROM people p JOIN accounts a ON a.id = p.account_id ORDER BY p.id
    """).fetchall()
    rules = suppression_rules(connection)
    aliases = account_aliases(connection, {row["account_id"] for row in people})
    return select_source_contacts(people, apollo, evidence, rules, aliases)


class SqlContactTransfer:
    def __init__(self, connection):
        self.connection = connection

    def people(self):
        return self.connection.execute("SELECT * FROM people").fetchall()

    def accounts(self):
        return account_identity_rows(self.connection)

    def apollo_links(self):
        return _apollo_links(self.connection)

    def evidence_links(self):
        if not table_exists(self.connection, "cvent_contact_evidence"):
            return ()
        return self.connection.execute("SELECT person_id, source_record_id FROM cvent_contact_evidence").fetchall()

    def rules(self):
        return suppression_rules(self.connection)

    def add_person(self, account_id, person):
        fields = ", ".join(("account_id", *PERSON_FIELDS))
        placeholders = ", ".join("?" for _ in range(len(PERSON_FIELDS) + 1))
        return self.connection.execute(
            f"INSERT INTO people ({fields}) VALUES ({placeholders})",
            (account_id, *(person[field] for field in PERSON_FIELDS)),
        ).lastrowid

    def add_apollo_links(self, links):
        self.connection.execute(APOLLO_SCHEMA)
        insert_rows(self.connection, "cvent_apollo_leads", ("apollo_id", "person_id", *APOLLO_FIELDS), (
            (link["apollo_id"], person_id, *(link[field] for field in APOLLO_FIELDS)) for person_id, link in links
        ))

    def add_contact_evidence(self, links):
        self.connection.execute(EVIDENCE_SCHEMA)
        insert_rows(self.connection, "cvent_contact_evidence", ("person_id", "source_record_id", "observed_at"), links)


def transfer_contacts(connection, data, account_ids, evidence_ids, counts, *, apply):
    return transfer_contact_data(SqlContactTransfer(connection), data, account_ids, evidence_ids, counts, apply=apply)
