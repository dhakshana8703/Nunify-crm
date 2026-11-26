import io
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from app import create_app
from crm_store import connect
from source_connectors import SavedExportConnector
from source_ops import work_one
from test_workflow import RenderedForm
from workflow_store import DEMO_PASSWORD


class SourceConnectorLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.folder = self.root / "instance" / f"connector-test-{uuid4().hex}"
        self.folder.mkdir(parents=True)
        self.addCleanup(self.remove_files)
        self.database = self.folder / "test.sqlite3"
        self.clock = datetime(2027, 1, 4, 12, tzinfo=timezone.utc)
        self.event_id = str(uuid4())
        self.listing_id = str(uuid4())
        self.save_inputs()

    def remove_files(self):
        for file in self.folder.iterdir():
            file.unlink()
        self.folder.rmdir()

    def save_inputs(self):
        review_id = str(uuid4())
        events = [
            {"event_uuid": self.event_id, "cvent_url": f"https://web.cvent.com/event/{self.event_id}/summary",
             "organizer": "Example Trade Association", "company_domain": "trade.example",
             "event_name": "Example Annual Meeting", "start_date": "2027-01-14"},
            {"event_uuid": review_id, "cvent_url": f"https://web.cvent.com/event/{review_id}/summary",
             "organizer": "Unverified Local Chapter", "event_name": "Unverified Meeting"},
        ]
        listings = [{
            "name": "Example Trade Association", "website": "https://trade.example", "type": "National",
            "category": "Business", "member_count": "2500",
            "source_url": f"https://directoryofassociations.com/view/?di={self.listing_id}",
        }]
        for filename, records in (("master_cvent_events.json", events), ("all_associations.json", listings)):
            (self.folder / filename).write_text(json.dumps(records), encoding="utf-8")

    def start_app(self, connectors):
        with patch.dict(os.environ, CRM_SOURCE_CONNECTORS=connectors):
            app = create_app(self.database, demo=not self.database.exists())
        app.config.update(TESTING=True, CRM_CLOCK=lambda: self.clock)
        return app

    def sign_in(self, app, username):
        client = app.test_client()
        self.assertEqual(client.get("/login").status_code, 200)
        signed_in = self.post(client, "/login", email=f"{username}@example.test", password=DEMO_PASSWORD)
        self.assertEqual(signed_in.status_code, 303)
        self.assertEqual(client.get(signed_in.location).status_code, 200)
        return client

    def post(self, client, path, **data):
        with client.session_transaction() as session:
            token = session["csrf"]
        return client.post(path, data={"csrf_token": token, **data})

    def run_path(self, response):
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.location.startswith("/sources/runs/"))
        return response.location

    def query(self, statement, *params):
        with closing(connect(self.database, read_only=True)) as db:
            return [tuple(row) for row in db.execute(statement, params)]

    def work(self, connectors):
        with (
            patch.dict(os.environ, CRM_SOURCE_CONNECTORS=connectors),
            patch("source_ops.utcnow", return_value=self.clock),
            closing(connect(self.database)) as db,
        ):
            return work_one(db, cvent_dir=self.folder, directory_dir=self.folder)

    def assert_worked(self, connectors, run_path):
        self.assertEqual(self.work(connectors), int(run_path.rsplit("/", 1)[1]))

    def test_saved_connector_only_needs_to_read_rows(self):
        app = self.start_app("cvent")
        built_in = app.config["SOURCE_CONNECTORS"]["cvent"].adapter
        reader = SimpleNamespace(FILE_NAME=built_in.FILE_NAME, DEFAULT_POLICY=built_in.DEFAULT_POLICY,
                                 load_rows=built_in.load_rows)
        connectors = {"cvent": SavedExportConnector(reader, self.folder)}
        app.config["SOURCE_CONNECTORS"] = connectors
        admin = self.sign_in(app, "admin")
        run = self.run_path(self.post(admin, "/sources/cvent/queue"))
        run_id = int(run.rsplit("/", 1)[1])
        with closing(connect(self.database)) as db:
            self.assertEqual(work_one(db, connectors=connectors), run_id)
        self.assertEqual(admin.get(run + "/status").json["state"], "ready")
        self.assertEqual(self.query("SELECT id FROM accounts"), [])

        self.run_path(self.post(admin, run + "/queue"))
        with closing(connect(self.database)) as db:
            self.assertEqual(work_one(db, connectors=connectors), run_id)
        self.assertEqual(admin.get(run + "/status").json["state"], "partial")
        self.assertEqual(self.query("SELECT domain FROM accounts"), [("trade.example",)])

    def test_detach_cvent_keeps_history_and_sdr_work_while_directory_runs(self):
        both = "cvent,directory"
        app = self.start_app(both)
        admin = self.sign_in(app, "admin")
        first = self.run_path(self.post(admin, "/sources/cvent/queue"))
        self.assert_worked(both, first)
        self.run_path(self.post(admin, first + "/queue"))
        self.assert_worked(both, first)
        [(account_id,)] = self.query("SELECT id FROM accounts")
        [(rep_id,)] = self.query("SELECT id FROM users WHERE email = 'riya@example.test'")
        with closing(connect(self.database)) as db, db:
            db.execute("""
                INSERT INTO people(account_id, name, email, origin, unlocked, email_verified, linkedin_url)
                VALUES (?, 'Saved Contact', 'sales.fixture@trade.example', 'apollo', 1, 1,
                        'https://www.linkedin.com/in/saved-fixture')
            """, (account_id,))
        account_path = f"/accounts/{account_id}"
        manager, rep = self.sign_in(app, "manager"), self.sign_in(app, "riya")
        assignment = self.post(manager, f"/assignment/{account_id}", owner_id=rep_id, revision=0)
        self.assertEqual(assignment.status_code, 303)
        account = rep.get(account_path)
        self.assertEqual(account.status_code, 200)
        call_path = account_path + "/activities"
        form = RenderedForm(call_path)
        form.feed(account.get_data(as_text=True))
        self.assertTrue(form.found)
        logged = self.post(rep, call_path, **{
            **form.hidden, "channel": "call", "outcome": "no_answer", "due_tomorrow": 1,
            "next_step": "Confirm the upcoming conference",
        })
        self.assertEqual(logged.status_code, 303)
        waiting = self.run_path(self.post(admin, "/sources/cvent/queue"))

        attached = "directory"
        app = self.start_app(attached)
        admin, rep = self.sign_in(app, "admin"), self.sign_in(app, "riya")
        sources = admin.get("/sources")
        self.assertIn(b"Connector disconnected", sources.data)
        self.assertIn(first.encode(), sources.data)
        waiting_page = admin.get(waiting)
        self.assertEqual(waiting_page.status_code, 200)
        self.assertNotIn(b"data-source-run-status", waiting_page.data)
        self.assertEqual(admin.get(waiting + "/status").json, {"state": "queued", "attempts": 0})
        self.assertIn(b"Example Annual Meeting", admin.get(first).data)
        review_path = f"/source-review/{first.rsplit('/', 1)[1]}/2"
        self.assertIn(b"Unverified Meeting", admin.get(review_path).data)
        for route in ("/sources/cvent/queue", "/sources/cvent/schedule", waiting + "/queue"):
            with self.subTest(route=route):
                self.assertEqual(self.post(admin, route).status_code, 409)

        listing = self.run_path(self.post(admin, "/sources/directory/export"))
        self.assert_worked(attached, listing)
        self.run_path(self.post(admin, listing + "/queue"))
        self.assert_worked(attached, listing)
        self.assertIsNone(self.work(attached))
        self.assertEqual(self.query("SELECT id FROM accounts"), [(account_id,)])
        self.assertEqual(self.query("SELECT source, account_id FROM source_records ORDER BY source"),
                         [("cvent", account_id), ("directory", account_id)])
        account = rep.get(account_path)
        self.assertIn(b"Example Annual Meeting", account.data)
        self.assertIn(b"Directory of Associations", account.data)
        self.assertIn(b"sales.fixture@trade.example", account.data)
        self.assertEqual(self.query("SELECT unlocked, email_verified FROM people WHERE account_id = ?", account_id),
                         [(1, 1)])
        self.assertIn(b"Confirm the upcoming conference", rep.get("/today?bucket=tomorrow").data)
        self.assertEqual(self.query("SELECT owner_id FROM account_work WHERE account_id = ?", account_id), [(rep_id,)])

        admin = self.sign_in(self.start_app(both), "admin")
        self.assert_worked(both, waiting)
        self.assertEqual(admin.get(waiting + "/status").json, {"state": "ready", "attempts": 1})

    def test_detach_directory_keeps_schedule_and_generic_imports_work_with_no_builtin(self):
        admin = self.sign_in(self.start_app("cvent,directory"), "admin")
        with patch("source_ops.utcnow", return_value=self.clock):
            scheduled = self.post(admin, "/sources/directory/schedule", schedule_days=1)
        self.assertEqual(scheduled.status_code, 303)
        due = self.query("SELECT next_run_at FROM ingest_sources WHERE slug = 'directory'")
        self.assertTrue(due[0][0])
        self.clock += timedelta(days=1)

        admin = self.sign_in(self.start_app("cvent"), "admin")
        self.assertEqual(self.post(admin, "/sources/directory/export").status_code, 409)
        self.assertEqual(self.post(admin, "/sources/directory/schedule").status_code, 409)
        self.assertNotIn(b"Stage association export", admin.get("/sources").data)
        preview = self.run_path(self.post(admin, "/sources/cvent/queue"))
        self.assert_worked("cvent", preview)
        self.assertIsNone(self.work("cvent"))
        self.assertEqual(self.query("SELECT * FROM source_runs WHERE source_slug = 'directory'"), [])
        self.assertEqual(self.query("SELECT next_run_at FROM ingest_sources WHERE slug = 'directory'"), due)

        app = self.start_app("none")
        admin = self.sign_in(app, "admin")
        self.assertEqual(self.post(admin, preview + "/queue").status_code, 409)
        csv = io.BytesIO(b"source_id,name,domain\nupload-1,Uploaded Company,uploaded.example\n")
        uploaded = self.run_path(self.post(admin, "/sources/upload", source_file=(csv, "companies.csv")))
        self.run_path(self.post(admin, uploaded + "/queue"))
        self.assert_worked("none", uploaded)

        issued = self.post(admin, "/sources/keys", name="Partner", slug="partner-events")
        self.assertEqual(issued.status_code, 200)
        key = re.search(rb"<code>([a-zA-Z0-9_-]{43})</code>", issued.data)[1].decode()
        sender = app.test_client()
        headers = {"Authorization": f"Bearer {key}"}
        intake = "/api/ingest/partner-events/runs"
        opened = sender.post(intake, headers=headers, json={"idempotency_key": "partner-run-1"})
        self.assertEqual(opened.status_code, 201)
        run = f"{intake}/{opened.json['run_id']}"
        batch = sender.post(run + "/records", headers=headers, json={
            "idempotency_key": "batch-1", "batch_number": 1,
            "records": [{"source_id": "p1", "name": "Partner Organization", "domain": "partner.example"}],
        })
        self.assertEqual(batch.status_code, 201)
        external = sender.post(run + "/complete", headers=headers, json={"expected_batches": 1})
        self.assertEqual(external.status_code, 202)
        external_path = f"/sources/runs/{external.json['source_run_id']}"
        self.run_path(self.post(admin, external_path + "/queue"))
        self.assert_worked("none", external_path)
        self.assertEqual(self.post(admin, "/sources/partner-events/keys/revoke").status_code, 303)
        denied = sender.post(intake, headers=headers, json={"idempotency_key": "partner-run-2"})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(self.query("SELECT domain FROM accounts ORDER BY domain"),
                         [("partner.example",), ("uploaded.example",)])
        self.assertIsNone(self.work("none"))

        restored = self.work("directory")
        self.assertIsNotNone(restored)
        self.assertEqual(self.query("SELECT trigger, state FROM source_runs WHERE id = ?", restored),
                         [("scheduled", "ready")])

    def test_application_and_worker_start_without_detached_modules(self):
        script = """
import sys

database, *absent = sys.argv[1:]
for name in absent:
    sys.modules[name] = None

from app import create_app
from worker import main

app = create_app(database)
assert app.test_client().get('/login').status_code == 200
sys.argv = ['worker.py', '--db', database, '--offline', '--once']
main()
"""
        for connectors, absent in (
            ("directory", ["cvent_import"]), ("cvent", ["directory_import"]),
            ("", ["cvent_import", "directory_import"]),
        ):
            with self.subTest(connectors=connectors):
                result = subprocess.run(
                    [sys.executable, "-B", "-c", script, str(self.database), *absent], cwd=self.root,
                    env={**os.environ, "CRM_SOURCE_CONNECTORS": connectors}, capture_output=True, text=True,
                    timeout=90, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
