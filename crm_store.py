"""Local accounts and the saved source records that support them."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


DEFAULT_DB = Path(__file__).resolve().parent / "instance" / "demo.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    identity_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    organization_type TEXT NOT NULL,
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
    path = Path(database_path).resolve()
    database = f"{path.as_uri()}?mode=ro" if read_only else path
    connection = sqlite3.connect(database, uri=read_only)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(database_path):
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(database_path)) as connection:
        connection.executescript(SCHEMA)


def account_summaries(connection):
    rows = connection.execute("""
        SELECT a.*,
               COUNT(CASE WHEN r.source = 'cvent' THEN 1 END) AS event_count,
               COUNT(CASE WHEN r.source = 'directory' THEN 1 END) AS listing_count
        FROM accounts a LEFT JOIN source_records r ON a.id = r.account_id
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


def import_snapshot(database_path, accounts, records):
    initialize(database_path)
    with closing(connect(database_path)) as connection, connection:
        # Reserve the writer so a concurrent replay sees the first import's identities.
        connection.execute("BEGIN IMMEDIATE")
        for identity, account in accounts.items():
            connection.execute("""
                INSERT INTO accounts (identity_key, name, domain, organization_type)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (identity_key) DO NOTHING
            """, (identity, account["name"], account["domain"], account["organization_type"]))
        account_ids = {
            row["identity_key"]: row["id"]
            for row in connection.execute("SELECT id, identity_key FROM accounts")
        }

        changes = 0
        seen = set()
        for record in records:
            key = (record["source"], record["source_id"])
            if key in seen:
                raise ValueError(f"Duplicate input source record: {key}")
            seen.add(key)
            identity = record["identity_key"]
            account_id = account_ids[identity] if identity is not None else None
            current = connection.execute("""
                SELECT account_id FROM source_records WHERE source = ? AND source_id = ?
            """, key).fetchone()
            if current is not None and current["account_id"] != account_id:
                raise ValueError(f"Review required before changing the account link for {key}")

            result = connection.execute("""
                INSERT INTO source_records (
                    source, source_id, source_url, source_file, account_id, review_reason,
                    details_json, raw_json
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
            """, (*key, record["source_url"], record["source_file"], account_id, record["reason"],
                  json.dumps(record["details"], sort_keys=True, ensure_ascii=False),
                  json.dumps(record["raw"], sort_keys=True, ensure_ascii=False)))
            changes += result.rowcount

        counts = connection.execute("""
            SELECT COUNT(*) AS source_records,
                   COUNT(CASE WHEN account_id IS NOT NULL THEN 1 END) AS linked_records,
                   COUNT(CASE WHEN account_id IS NULL THEN 1 END) AS staged_records
            FROM source_records
        """).fetchone()
        return dict(counts, changed_records=changes, accounts=account_summaries(connection))

