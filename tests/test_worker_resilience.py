import io
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import worker
from crm_store import connect, initialize


class WorkerResilienceTest(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1] / "instance" / f"worker-resilience-{uuid4().hex}"
        folder.mkdir(parents=True)
        self.database = folder / "test.sqlite3"
        self.addCleanup(self.remove_database)
        initialize(self.database)

    def remove_database(self):
        for suffix in ("-wal", "-shm", "-journal", ""):
            self.database.with_name(self.database.name + suffix).unlink(missing_ok=True)
        self.database.parent.rmdir()

    def test_an_open_read_does_not_block_a_worker_commit(self):
        with closing(connect(self.database, read_only=True)) as viewer, closing(connect(self.database)) as writer:
            viewer.execute("BEGIN")
            self.assertEqual(viewer.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
            with writer:
                writer.execute("""
                    INSERT INTO accounts(identity_key, name, domain, organization_type)
                    VALUES ('sample', 'Organization', 'org.example', 'Association')
                """)
            self.assertEqual(viewer.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
            viewer.rollback()
            self.assertEqual(viewer.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)

    def test_worker_retries_a_temporary_lock(self):
        failure = sqlite3.OperationalError("database is locked")
        failure.sqlite_errorcode = sqlite3.SQLITE_BUSY
        args = ["worker.py", "--offline", "--once", "--db", str(self.database)]
        with (
            patch("sys.argv", args),
            patch("worker.run_pass", side_effect=[failure, (None, None, None, None)]) as execute_pass,
            patch("worker.time.sleep") as wait,
            redirect_stdout(io.StringIO()) as output,
        ):
            worker.main()
        self.assertEqual(execute_pass.call_count, 2)
        wait.assert_called_once_with(2)
        self.assertIn("Retrying the worker pass", output.getvalue())


if __name__ == "__main__":
    unittest.main()
