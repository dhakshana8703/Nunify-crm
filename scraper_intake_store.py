"""SQL persistence and request event history for external source intake."""

from crm_core.source_intake import compact
from source_run_store import _active, _new_run
from workflow_store import audit, timestamp


SCHEMA = """
CREATE TABLE IF NOT EXISTS scraper_promotion_settings (
    source_slug TEXT PRIMARY KEY REFERENCES ingest_sources(slug),
    promotion TEXT NOT NULL CHECK (promotion IN ('preview', 'automatic'))
);
CREATE TABLE IF NOT EXISTS scraper_intake_runs (
    id INTEGER PRIMARY KEY,
    source_slug TEXT NOT NULL REFERENCES ingest_sources(slug),
    request_key TEXT NOT NULL,
    input_name TEXT NOT NULL,
    promotion TEXT NOT NULL CHECK (promotion IN ('preview', 'automatic')),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    source_run_id INTEGER UNIQUE REFERENCES source_runs(id),
    UNIQUE (source_slug, request_key)
);
CREATE TABLE IF NOT EXISTS scraper_intake_batches (
    run_id INTEGER NOT NULL REFERENCES scraper_intake_runs(id),
    batch_number INTEGER NOT NULL,
    request_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    PRIMARY KEY (run_id, batch_number),
    UNIQUE (run_id, request_key)
);
CREATE TABLE IF NOT EXISTS scraper_intake_records (
    run_id INTEGER NOT NULL REFERENCES scraper_intake_runs(id),
    identity TEXT NOT NULL,
    site_identity TEXT NOT NULL,
    batch_number INTEGER NOT NULL,
    record_number INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, identity)
);
CREATE INDEX IF NOT EXISTS scraper_identity_by_site ON scraper_intake_records(run_id, site_identity);
CREATE TABLE IF NOT EXISTS scraper_api_requests (
    id INTEGER PRIMARY KEY,
    source_slug TEXT NOT NULL,
    operation TEXT NOT NULL,
    credential_hash TEXT,
    occurred_at TEXT NOT NULL,
    status_code INTEGER
);
CREATE INDEX IF NOT EXISTS scraper_api_rate ON scraper_api_requests(credential_hash, occurred_at);
"""


def initialize(connection):
    connection.executescript(SCHEMA)


