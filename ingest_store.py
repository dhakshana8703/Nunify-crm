"""SQL persistence for the source promotion use case."""

import hashlib
import json

from account_redirects import account_identity_rows
from crm_core.suppression import matches_organization_domain, matching_rule
from organization_identity import root_domain
from suppression_projection import refresh_account_suppression
from workflow_store import is_blocked, suppression_rules, timestamp


class SourceStore:
    def __init__(self, connection):
        self.connection = connection

    def account_candidates(self, root):
        return account_identity_rows(self.connection, root)

    def directory_accounts(self):
        rows = self.connection.execute("""
            SELECT DISTINCT a.id, a.name, a.domain FROM accounts a
            JOIN source_records r ON r.account_id = a.id AND r.source = 'directory'
        """).fetchall()
        return [dict(row) for row in rows]

    def saved_record(self, source, source_id):
        row = self.connection.execute("""
            SELECT r.account_id, r.review_reason, r.details_json, a.domain FROM source_records r
            LEFT JOIN accounts a ON a.id = r.account_id WHERE r.source = ? AND r.source_id = ?
        """, (source, source_id)).fetchone()
        if row is None:
            return None
        return dict(account_id=row["account_id"], review_reason=row["review_reason"],
                    details=json.loads(row["details_json"]), domain=row["domain"])

    def imported_record(self, source, source_id):
        row = self.connection.execute("""
            SELECT e.account_id, e.payload_json AS raw, a.domain FROM imported_source_evidence e
            JOIN accounts a ON a.id = e.account_id WHERE e.source_slug = ? AND e.external_id = ?
        """, (source, source_id)).fetchone()
        return dict(row) if row is not None else None

    def organization_blocked(self, domain):
        return any(rule["kind"] == "domain" and matches_organization_domain(domain, rule["value"])
                   for rule in suppression_rules(self.connection))

    def account_blocked(self, account_id):
        return is_blocked(self.connection, account_id)

    def address_blocked(self, address):
        rules = (rule for rule in suppression_rules(self.connection) if rule["kind"] != "account")
        return matching_rule(rules, None, None, (address,)) is not None

    def create_account(self, name, domain, organization_type):
        root = root_domain(domain)
        if not root or account_identity_rows(self.connection, root):
            return None
        identity = "ingested:" + hashlib.sha256(root.encode()).hexdigest()
        result = self.connection.execute("""
            INSERT INTO accounts (identity_key, name, domain, organization_type, root_domain)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
        """, (identity, name, domain, organization_type, root))
        if result.rowcount:
            refresh_account_suppression(self.connection, (result.lastrowid,))
            return result.lastrowid
        return None

    def save_record(self, source, row, account_id, details):
        changed = self.connection.execute("""
            INSERT INTO source_records
              (source, source_id, source_url, source_file, account_id, review_reason, details_json, raw_json)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT (source, source_id) DO UPDATE SET
                source_url = excluded.source_url, source_file = excluded.source_file,
                details_json = excluded.details_json, raw_json = excluded.raw_json
            WHERE source_records.source_url <> excluded.source_url
               OR source_records.source_file <> excluded.source_file
               OR source_records.details_json <> excluded.details_json
               OR source_records.raw_json <> excluded.raw_json
        """, (source, row["source_id"], row["source_url"], row["source_file"], account_id,
              json.dumps(details, sort_keys=True, ensure_ascii=False),
              json.dumps(row["raw"], sort_keys=True, ensure_ascii=False)))
        return bool(changed.rowcount)

    def save_imported_record(self, source, source_id, source_url, account_id, raw):
        self.connection.execute("""
            INSERT INTO imported_source_evidence (source_slug, external_id, account_id, source_url, observed_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_slug, external_id) DO UPDATE SET
                source_url = excluded.source_url, observed_at = excluded.observed_at, payload_json = excluded.payload_json
        """, (source, source_id, account_id, source_url, timestamp(), raw))

    def save_contact(self, account_id, contact):
        self.connection.execute("""
            INSERT INTO people (account_id, name, title, email, origin)
            VALUES (?, ?, ?, ?, 'source') ON CONFLICT DO NOTHING
        """, (account_id, *contact))


def account_sources(connection, account_ids=None):
    """Return the registered sources with linked evidence for each account."""
    account_filter = ""
    parameters = ()
    if account_ids is not None:
        parameters = tuple(account_ids)
        if not parameters:
            return {}
        placeholders = ", ".join("?" for _ in parameters)
        account_filter = f"WHERE evidence.account_id IN ({placeholders})"
    rows = connection.execute(f"""
        WITH linked_sources AS (
            SELECT account_id, source AS slug FROM source_records WHERE account_id IS NOT NULL
            UNION
            SELECT account_id, source_slug FROM imported_source_evidence
        )
        SELECT evidence.account_id, source.slug, source.name
        FROM linked_sources evidence
        JOIN ingest_sources source ON source.slug = evidence.slug
        {account_filter}
        ORDER BY evidence.account_id, source.name COLLATE NOCASE, source.slug
    """, parameters)
    sources = {}
    for row in rows:
        sources.setdefault(row["account_id"], []).append(row)
    return sources


def evidence_for_account(connection, account_id):
    rows = connection.execute("""
        SELECT e.*, s.name AS source_name FROM imported_source_evidence e
        JOIN ingest_sources s ON s.slug = e.source_slug WHERE e.account_id = ?
        ORDER BY e.observed_at DESC, e.id DESC
    """, (account_id,)).fetchall()
    return [dict(row, raw=json.loads(row["payload_json"])) for row in rows]
