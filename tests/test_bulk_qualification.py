import json
import re
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app import create_app
from crm_store import connect, import_snapshot
from growth_ops import DEFAULT_RULES, qualify_batch
from workflow_store import DEMO_PASSWORD, timestamp


def saved_record(source, identity):
    details = {"event_name": "Annual Expo", "start_date_iso": "2027-05-03"} if source == "cvent" else {"members": "12,000"}
    return {
        "source": source, "source_id": f"{identity}-{source}", "source_url": f"https://source.test/{identity}",
        "source_file": "evidence.json", "identity_key": identity, "reason": None, "details": details, "raw": details,
    }


class BulkQualificationTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / "instance" / f"test-qualification-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(self.remove_files)
        self.database = self.directory / "work.sqlite3"
        accounts = {
            ident: {"name": f"{ident.title()} Organization", "domain": f"{ident}.example.test", "organization_type": "Association"}
            for ident in ("expo", "society", "combined", "blocked", "reviewed")
        }
        records = [
            saved_record("cvent", "expo"), saved_record("directory", "society"),
            saved_record("cvent", "combined"), saved_record("directory", "combined"),
            saved_record("cvent", "blocked"), saved_record("directory", "reviewed"),
        ]
        result = import_snapshot(self.database, accounts, records)
        self.ids = {account["identity_key"]: account["id"] for account in result["accounts"]}
        self.app = create_app(self.database, demo=True)
        self.app.testing = True
        self.now = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
        self.app.config["CRM_CLOCK"] = lambda: self.now
        with closing(connect(self.database)) as connection, connection:
            self.rep_id = connection.execute("SELECT id FROM users WHERE email = 'riya@example.test'").fetchone()[0]
            connection.execute("""
                INSERT INTO account_work (account_id, owner_id, stage, revision, assigned_at)
                VALUES (?, ?, 'working', 3, ?)
            """, (self.ids["combined"], self.rep_id, timestamp(self.now)))
            connection.execute("""
                INSERT INTO qualifications (account_id, score, signals, observed_at, override)
                VALUES (?, 55, '[{"label":"Manually reviewed"}]', ?, 1)
            """, (self.ids["reviewed"], timestamp(self.now)))
            connection.execute("""
                INSERT INTO suppression(kind, value, reason, created_at)
                VALUES ('domain', 'blocked.example.test', 'Do not call', ?)
            """, (timestamp(self.now),))
            connection.execute("INSERT INTO workflow_settings (key, value) VALUES ('qualification', ?)", (
                json.dumps(dict(DEFAULT_RULES, auto_enabled=True)),
            ))

    def remove_files(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def login(self, email, landing):
        client = self.app.test_client()
        login = client.get("/login")
        csrf = re.search(rb'name="csrf_token"\s+value="([^"]+)"', login.data)[1].decode()
        result = client.post("/login", data={"email": email, "password": DEMO_PASSWORD, "csrf_token": csrf})
        self.assertIn(result.status_code, (302, 303))
        self.assertEqual(client.get(landing).status_code, 200)
        return client

    def post(self, client, url, **data):
        with client.session_transaction() as session:
            csrf = session["csrf"]
        return client.post(url, data={"csrf_token": csrf, **data})

    def batch(self):
        with closing(connect(self.database)) as connection:
            return qualify_batch(connection, clock=lambda: self.now)

    def test_scope_preserves_existing_work_and_scores_without_spend(self):
        manager = self.login("manager@example.test", "/operations")
        rep = self.login("riya@example.test", "/today")
        self.assertEqual(self.post(rep, "/operations/qualification-jobs", scope="all").status_code, 403)
        self.assertEqual(self.post(manager, "/operations/qualification-jobs", scope="both").status_code, 303)
        self.assertEqual(self.post(manager, "/operations/qualification-jobs", scope="both").status_code, 303)
        self.assertEqual(self.batch(), 1)
        self.assertIsNone(self.batch())
        with closing(connect(self.database)) as connection:
            job = connection.execute("SELECT state, total, scored, qualified FROM qualification_jobs").fetchone()
            self.assertEqual(tuple(job), ("done", 1, 1, 1))
            self.assertEqual(connection.execute("SELECT count(*) FROM qualification_jobs").fetchone()[0], 1)
        self.assertIn(b"1 of 1 processed", manager.get("/operations").data)
        self.assertEqual(self.post(manager, "/operations/qualification-jobs", scope="all").status_code, 303)
        self.assertEqual(self.batch(), 2)
        self.assertIsNone(self.batch())
        with closing(connect(self.database)) as connection:
            scores = dict(connection.execute("SELECT account_id, score FROM qualifications").fetchall())
            self.assertEqual(scores, {
                self.ids["expo"]: 70, self.ids["society"]: 15, self.ids["combined"]: 95, self.ids["reviewed"]: 55,
            })
            owner = connection.execute("""
                SELECT owner_id, stage, revision FROM account_work WHERE account_id = ?
            """, (self.ids["combined"],)).fetchone()
            self.assertEqual(tuple(owner), (self.rep_id, "working", 3))
            job = connection.execute("SELECT total, processed, scored, suppressed FROM qualification_jobs WHERE id = 2").fetchone()
            self.assertEqual(tuple(job), (4, 4, 2, 1))
            self.assertEqual(connection.execute("SELECT count(*) FROM credit_jobs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 0)

    def test_failed_batch_retries_from_cursor(self):
        manager = self.login("manager@example.test", "/operations")
        self.assertEqual(self.post(manager, "/operations/qualification-jobs", scope="all").status_code, 303)
        with patch("growth_ops.QUALIFICATION_BATCH_SIZE", 1):
            self.assertEqual(self.batch(), 1)
            with patch("growth_ops.qualify", side_effect=ValueError("Unprocessable evidence")):
                self.assertEqual(self.batch(), 1)
        with closing(connect(self.database)) as connection:
            job = connection.execute("SELECT state, processed, scored FROM qualification_jobs").fetchone()
            self.assertEqual(tuple(job), ("failed", 1, 1))
        self.assertIn(b"Retry qualification", manager.get("/operations").data)
        response = self.post(manager, "/operations/qualification-jobs/1/retry")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.batch(), 1)
        self.assertIsNone(self.batch())
        with closing(connect(self.database)) as connection:
            job = connection.execute("SELECT state, processed, scored, suppressed FROM qualification_jobs").fetchone()
            self.assertEqual(tuple(job), ("done", 5, 3, 1))
            self.assertEqual(connection.execute("SELECT count(*) FROM qualifications").fetchone()[0], 4)

    def test_saved_scores_populate_shortlist_without_recomputing_evidence(self):
        manager = self.login("manager@example.test", "/operations")
        shortlist = "/assignment?filter=qualified_unassigned"
        with closing(connect(self.database)) as connection, connection:
            for identity, score, override in (("expo", 60, None), ("society", 80, 0), ("combined", 20, None)):
                connection.execute("""
                    INSERT INTO qualifications(account_id, score, signals, observed_at, override)
                    VALUES (?, ?, '[{"label":"Saved website evidence"}]', ?, ?)
                """, (self.ids[identity], score, timestamp(self.now), override))
            connection.execute("UPDATE qualifications SET score = 5 WHERE account_id = ?", (self.ids["reviewed"],))
            connection.execute("""
                INSERT INTO account_work(account_id, stage, revision) VALUES (?, 'qualified', 2)
            """, (self.ids["society"],))
            expected_scores = [tuple(row) for row in connection.execute("SELECT * FROM qualifications ORDER BY account_id")]
        self.assertNotIn(b"Expo Organization", manager.get(shortlist).data)
        self.assertEqual(self.post(manager, "/operations/qualification-jobs", scope="cvent").status_code, 303)
        with patch("growth_ops.qualify", side_effect=AssertionError("Saved scores must be reused")):
            self.assertEqual(self.batch(), 1)
        self.assertIn(b"Expo Organization", manager.get(shortlist).data)
        old_submission = self.post(manager, f'/assignment/{self.ids["expo"]}', revision=0, owner_id=self.rep_id)
        self.assertEqual(old_submission.status_code, 409)
        self.assertEqual(self.post(manager, "/operations/qualification-jobs", scope="directory").status_code, 303)
        with patch("growth_ops.qualify", side_effect=AssertionError("Saved scores must be reused")):
            self.assertEqual(self.batch(), 2)
        filtered = manager.get(shortlist).data
        self.assertIn(b"Expo Organization", filtered)
        self.assertIn(b"Reviewed Organization", filtered)
        self.assertNotIn(b"Society Organization", filtered)
        self.assertNotIn(b"Combined Organization", filtered)
        self.assertEqual(self.post(manager, "/operations/qualification-jobs", scope="directory").status_code, 303)
        self.assertEqual(self.batch(), 3)

        with closing(connect(self.database)) as connection:
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM qualifications ORDER BY account_id")],
                             expected_scores)
            stages = {row["account_id"]: tuple(row)[1:] for row in connection.execute("""
                SELECT account_id, owner_id, stage, revision FROM account_work
            """)}
            self.assertEqual(stages, {
                self.ids["expo"]: (None, "qualified", 1), self.ids["reviewed"]: (None, "qualified", 1),
                self.ids["society"]: (None, "disqualified", 3), self.ids["combined"]: (self.rep_id, "working", 3),
            })
            jobs = [tuple(row) for row in connection.execute("""
                SELECT total, processed, scored, qualified, suppressed FROM qualification_jobs ORDER BY id
            """)]
            self.assertEqual(jobs, [(2, 2, 0, 1, 1), (2, 2, 0, 1, 0), (0, 0, 0, 0, 0)])
            self.assertEqual(connection.execute("SELECT count(*) FROM credit_jobs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 0)