class ScraperIntakeStore:
    def __init__(self, connection):
        self.connection = connection

    def source_key(self, slug):
        return self.connection.execute("""
            SELECT key_hash FROM ingest_sources WHERE slug = ? AND adapter = 'external'
        """, (slug,)).fetchone()

    def key_is_current(self, slug, credential_hash):
        return self.connection.execute("""
            SELECT 1 FROM ingest_sources WHERE slug = ? AND key_hash = ?
        """, (slug, credential_hash)).fetchone() is not None

    def recent_requests(self, credential_hash, cutoff):
        return self.connection.execute("""
            SELECT COUNT(*) FROM scraper_api_requests WHERE credential_hash = ? AND occurred_at > ?
                AND status_code IS NOT 429
        """, (credential_hash, timestamp(cutoff))).fetchone()[0]

    def log_request(self, source, operation, credential_hash, observed, status):
        return self.connection.execute("""
            INSERT INTO scraper_api_requests(source_slug, operation, credential_hash, occurred_at, status_code)
            VALUES (?, ?, ?, ?, ?)
        """, (source, operation, credential_hash, timestamp(observed), status)).lastrowid

    def log_response(self, request_id, status):
        self.connection.execute("UPDATE scraper_api_requests SET status_code = ? WHERE id = ?",
                                (status, request_id))

    def promotion(self, slug):
        row = self.connection.execute("""
            SELECT promotion FROM scraper_promotion_settings WHERE source_slug = ?
        """, (slug,)).fetchone()
        return row["promotion"] if row else "preview"

    def store_promotion(self, slug, promotion, actor_id):
        self.connection.execute("""
            INSERT INTO scraper_promotion_settings(source_slug, promotion) VALUES (?, ?)
            ON CONFLICT(source_slug) DO UPDATE SET promotion = excluded.promotion
        """, (slug, promotion))
        audit(self.connection, actor_id, None, "scraper_promotion_changed", {"source": slug, "promotion": promotion})

    def run(self, slug, run_id):
        return self.connection.execute("""
            SELECT * FROM scraper_intake_runs WHERE id = ? AND source_slug = ?
        """, (run_id, slug)).fetchone()

    def prior_run(self, slug, key):
        return self.connection.execute("""
            SELECT * FROM scraper_intake_runs WHERE source_slug = ? AND request_key = ?
        """, (slug, key)).fetchone()

    def create_run(self, slug, key, name, promotion, observed):
        return self.connection.execute("""
            INSERT INTO scraper_intake_runs(source_slug, request_key, input_name, promotion, opened_at)
            VALUES (?, ?, ?, ?, ?)
        """, (slug, key, name, promotion, timestamp(observed))).lastrowid

    def batch_counts(self, run_id):
        return self.connection.execute("""
            SELECT COUNT(*) AS count, COALESCE(MAX(batch_number), 0) AS latest
            FROM scraper_intake_batches WHERE run_id = ?
        """, (run_id,)).fetchone()

    def staged_count(self, run_id):
        return self.connection.execute("""
            SELECT COUNT(*) FROM scraper_intake_records WHERE run_id = ?
        """, (run_id,)).fetchone()[0]

    def source_state(self, source_run_id):
        return self.connection.execute("SELECT state FROM source_runs WHERE id = ?", (source_run_id,)).fetchone()

    def record(self, run_id, identity):
        return self.connection.execute("""
            SELECT * FROM scraper_intake_records WHERE run_id = ? AND identity = ?
        """, (run_id, identity)).fetchone()

    def records_for_site(self, run_id, site_identity):
        return self.connection.execute("""
            SELECT * FROM scraper_intake_records WHERE run_id = ? AND site_identity = ?
            ORDER BY identity LIMIT 2
        """, (run_id, site_identity)).fetchall()

    def set_record_identity(self, run_id, prior_identity, identity, record):
        self.connection.execute("""
            UPDATE scraper_intake_records SET identity = ?, payload_json = ?
            WHERE run_id = ? AND identity = ?
        """, (identity, compact(record), run_id, prior_identity))

    def write_record(self, run_id, prior_identity, identity, site_identity, batch_number, record_number, record):
        if prior_identity is not None:
            self.connection.execute("""
                UPDATE scraper_intake_records SET identity = ?, site_identity = ?, batch_number = ?,
                    record_number = ?, payload_json = ? WHERE run_id = ? AND identity = ?
            """, (identity, site_identity, batch_number, record_number, compact(record), run_id, prior_identity))
        else:
            self.connection.execute("""
                INSERT INTO scraper_intake_records(run_id, identity, site_identity, batch_number, record_number, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (run_id, identity, site_identity, batch_number, record_number, compact(record)))

    def prior_batch(self, run_id, key):
        return self.connection.execute("""
            SELECT * FROM scraper_intake_batches WHERE run_id = ? AND request_key = ?
        """, (run_id, key)).fetchone()

    def next_batch_number(self, run_id):
        return self.connection.execute("""
            SELECT COALESCE(MAX(batch_number), 0) + 1 FROM scraper_intake_batches WHERE run_id = ?
        """, (run_id,)).fetchone()[0]

    def occupied_batch(self, run_id, batch_number):
        return self.connection.execute("""
            SELECT 1 FROM scraper_intake_batches WHERE run_id = ? AND batch_number = ?
        """, (run_id, batch_number)).fetchone() is not None

    def store_receipt(self, run_id, batch_number, key, digest, receipt):
        self.connection.execute("""
            INSERT INTO scraper_intake_batches(run_id, batch_number, request_key, request_hash, receipt_json)
            VALUES (?, ?, ?, ?, ?)
        """, (run_id, batch_number, key, digest, compact(receipt)))

    def active_source_run(self, slug):
        return _active(self.connection, slug)

    def finish(self, run, state, observed):
        run_id = _new_run(self.connection, run["source_slug"], run["input_name"], "api", state)
        self.connection.execute("""
            INSERT INTO source_run_rows(run_id, row_number, payload_json)
            SELECT ?, ROW_NUMBER() OVER (ORDER BY batch_number, record_number, identity), payload_json
            FROM scraper_intake_records WHERE run_id = ?
        """, (run_id, run["id"]))
        self.connection.execute("""
            UPDATE scraper_intake_runs SET closed_at = ?, source_run_id = ? WHERE id = ?
        """, (timestamp(observed), run_id, run["id"]))
        return run_id
