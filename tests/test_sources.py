import io
import json
import re
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

from openpyxl import Workbook

import source_ops
from app import create_app
from crm_store import connect, import_snapshot
from import_demo import ACCOUNTS, load_cvent_records
from source_fixtures import write_cvent_fixture
from source_ops import _new_run, work_one
from workflow_store import DEMO_PASSWORD


class SourcesTest(unittest.TestCase):
    def setUp(self):
        instance = Path(__file__).resolve().parents[1] / "instance"
        self.directory = instance / f"source-test-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(self._clean_files)
        self.database = self.directory / "sources.sqlite3"
        import_snapshot(self.database, ACCOUNTS, [])
        self.app = create_app(self.database, demo=True)
        self.app.testing = True
        self.admin = self.app.test_client()
        self._login(self.admin, "admin")
        self.assertEqual(self.admin.get("/sources").status_code, 200)

    def _clean_files(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def _token(self, client):
        with client.session_transaction() as session:
            return session["csrf"]

    def _login(self, client, actor):
        self.assertEqual(client.get("/login").status_code, 200)
        response = client.post("/login", data={
            "email": f"{actor}@example.test", "password": DEMO_PASSWORD, "csrf_token": self._token(client),
        })
        self.assertEqual(response.status_code, 303)
        if actor == "admin":
            self.assertEqual(response.location, "/sources")

    def _post(self, url, **data):
        return self.admin.post(url, data={"csrf_token": self._token(self.admin), **data})

    def _run(self, **provider_paths):
        with closing(connect(self.database)) as connection:
            return work_one(connection, **provider_paths)

    def _preview(self, name, payload):
        response = self._post("/sources/upload", source_file=(payload, name))
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.location.startswith("/sources/runs/"))
        return response.location

    def test_legacy_saved_run_never_promotes_demo_records(self):
        write_cvent_fixture(self.directory)
        selected_event = load_cvent_records(self.directory)[0]
        with closing(connect(self.database)) as connection, connection:
            run_id = _new_run(connection, "cvent", "Legacy saved run", "manual", rows=[(1, selected_event)])
        self.assertEqual(self._run(), run_id)
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 0)
            run = connection.execute("SELECT state, error FROM source_runs WHERE id = ?", (run_id,)).fetchone()
            self.assertEqual(run["state"], "failed")
            self.assertIn("Start a new preview", run["error"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_run_rows WHERE run_id = ?", (run_id,)).fetchone()[0], 1)

    def test_csv_and_xlsx_share_staging_and_keep_source_identity(self):
        payload = io.BytesIO(b"source_id,name,domain,contact_name,contact_email\n"
                             b"aarp-1,AARP,aarp.org,Test Planner,planner@example.test\n"
                             b"chapter,AARP Local Chapter,aarp.org,,\n"
                             b"blocked,Cvent staff,staff.cventevents.com,,\n"
                             b"new,Test Association,test-association.example,,\n")
        source_path = self._preview("saved-organizations.csv", payload)
        preview = self.admin.get(source_path)
        self.assertIn(b"Matches an existing account.", preview.data)
        self.assertIn(b"The domain is shared", preview.data)
        self.assertIn(b"Domain is on the suppression list.", preview.data)
        self.assertEqual(self._post(source_path + "/queue").status_code, 303)
        self.assertEqual(self._run(), 1)
        self.assertIn(b"2 imported", self.admin.get(source_path).data)
        self.assertIn(b"1 suppressed", self.admin.get(source_path).data)
        with closing(connect(self.database)) as connection, connection:
            aarp = connection.execute("SELECT id FROM accounts WHERE identity_key = 'aarp'").fetchone()[0]
            rep_id = connection.execute("SELECT id FROM users WHERE email = 'riya@example.test'").fetchone()[0]
            connection.execute("UPDATE people SET unlocked = 1 WHERE email = 'planner@example.test'")
            connection.execute("UPDATE accounts SET owner = 'Configured owner' WHERE id = ?", (aarp,))
        self.assertEqual(self._post(f"/assignment/{aarp}", owner_id=rep_id, revision=0).status_code, 303)
        rep = self.app.test_client()
        self._login(rep, "riya")
        self.assertEqual(rep.get(f"/accounts/{aarp}").status_code, 200)
        note = rep.post(f"/accounts/{aarp}/activities", data={
            "csrf_token": self._token(rep), "action_key": "rep-note-source-replay", "revision": 1,
            "channel": "call", "outcome": "no_answer", "note": "Keep this rep-entered activity",
        })
        self.assertEqual(note.status_code, 303)

        write_cvent_fixture(self.directory)
        saved_event = load_cvent_records(self.directory)[0]
        self.assertEqual(import_snapshot(self.database, ACCOUNTS, [saved_event])["changed_records"], 1)
        self.assertEqual(import_snapshot(self.database, ACCOUNTS, [saved_event])["changed_records"], 0)

        book = Workbook()
        sheet = book.active
        sheet.append(["Source ID", "Name", "Domain", "Contact Name", "Contact Email"])
        sheet.append(["aarp-1", "AARP National", "https://www.aarp.org", "Source planner update", "planner@example.test"])
        workbook = io.BytesIO()
        book.save(workbook)
        workbook.seek(0)
        xlsx_path = self._preview("saved-organizations.xlsx", workbook)
        self.assertEqual(self._post(xlsx_path + "/queue").status_code, 303)
        self.assertEqual(self._run(), 2)
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM accounts").fetchone()[0], 3)
            account = connection.execute("SELECT owner FROM accounts WHERE id = ?", (aarp,)).fetchone()
            self.assertEqual(account["owner"], "Configured owner")
            workflow = connection.execute("SELECT owner_id, stage FROM account_work WHERE account_id = ?", (aarp,)).fetchone()
            self.assertEqual(tuple(workflow), (rep_id, "working"))
            activity = connection.execute("SELECT actor_id, note FROM activities WHERE account_id = ?", (aarp,)).fetchone()
            self.assertEqual(tuple(activity), (rep_id, "Keep this rep-entered activity"))
            person = connection.execute("SELECT name, unlocked FROM people WHERE email = 'planner@example.test'").fetchone()
            self.assertEqual(tuple(person), ("Test Planner", 1))
            original = connection.execute("""
                SELECT account_id, json_extract(payload_json, '$.name') FROM imported_source_evidence WHERE external_id = 'aarp-1'
            """).fetchone()
            self.assertEqual(tuple(original), (aarp, "AARP National"))

    def test_csv_contact_checks_mixed_case_active_suppression(self):
        with closing(connect(self.database)) as connection, connection:
            rule_id = connection.execute("""
                INSERT INTO suppression (kind, value, reason, reason_note, created_at)
                VALUES ('email', 'Planner@EXAMPLE.TEST', 'manual', 'Existing restriction', '2026-11-20T00:00:00+00:00')
            """).lastrowid

        def upload(source_id):
            return self._preview("contacts.csv", io.BytesIO((
                "source_id,name,domain,contact_name,contact_email\n"
                f"{source_id},AARP,aarp.org,Test Planner,planner@example.test\n"
            ).encode()))

        blocked_path = upload("blocked-contact")
        self.assertIn(b"The contact matches an outreach restriction.", self.admin.get(blocked_path).data)
        self.assertEqual(self._post(blocked_path + "/queue").status_code, 303)
        self.assertEqual(self._run(), 1)
        with closing(connect(self.database)) as connection:
            self.assertIsNone(connection.execute("SELECT id FROM people WHERE email = 'planner@example.test'").fetchone())

        self.assertEqual(self._post(f"/suppression/{rule_id}/remove").status_code, 303)
        active_path = upload("available-contact")
        self.assertEqual(self._post(active_path + "/queue").status_code, 303)
        self.assertEqual(self._run(), 2)
        with closing(connect(self.database)) as connection:
            self.assertIsNotNone(connection.execute("SELECT id FROM people WHERE email = 'planner@example.test'").fetchone())

    def test_external_key_scope_overlap_retry_and_revocation(self):
        manager = self.app.test_client()
        self._login(manager, "manager")
        self.assertEqual(manager.get("/sources").status_code, 403)
        issued = self._post("/sources/keys", name="Partner", slug="partner-events")
        self.assertEqual(issued.status_code, 200)
        key = re.search(rb"<code>([a-zA-Z0-9_-]{43})</code>", issued.data).group(1).decode()
        api = self.app.test_client()
        headers = {"Authorization": f"Bearer {key}"}
        route = "/api/sources/partner-events/ingest"
        submission = {"idempotency_key": "partner-first", "records": [
            {"source_id": "partner-1", "name": "AARP", "domain": "aarp.org"},
        ]}
        self.assertEqual(api.post(route, json=submission).status_code, 401)
        self.assertEqual(api.post("/api/sources/cvent/ingest", json=submission, headers=headers).status_code, 401)
        accepted = api.post(route, json=submission, headers=headers)
        self.assertEqual(accepted.status_code, 202)
        replay = api.post(route, json=submission, headers=headers)
        self.assertEqual((replay.status_code, replay.json["run_id"]), (200, accepted.json["run_id"]))
        self.assertEqual(api.post(route, json=dict(submission, idempotency_key="partner-next"), headers=headers).status_code, 409)
        self.assertEqual(api.post(route, json=dict(submission, records=[{}]), headers=headers).status_code, 409)
        self.assertEqual(self._run(), accepted.json["run_id"])
        self.assertEqual(self._post("/sources/partner-events/keys/revoke").status_code, 303)
        self.assertEqual(api.post(route, json=submission, headers=headers).status_code, 401)

    def test_workbook_with_doctype_cannot_enter_staging(self):
        payload = io.BytesIO()
        with ZipFile(payload, mode="w") as zipped:
            zipped.writestr("_rels/.rels", '<!DOCTYPE Relationships><Relationships/>')
        payload.seek(0)
        response = self._post("/sources/upload", source_file=(payload, "invalid.xlsx"))
        self.assertEqual((response.status_code, response.location), (303, "/sources"))
        self.assertIn(b"cannot contain a document type declaration", self.admin.get("/sources").data)
        self.assertIsNone(self._run())

    def test_worker_failure_is_visible_and_retry_reuses_run(self):
        queued = self._post("/sources/cvent/queue")
        self.assertEqual(queued.status_code, 303)
        status_path = queued.location + "/status"
        page = self.admin.get(queued.location)
        self.assertIn(f'data-source-run-status="{status_path}"'.encode(), page.data)
        status = self.admin.get(status_path)
        self.assertEqual(status.json, {"state": "queued", "attempts": 0})
        self.assertTrue(status.cache_control.no_store)
        for actor in ("manager", "riya"):
            client = self.app.test_client()
            self._login(client, actor)
            self.assertEqual(client.get(status_path).status_code, 403)

        self.assertEqual(self._post("/sources/cvent/queue").location, queued.location)
        self.assertEqual(self._run(cvent_dir=self.directory), 1)
        failure = self.admin.get(queued.location)
        self.assertIn(b"Retry run", failure.data)
        self.assertIn(b"FileNotFoundError", failure.data)
        self.assertNotIn(b"data-source-run-status", failure.data)
        self.assertEqual(self.admin.get(status_path).json, {"state": "failed", "attempts": 1})
        self.assertEqual(self._post(queued.location + "/queue").status_code, 303)
        saved_export = write_cvent_fixture(self.directory)
        self.assertEqual(self._run(cvent_dir=self.directory), 1)
        recovered = self.admin.get(queued.location)
        self.assertIn(b"Promote eligible events", recovered.data)
        self.assertIn(b"2 worker attempts", recovered.data)
        self.assertNotIn(b"data-source-run-status", recovered.data)
        self.assertEqual(self.admin.get(status_path).json, {"state": "ready", "attempts": 2})
        with closing(connect(self.database)) as connection:
            expected = json.loads(saved_export.read_text(encoding="utf-8"))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_run_rows WHERE run_id = 1").fetchone()[0], len(expected))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 0)

    def test_full_directory_preview_and_promotion_hold_ambiguous_and_low_fit_listings(self):
        manager = self.app.test_client()
        self._login(manager, "manager")
        self.assertEqual(manager.get("/assignment").status_code, 200)
        denied = manager.post("/sources/directory/export", data={"csrf_token": self._token(manager)})
        self.assertEqual(denied.status_code, 403)

        def listing(index, name, website, members):
            return {
                "name": name, "type": "National", "category": "Business", "member_count": members,
                "website": website, "contact": "Private Example", "email": "private@example.test",
                "source_url": f"https://directoryofassociations.com/view/?di=10000000-0000-4000-8000-{index:012d}",
            }

        data = [
            listing(1, "Example Business Association", "https://business.example", "5,000"),
            listing(2, "Low Membership Association", "https://small.example", "20"),
            listing(3, "Sharing an Organization Domain", "https://shared.example", "2,000"),
            listing(4, "Local Chapter", "https://shared.example/chapter", "2,000"),
            listing(5, "Blocked Association", "https://staff.cventevents.com", "9,000"),
        ]
        snapshot = self.directory / "all_associations.json"
        snapshot.write_text(json.dumps(data), encoding="utf-8")
        queued = self._post("/sources/directory/export", min_members="2000")
        self.assertEqual(queued.status_code, 303)
        self.assertEqual(self._post("/sources/directory/export").location, queued.location)
        evaluate = source_ops.evaluate_saved

        def interrupted(repository, source, row, policy, *, promote=False):
            if row["source_id"] == "10000000-0000-4000-8000-000000000003":
                raise RuntimeError("Simulated worker interruption")
            return evaluate(repository, source, row, policy, promote=promote)

        with patch("source_ops.SAVED_EXPORT_BATCH_SIZE", 2), patch("source_ops.evaluate_saved", interrupted):
            self.assertEqual(self._run(directory_dir=self.directory), 1)
        self.assertIn(b"Retry run", self.admin.get(queued.location).data)
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT last_row FROM saved_export_runs").fetchone()[0], 2)
        self.assertEqual(self._post(queued.location + "/queue").status_code, 303)
        self.assertEqual(self._run(directory_dir=self.directory / "missing"), 1)
        preview = self.admin.get(queued.location)
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"1 eligible to promote", preview.data)
        self.assertIn(b"Promote eligible listings", preview.data)
        self.assertNotIn(b"private@example.test", preview.data)
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_run_rows").fetchone()[0], 5)
            policy = connection.execute("""
                SELECT policy_json FROM source_promotion_policies WHERE source_slug = 'directory'
            """).fetchone()[0]
            self.assertEqual(json.loads(policy)["min_members"], 2000)

        self.assertEqual(self._post(queued.location + "/queue").status_code, 303)
        self.assertEqual(self._run(directory_dir=self.directory / "missing"), 1)
        finished = self.admin.get(queued.location + "?result=imported&search=Example+Business")
        self.assertIn(b"1 imported", finished.data)
        self.assertIn(b"Example Business Association", finished.data)
        self.assertNotIn(b"Low Membership Association", finished.data)
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 3)
            record = connection.execute("SELECT * FROM source_records WHERE source = 'directory'").fetchone()
            self.assertEqual(record["source_id"], "10000000-0000-4000-8000-000000000001")
            self.assertEqual(json.loads(record["raw_json"])["member_count"], "5,000")
            self.assertNotIn("private@example.test", record["raw_json"])
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_expired_lease_is_recoverable_and_schedule_is_editable(self):
        now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
        (self.directory / "all_associations.json").write_text(json.dumps([{
            "name": "Scheduled Association", "type": "National", "category": "Business",
            "member_count": "2500", "website": "https://scheduled.example",
            "source_url": "https://directoryofassociations.com/view/?di=10000000-0000-4000-8000-000000000001",
        }]), encoding="utf-8")
        with patch("source_ops.utcnow", return_value=now):
            self.assertEqual(self._post("/sources/directory/schedule", schedule_days="3").status_code, 303)
            queued = self._post("/sources/cvent/queue")
        with closing(connect(self.database)) as connection, connection:
            connection.execute("""
                UPDATE source_runs SET state = 'running', attempts = 1, lease_until = ? WHERE id = 1
            """, ((now + timedelta(days=1)).isoformat(),))
        with patch("source_ops.utcnow", return_value=now + timedelta(days=3)):
            self.assertEqual(self._run(directory_dir=self.directory), 2)
            self.assertIsNone(self._run(directory_dir=self.directory))
        self.assertIn(b"Worker stopped before finishing", self.admin.get(queued.location).data)
        with closing(connect(self.database)) as connection:
            scheduled = connection.execute("SELECT trigger, state FROM source_runs WHERE id = 2").fetchone()
            self.assertEqual(tuple(scheduled), ("scheduled", "ready"))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_run_rows WHERE run_id = 2").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 0)
        self.assertEqual(self._post("/sources/directory/schedule", schedule_days="0").status_code, 303)
        with patch("source_ops.utcnow", return_value=now + timedelta(days=6)):
            self.assertIsNone(self._run())
