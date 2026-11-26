"""Local accounts and the saved source records that support them."""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from account_facts_store import refresh_account_facts
from account_identity_schema import initialize_account_identity
from account_redirects import account_identity_rows
from crm_core.account_identity import corroborated_account
from crm_core.account_profile import organization_type_or_unknown
from organization_identity import root_domain
from suppression_projection import account_projection_available, refresh_account_suppression


DEFAULT_DB = Path(__file__).resolve().parent / "instance" / "demo.sqlite3"


def is_postgres(database):
    return isinstance(database, str) and database.startswith(("postgresql://", "postgres://"))


def configured_database():
    database = os.environ.get("DATABASE_URL")
    if database and not is_postgres(database):
        raise ValueError("DATABASE_URL must be a PostgreSQL connection URL.")
    return database or DEFAULT_DB


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    identity_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    organization_type TEXT NOT NULL DEFAULT 'Unknown',
    owner TEXT
);

CREATE TABLE IF NOT EXISTS source_records (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK (source IN ('cvent', 'directory')),
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_file TEXT NOT NULL,
    account_id INTEGER REFERENCES accounts(id),
    review_reason TEXT,
    details_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE (source, source_id),
    CHECK ((account_id IS NULL) = (review_reason IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS source_records_account ON source_records(account_id, source);

CREATE VIEW IF NOT EXISTS cvent_read AS
SELECT r.id, r.source_id, r.source_url AS cvent_url, r.source_url,
       r.details_json ->> 'event_url' AS event_url,
       r.details_json ->> 'event_name' AS event_name,
       r.details_json ->> 'start_date' AS start_date,
       r.details_json ->> 'start_date_iso' AS start_date_iso,
       r.details_json ->> 'end_date' AS end_date,
       r.details_json ->> 'venue_name' AS venue_name,
       r.details_json ->> 'city' AS city,
       r.details_json ->> 'state' AS state,
       r.details_json ->> 'country' AS country,
       r.details_json ->> 'organizer_company' AS organizer_company,
       r.details_json ->> 'client_domain' AS client_domain,
       r.details_json ->> 'evidence_keys' AS evidence_keys,
       r.details_json ->> 'status' AS status,
       r.details_json ->> 'scrape_date' AS scrape_date,
       r.account_id AS company_id,
       a.name AS company_name, a.domain AS company_domain, '' AS company_website,
       '' AS creator_email, 0 AS people_count
FROM source_records r JOIN accounts a ON r.account_id = a.id
WHERE r.source = 'cvent';
"""


def connect(database_path, *, read_only=False):
    if is_postgres(database_path):
        from postgres_store import PostgresConnection

        return PostgresConnection(database_path, read_only=read_only)
    path = Path(database_path).resolve()
    database = f"{path.as_uri()}?mode=ro" if read_only else path
    connection = sqlite3.connect(database, uri=read_only, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(database_path):
    postgres = is_postgres(database_path)
    if not postgres:
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(database_path)) as connection:
        if postgres:
            connection.execute("CREATE EXTENSION IF NOT EXISTS citext")
        else:
            connection.execute("PRAGMA journal_mode = WAL").fetchone()
        connection.executescript(SCHEMA)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            initialize_account_identity(connection)


def has_accounts(connection):
    return connection.execute("""
        SELECT a.id FROM accounts a WHERE NOT EXISTS (SELECT 1 FROM account_merges WHERE from_id = a.id) LIMIT 1
    """).fetchone() is not None


def unlinked_source_count(connection):
    return connection.execute("SELECT COUNT(*) FROM source_records WHERE account_id IS NULL").fetchone()[0]


def table_exists(connection, name):
    if getattr(connection, "dialect", "sqlite") == "postgresql":
        sql = """SELECT 1 FROM information_schema.tables
                 WHERE table_schema = current_schema() AND table_name = ?"""
    else:
        sql = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
    return connection.execute(sql, (name,)).fetchone() is not None


def insert_rows(connection, table, fields, rows):
    """Batch inserts for trusted internal identifiers, with all row values bound."""
    fields = tuple(fields)
    rows = list(rows)
    if not fields or not all(name.isidentifier() for name in (table, *fields)):
        raise ValueError("Invalid bulk insert identifiers.")
    placeholders = "(" + ", ".join("?" for _ in fields) + ")"
    columns = ", ".join(fields)
    for start in range(0, len(rows), 100):
        batch = rows[start:start + 100]
        values = ", ".join(placeholders for _ in batch)
        connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES {values}",
            tuple(value for row in batch for value in row),
        )


def account_summaries(connection):
    rows = connection.execute("""
        SELECT a.*,
               COUNT(CASE WHEN r.source = 'cvent' THEN 1 END) AS event_count,
               COUNT(CASE WHEN r.source = 'directory' THEN 1 END) AS listing_count
        FROM accounts a LEFT JOIN source_records r ON a.id = r.account_id
        WHERE NOT EXISTS (SELECT 1 FROM account_merges WHERE from_id = a.id)
        GROUP BY a.id
        ORDER BY a.id
    """)
    return [dict(row) for row in rows]


def record_evidence(connection, source, source_id):
    row = connection.execute("""
        SELECT source_id, source_url, source_file, account_id, review_reason,
               details_json, raw_json
        FROM source_records WHERE source = ? AND source_id = ?
    """, (source, source_id)).fetchone()
    if row is None:
        return None
    return {
        "source": source,
        "source_id": row["source_id"],
        "source_url": row["source_url"],
        "source_file": row["source_file"],
        "account_id": row["account_id"],
        "review_reason": row["review_reason"],
        "details": json.loads(row["details_json"]),
        "raw": json.loads(row["raw_json"]),
    }


def directory_listings(connection, account_id):
    rows = connection.execute("""
        SELECT details_json, review_reason FROM source_records
        WHERE source = 'directory' AND account_id IS ?
        ORDER BY source_id
    """, (account_id,))
    return [dict(json.loads(row["details_json"]), reason=row["review_reason"]) for row in rows]


class SqlSeedSnapshot:
    def __init__(self, connection):
        self.connection = connection
        self.created_accounts = []
        self.changed_accounts = set()
        self.requested_account_ids = {}

    def ensure_account(self, identity, account):
        root = root_domain(account["domain"])
        if not root:
            raise ValueError(f"Choose a valid organization domain for {identity}.")
        candidates = account_identity_rows(self.connection, root)
        linked = next((candidate for candidate in candidates if candidate["identity_key"] == identity), None)
        if linked is None and self.connection.execute(
            "SELECT id FROM accounts WHERE identity_key = ?", (identity,),
        ).fetchone() is not None:
            raise ValueError(f"Review required before changing the account domain for {identity}.")
        if candidates:
            match = corroborated_account(candidates, account["name"], identity_key=identity)
            if match is None:
                raise ValueError(f"Review the organization identity before linking {identity}.")
            self.requested_account_ids[identity] = match["canonical_id"]
            return
        result = self.connection.execute("""
            INSERT INTO accounts (identity_key, name, domain, organization_type, root_domain)
            VALUES (?, ?, ?, ?, ?)
        """, (identity, account["name"], account["domain"],
              organization_type_or_unknown(account.get("organization_type")), root))
        self.created_accounts.append(result.lastrowid)

    def account_ids(self):
        ids = {row["identity_key"]: row["canonical_id"] for row in account_identity_rows(self.connection)}
        return ids | self.requested_account_ids

    def record_link(self, source, source_id):
        return self.connection.execute("""
            SELECT account_id FROM source_records WHERE source = ? AND source_id = ?
        """, (source, source_id)).fetchone()

    def write_record(self, record, account_id):
        result = self.connection.execute("""
            INSERT INTO source_records (
                source, source_id, source_url, source_file, account_id, review_reason, details_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, source_id) DO UPDATE SET
                source_url = excluded.source_url,
                source_file = excluded.source_file,
                review_reason = excluded.review_reason,
                details_json = excluded.details_json,
                raw_json = excluded.raw_json
            WHERE source_records.source_url IS NOT excluded.source_url
               OR source_records.source_file IS NOT excluded.source_file
               OR source_records.review_reason IS NOT excluded.review_reason
               OR source_records.details_json IS NOT excluded.details_json
               OR source_records.raw_json IS NOT excluded.raw_json
        """, (record["source"], record["source_id"], record["source_url"], record["source_file"], account_id,
              record["reason"], json.dumps(record["details"], sort_keys=True, ensure_ascii=False),
              json.dumps(record["raw"], sort_keys=True, ensure_ascii=False)))
        if result.rowcount and account_id is not None:
            self.changed_accounts.add(account_id)
        return result.rowcount


def import_snapshot(database_path, accounts, records):
    from crm_core.transfer import seed_snapshot

    initialize(database_path)
    with closing(connect(database_path)) as connection, connection:
        # Reserve the writer so a concurrent replay sees the first import's identities.
        connection.execute("BEGIN IMMEDIATE")
        store = SqlSeedSnapshot(connection)
        changes = seed_snapshot(store, accounts, records)
        if store.created_accounts and account_projection_available(connection):
            refresh_account_suppression(connection, store.created_accounts)
        if store.changed_accounts or store.created_accounts:
            refresh_account_facts(connection, store.changed_accounts.union(store.created_accounts))

        counts = connection.execute("""
            SELECT COUNT(*) AS source_records,
                   COUNT(CASE WHEN account_id IS NOT NULL THEN 1 END) AS linked_records,
                   COUNT(CASE WHEN account_id IS NULL THEN 1 END) AS staged_records
            FROM source_records
        """).fetchone()
        return dict(counts, changed_records=changes, accounts=account_summaries(connection))

