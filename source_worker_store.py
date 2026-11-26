"""Persist source-worker leases, row results, and progress in SQL."""

from ingest_store import SourceStore
from source_review_store import replay_result
from source_run_store import _active, _stage, _stage_saved, saved_source_policy
from workflow_store import timestamp, transaction


def _available(slugs):
    placeholders = ", ".join("?" for _ in slugs) or "NULL"
    return f"(s.adapter IN ('upload', 'external') OR s.slug IN ({placeholders}))"


class SourceWorkerStore:
    def __init__(self, connection):
        self.connection = connection
        self.intake = SourceStore(connection)

    def transaction(self):
        return transaction(self.connection)

    def expired_runs(self, slugs, observed):
        return self.connection.execute(f"""
            SELECT r.* FROM source_runs r JOIN ingest_sources s ON s.slug = r.source_slug
            WHERE r.state = 'running' AND r.lease_until <= ? AND {_available(slugs)}
        """, (timestamp(observed), *slugs)).fetchall()

    def due_sources(self, observed):
        return self.connection.execute("""
            SELECT s.*, cal.cadence, cal.timezone, cal.local_time, cal.schedule_day
            FROM ingest_sources s LEFT JOIN source_schedules cal ON cal.source_slug = s.slug
            WHERE s.next_run_at <= ? AND (s.schedule_days IS NOT NULL OR cal.cadence IS NOT NULL)
        """, (timestamp(observed),)).fetchall()

    def active_run(self, slug):
        return _active(self.connection, slug)

    def saved_policy(self, slug, default_policy):
        return saved_source_policy(self.connection, slug, default_policy)

    def queue_saved(self, slug, input_name, policy):
        return _stage_saved(self.connection, slug, input_name, policy, "scheduled")

    def schedule_at(self, slug, next_at):
        self.connection.execute("UPDATE ingest_sources SET next_run_at = ? WHERE slug = ?", (timestamp(next_at), slug))

    def next_run(self, slugs, observed):
        return self.connection.execute(f"""
            SELECT r.*, s.adapter FROM source_runs r JOIN ingest_sources s ON s.slug = r.source_slug
            WHERE r.state = 'queued' AND (r.retry_after IS NULL OR r.retry_after <= ?) AND {_available(slugs)}
            ORDER BY r.id LIMIT 1
        """, (timestamp(observed), *slugs)).fetchone()

    def claim(self, run_id, observed, lease_until):
        self.connection.execute("""
            UPDATE source_runs SET state = 'running', started_at = ?, finished_at = NULL, attempts = attempts + 1,
                error = NULL, lease_until = ?, retry_after = NULL WHERE id = ?
        """, (timestamp(observed), timestamp(lease_until), run_id))

    def owns(self, run):
        return self.connection.execute("""
            SELECT 1 FROM source_runs WHERE id = ? AND state = 'running' AND attempts = ?
        """, (run["id"], run["attempts"])).fetchone() is not None

    def health_event(self, source, run_id, kind, message, observed):
        self.connection.execute("""
            INSERT INTO source_health_events(source_slug, run_id, kind, message, occurred_at)
            VALUES (?, ?, ?, ?, ?)
        """, (source, run_id, kind, message, timestamp(observed)))

    def retry(self, run_id, retry_after, message):
        self.connection.execute("""
            UPDATE source_runs SET state = 'queued', retry_count = retry_count + 1, retry_after = ?,
                lease_until = NULL, error = ? WHERE id = ?
        """, (timestamp(retry_after), message, run_id))

    def finish(self, run_id, state, observed, *, error=None):
        self.connection.execute("""
            UPDATE source_runs SET state = ?, error = ?, finished_at = ?, lease_until = NULL, retry_after = NULL
            WHERE id = ?
        """, (state, error, timestamp(observed), run_id))

    def saved_run(self, run_id):
        return self.connection.execute("SELECT * FROM saved_export_runs WHERE run_id = ?", (run_id,)).fetchone()

    def has_staged(self, run_id):
        return self.connection.execute("SELECT 1 FROM source_run_rows WHERE run_id = ? LIMIT 1", (run_id,)).fetchone() is not None

    def stage(self, run_id, rows):
        _stage(self.connection, run_id, rows)

    def rows(self, run_id, *, after=None, limit=None):
        query = "SELECT row_number, payload_json FROM source_run_rows WHERE run_id = ?"
        params = (run_id,)
        if after is not None:
            query += " AND row_number > ?"
            params += (after,)
        query += " ORDER BY row_number"
        if limit is not None:
            query += " LIMIT ?"
            params += (limit,)
        return self.connection.execute(query, params).fetchall()

    def save_results(self, results):
        self.connection.executemany("""
            UPDATE source_run_rows SET result = ?, detail = ? WHERE run_id = ? AND row_number = ?
        """, results)

    def has_review_results(self, run_id):
        return self.connection.execute("""
            SELECT 1 FROM source_run_rows WHERE run_id = ? AND result IN ('invalid', 'review', 'suppressed') LIMIT 1
        """, (run_id,)).fetchone() is not None

    def replay(self, source, payload):
        return replay_result(self.connection, source, payload)

    def remember_progress(self, run_id, cursor, lease_until):
        self.connection.execute("UPDATE saved_export_runs SET last_row = ? WHERE run_id = ?", (cursor, run_id))
        self.connection.execute("UPDATE source_runs SET lease_until = ? WHERE id = ?", (timestamp(lease_until), run_id))

    def count_rows(self, run_id):
        return self.connection.execute("SELECT COUNT(*) FROM source_run_rows WHERE run_id = ?", (run_id,)).fetchone()[0]

    def recent_row_counts(self, run):
        rows = self.connection.execute("""
            SELECT (SELECT COUNT(*) FROM source_run_rows WHERE run_id = r.id) AS total
            FROM source_runs r
            WHERE r.source_slug = ? AND r.input_name = ? AND r.id != ?
                AND r.state IN ('ready', 'complete', 'partial') AND r.finished_at IS NOT NULL
            ORDER BY r.finished_at DESC LIMIT 5
        """, (run["source_slug"], run["input_name"], run["id"])).fetchall()
        return [row["total"] for row in rows]
