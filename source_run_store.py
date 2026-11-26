"""SQL persistence and read projections for source registrations and staged runs."""

import json

from workflow_store import audit, timestamp, transaction


SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_sources (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    adapter TEXT NOT NULL CHECK (adapter IN ('cvent', 'directory', 'upload', 'external')),
    schedule_days INTEGER CHECK (schedule_days BETWEEN 1 AND 365),
    next_run_at TEXT,
    key_hash TEXT
);
CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY,
    source_slug TEXT NOT NULL REFERENCES ingest_sources(slug),
    input_name TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'scheduled', 'upload', 'api')),
    state TEXT NOT NULL CHECK (state IN ('ready', 'queued', 'running', 'complete', 'partial', 'failed')),
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    retry_after TEXT,
    error TEXT,
    request_key TEXT,
    request_hash TEXT,
    UNIQUE (source_slug, request_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS source_run_one_active ON source_runs(source_slug)
    WHERE state IN ('queued', 'running');
CREATE TABLE IF NOT EXISTS source_run_rows (
    run_id INTEGER NOT NULL REFERENCES source_runs(id),
    row_number INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT 'staged'
        CHECK (result IN ('staged', 'imported', 'unchanged', 'review', 'suppressed', 'invalid')),
    detail TEXT,
    PRIMARY KEY (run_id, row_number)
);
CREATE TABLE IF NOT EXISTS imported_source_evidence (
    id INTEGER PRIMARY KEY,
    source_slug TEXT NOT NULL REFERENCES ingest_sources(slug),
    external_id TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    source_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (source_slug, external_id)
);
CREATE INDEX IF NOT EXISTS imported_source_account ON imported_source_evidence(account_id);
CREATE TABLE IF NOT EXISTS saved_export_runs (
    run_id INTEGER PRIMARY KEY REFERENCES source_runs(id),
    policy_json TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'preview' CHECK (phase IN ('preview', 'promote')),
    last_row INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS source_schedules (
    source_slug TEXT PRIMARY KEY REFERENCES ingest_sources(slug),
    cadence TEXT NOT NULL CHECK (cadence IN ('daily', 'weekly', 'monthly')),
    timezone TEXT NOT NULL,
    local_time TEXT NOT NULL,
    schedule_day INTEGER
);
CREATE TABLE IF NOT EXISTS source_promotion_policies (
    source_slug TEXT PRIMARY KEY REFERENCES ingest_sources(slug),
    policy_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_health_events (
    id INTEGER PRIMARY KEY,
    source_slug TEXT NOT NULL REFERENCES ingest_sources(slug),
    run_id INTEGER REFERENCES source_runs(id),
    kind TEXT NOT NULL CHECK (kind IN ('skipped', 'failed', 'low_yield')),
    message TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
INSERT OR IGNORE INTO ingest_sources (slug, name, adapter) VALUES
    ('cvent', 'Cvent', 'cvent'),
    ('directory', 'Directory of Associations', 'directory'),
    ('upload', 'File uploads', 'upload');
"""


def initialize(connection):
    from source_review_store import initialize as initialize_review

    if getattr(connection, "dialect", "sqlite") == "sqlite":
        with transaction(connection):
            if connection.execute("""
                SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'directory_import_runs'
            """).fetchone():
                connection.execute("ALTER TABLE directory_import_runs RENAME TO saved_export_runs")
    connection.executescript(SCHEMA)
    with transaction(connection):
        columns = {row[1] for row in connection.execute("PRAGMA table_info(source_runs)")}
        if "retry_count" not in columns:
            connection.execute("ALTER TABLE source_runs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
        if "retry_after" not in columns:
            connection.execute("ALTER TABLE source_runs ADD COLUMN retry_after TEXT")
    initialize_review(connection)



def _compact(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _stage(connection, run_id, rows):
    connection.executemany("""
        INSERT INTO source_run_rows (run_id, row_number, payload_json) VALUES (?, ?, ?)
    """, ((run_id, row_number, _compact(data)) for row_number, data in rows))


def _new_run(connection, slug, input_name, trigger, state="queued", *, rows=(), key=None, digest=None):
    run_id = connection.execute("""
        INSERT INTO source_runs (source_slug, input_name, trigger, state, queued_at, request_key, request_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (slug, input_name, trigger, state, timestamp(), key, digest)).lastrowid
    _stage(connection, run_id, rows)
    return run_id


def _active(connection, slug):
    return connection.execute("""
        SELECT id FROM source_runs WHERE source_slug = ? AND state IN ('queued', 'running')
    """, (slug,)).fetchone()


def _stage_saved(connection, source, file_name, policy, trigger):
    run_id = _new_run(connection, source, file_name, trigger)
    connection.execute("INSERT INTO saved_export_runs(run_id, policy_json) VALUES (?, ?)", (run_id, _compact(policy)))
    return run_id


def _new_saved_export(connection, source, connector, policy, trigger):
    return _stage_saved(connection, source, connector.adapter.FILE_NAME, policy, trigger)


def saved_source_policy(connection, source, default_policy):
    row = connection.execute("SELECT policy_json FROM source_promotion_policies WHERE source_slug = ?", (source,)).fetchone()
    return json.loads(row["policy_json"]) if row else default_policy


def _saved_source_policy(connection, source, connector):
    return saved_source_policy(connection, source, connector.adapter.DEFAULT_POLICY)


def _page(total, size, page):
    pages = max(1, (total + size - 1) // size)
    return {"size": size, "page": min(pages, max(1, page)), "pages": pages}


class SqlSourceRuns:
    def __init__(self, connection):
        self.connection = connection

    def active(self, source):
        return _active(self.connection, source)

    def stage(self, source, input_name, trigger, state="queued", **options):
        return _new_run(self.connection, source, input_name, trigger, state, **options)

    def stage_saved(self, source, file_name, policy, trigger):
        return _stage_saved(self.connection, source, file_name, policy, trigger)

    def save_policy(self, source, policy):
        self.connection.execute("""
            INSERT INTO source_promotion_policies(source_slug, policy_json)
            VALUES (?, ?) ON CONFLICT (source_slug) DO UPDATE SET policy_json = excluded.policy_json
        """, (source, _compact(policy)))

    def queue_again(self, run_id, promote, observed):
        if promote:
            self.connection.execute("""
                UPDATE saved_export_runs SET phase = 'promote', last_row = 0 WHERE run_id = ?
            """, (run_id,))
        self.connection.execute("""
            UPDATE source_runs SET state = 'queued', error = NULL, queued_at = ?,
                retry_count = 0, retry_after = NULL WHERE id = ?
        """, (timestamp(observed), run_id))

    def schedule(self, source, calendar, days, due_at):
        if calendar:
            self.connection.execute("""
                INSERT INTO source_schedules(source_slug, cadence, timezone, local_time, schedule_day)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT (source_slug) DO UPDATE SET
                    cadence = excluded.cadence, timezone = excluded.timezone, local_time = excluded.local_time,
                    schedule_day = excluded.schedule_day
            """, (source, calendar["cadence"], calendar["timezone"], calendar["local_time"], calendar["schedule_day"]))
        elif days:
            self.connection.execute("DELETE FROM source_schedules WHERE source_slug = ?", (source,))
        scheduled = timestamp(due_at) if due_at else None
        self.connection.execute("UPDATE ingest_sources SET schedule_days = ?, next_run_at = ? WHERE slug = ?",
                                (days, scheduled, source))
        return scheduled

    def connector_kind(self, source):
        row = self.connection.execute("SELECT adapter FROM ingest_sources WHERE slug = ?", (source,)).fetchone()
        return row["adapter"] if row else None

    def save_key(self, source, name, digest):
        self.connection.execute("""
            INSERT INTO ingest_sources (slug, name, adapter, key_hash) VALUES (?, ?, 'external', ?)
            ON CONFLICT (slug) DO UPDATE SET name = excluded.name, key_hash = excluded.key_hash
        """, (source, name, digest))

    def remove_key(self, source):
        return bool(self.connection.execute("""
            UPDATE ingest_sources SET key_hash = NULL WHERE slug = ? AND adapter = 'external' AND key_hash IS NOT NULL
        """, (source,)).rowcount)

    def key_digest(self, source):
        row = self.connection.execute("""
            SELECT key_hash FROM ingest_sources WHERE slug = ? AND adapter = 'external'
        """, (source,)).fetchone()
        return row["key_hash"] if row else None

    def key_is_current(self, source, digest):
        return self.key_digest(source) == digest

    def prior_request(self, source, key):
        return self.connection.execute("""
            SELECT id, state, request_hash FROM source_runs WHERE source_slug = ? AND request_key = ?
        """, (source, key)).fetchone()

    def record(self, actor_id, action, details):
        audit(self.connection, actor_id, None, action, details)

    def source_list(self):
        return self.connection.execute("""
            SELECT s.slug, s.name, s.adapter, s.schedule_days, s.next_run_at, s.key_hash IS NOT NULL AS key_active,
                   cal.cadence, cal.timezone, cal.local_time, cal.schedule_day
            FROM ingest_sources s LEFT JOIN source_schedules cal ON cal.source_slug = s.slug ORDER BY s.name
        """).fetchall()

    def run(self, run_id):
        return self.connection.execute("""
            SELECT r.*, s.name AS source_name, s.adapter FROM source_runs r
            JOIN ingest_sources s ON s.slug = r.source_slug WHERE r.id = ?
        """, (run_id,)).fetchone()

    def run_list(self, *, search, source, state, oldest, size, page):
        clauses = ["instr(lower(s.name || ' ' || r.input_name), lower(?)) > 0"]
        params = [search]
        if source:
            clauses.append("s.slug = ?")
            params.append(source)
        if state:
            clauses.append("r.state = ?")
            params.append(state)
        query = " FROM source_runs r JOIN ingest_sources s ON s.slug = r.source_slug WHERE " + " AND ".join(clauses)
        total = self.connection.execute("SELECT count(*)" + query, params).fetchone()[0]
        pagination = _page(total, size, page)
        order = "ASC" if oldest else "DESC"
        runs = self.connection.execute("SELECT r.*, s.name AS source_name" + query + f" ORDER BY r.id {order} LIMIT ? OFFSET ?",
                                       (*params, size, (pagination["page"] - 1) * size)).fetchall()
        return runs, pagination

    def health_events(self):
        return self.connection.execute("""
            SELECT e.*, s.name FROM source_health_events e JOIN ingest_sources s ON s.slug = e.source_slug
            ORDER BY e.id DESC LIMIT 10
        """).fetchall()

    def row_counts(self, run_id):
        return dict(self.connection.execute("""
            SELECT result, count(*) FROM source_run_rows WHERE run_id = ? GROUP BY result
        """, (run_id,)).fetchall())

    def rows(self, run_id, *, result, search, newest, size, page):
        where = """
            WHERE run_id = ? AND (? = '' OR result = ?) AND (
                ? = '' OR instr(lower(COALESCE(json_extract(payload_json, '$.raw.name'),
                json_extract(payload_json, '$.raw.event_name'), json_extract(payload_json, '$.name'), '')), lower(?)) > 0
            )
        """
        params = (run_id, result, result, search, search)
        total = self.connection.execute("SELECT COUNT(*) FROM source_run_rows" + where, params).fetchone()[0]
        pagination = _page(total, size, page)
        order = "DESC" if newest else "ASC"
        rows = self.connection.execute("SELECT * FROM source_run_rows" + where + f" ORDER BY row_number {order} LIMIT ? OFFSET ?",
                                       (*params, size, (pagination["page"] - 1) * size)).fetchall()
        return rows, pagination
