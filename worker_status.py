"""Store when the worker last completed a pass against this database."""

from workflow_store import timestamp, transaction


def last_pass(connection):
    row = connection.execute("SELECT value FROM workflow_settings WHERE key = 'worker_last_pass'").fetchone()
    return row["value"] if row else None


def record_pass(connection):
    with transaction(connection):
        connection.execute("""
            INSERT INTO workflow_settings(key, value) VALUES ('worker_last_pass', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (timestamp(),))
