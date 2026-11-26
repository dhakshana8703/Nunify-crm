import json
import re
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import cvent_import
import directory_import
import source_ops
import source_review
from app import create_app
from crm_store import connect
from workflow_store import DEMO_PASSWORD, timestamp


class SourceReviewTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1] / "instance" / f"review-test-{uuid4().hex}"
        self.folder.mkdir(parents=True)
        self.addCleanup(self._remove_files)
        self.database = self.folder / "review.sqlite3"
        self.app = create_app(self.database, demo=True)
        self.app.testing = True
        self.event_id, self.listing_id = str(uuid4()), str(uuid4()).upper()
        self.event = dict(cvent_import._record({
            "event_uuid": self.event_id, "cvent_url": f"https://web.cvent.com/event/{self.event_id}/summary",
            "event_name": "Annual Meeting", "organizer": "North Industry Association",
            "start_date": "2027-06-10", "end_date": "2027-06-10",
        }, {}, {}), duplicate=False, shared_domain=False)
        self.listing_raw = {
            "source_url": f"https://directoryofassociations.com/view/?di={self.listing_id}",
            "name": "North Industry Affiliate", "website": "https://north.example", "type": "National",
            "category": "Industry", "member_count": "10000",
        }
        self.listing = dict(directory_import._record(self.listing_raw), duplicate_id=False, shared_host=True)
        with closing(connect(self.database)) as connection, connection:
            self.account_id = connection.execute("""
                INSERT INTO accounts(identity_key, name, domain, organization_type)
                VALUES ('north', 'North Industry Association', 'north.example', 'Association')
            """).lastrowid
            self.other_id = connection.execute("""
                INSERT INTO accounts(identity_key, name, domain, organization_type)
                VALUES ('another', 'Other Organization', 'other.example', 'Association')
            """).lastrowid
            rep = connection.execute("SELECT id FROM users WHERE role = 'sdr' ORDER BY id LIMIT 1").fetchone()[0]
            connection.execute("""
                INSERT INTO account_work(account_id, owner_id, stage, revision) VALUES (?, ?, 'working', 7)
            """, (self.account_id, rep))
            connection.execute("""
                INSERT INTO people(account_id, name, email, unlocked) VALUES (?, 'Test Person', 'person@example.test', 1)
            """, (self.account_id,))
            self.cvent_run = self._seed_run(connection, "cvent", self.event)
            self.directory_run = self._seed_run(connection, "directory", self.listing)

    def _remove_files(self):
        for path in self.folder.iterdir():
            path.unlink()
        self.folder.rmdir()

    @staticmethod
    def _seed_run(connection, source, payload, *, result="review"):
        run = connection.execute("""
            INSERT INTO source_runs(source_slug, input_name, trigger, state, queued_at)
            VALUES (?, ?, 'manual', 'partial', ?)
        """, (source, payload["source_file"], timestamp())).lastrowid
        connection.execute("""
            INSERT INTO source_run_rows(run_id, row_number, payload_json, result, detail)
            VALUES (?, 1, ?, ?, 'Organization identity needs confirmation.')
        """, (run, json.dumps(payload), result))
        return run

    def _sign_in(self, role):
        client = self.app.test_client()
        response = client.get("/login")
        self.assertEqual(response.status_code, 200)
        token = self._field(response, "csrf_token")
        signed = client.post("/login", data={"email": f"{role}@example.test", "password": DEMO_PASSWORD,
                                            "csrf_token": token})
        self.assertEqual(signed.status_code, 303)
        return client

    @staticmethod
    def _field(response, field):
        return re.search(rf'name="{field}" value="([^"]+)"', response.get_data(as_text=True))[1]

    def _decision(self, client, run_id, state, **options):
        path = f"/source-review/{run_id}/1"
        detail = client.get(path)
        data = {"decision": state, "csrf_token": self._field(detail, "csrf_token"),
                "version": self._field(detail, "version"), "reason": "Reviewed the organization evidence."}
        data.update(options)
        return client.post(path, data=data)

    def test_staff_review_filters_and_stale_hold(self):
        rep = self._sign_in("riya")
        self.assertEqual(rep.get("/source-review").status_code, 403)
        with rep.session_transaction() as session:
            session["csrf"] = "fixture-token"
        self.assertEqual(rep.post(f"/source-review/{self.cvent_run}/1", data={
            "csrf_token": "fixture-token", "decision": "excluded", "reason": "Cannot change this."
        }).status_code, 403)
        manager = self._sign_in("manager")
        filtered = manager.get("/source-review?source=cvent&search=annual+meeting&sort=name&page_size=50")
        self.assertIn(b"Annual Meeting", filtered.data)
        self.assertNotIn(b"North Industry Affiliate", filtered.data)
        row = manager.get(f"/source-review/{self.cvent_run}/1")
        old_version = self._field(row, "version")
        self.assertEqual(self._decision(manager, self.cvent_run, "held", reason="").status_code, 400)
        self.assertEqual(self._decision(manager, self.cvent_run, "held").status_code, 303)
        self.assertEqual(self._decision(manager, self.cvent_run, "excluded", version=old_version).status_code, 409)
        self.assertIn(b"Annual Meeting", manager.get("/source-review?state=held").data)
        self.assertNotIn(b"Annual Meeting", manager.get(f"/source-review?run_id={self.directory_run}").data)

    def test_link_preserves_original_row_and_account_scope(self):
        admin = self._sign_in("admin")
        self.assertEqual(self._decision(admin, self.cvent_run, "linked", account_id=self.account_id,
                                        evidence_url="https://unrelated.example").status_code, 400)
        self.assertEqual(self._decision(admin, self.cvent_run, "linked", account_id=self.account_id,
                                        evidence_url="https://north.example/events").status_code, 303)
        self.assertIn(b"Annual Meeting", admin.get("/source-review?state=linked").data)
        with closing(connect(self.database)) as connection, connection:
            stored = connection.execute("""
                SELECT account_id, raw_json, details_json FROM source_records WHERE source_id = ?
            """, (self.event_id,)).fetchone()
            self.assertEqual(stored["account_id"], self.account_id)
            self.assertEqual(json.loads(stored["raw_json"]), self.event["raw"])
            self.assertEqual(json.loads(stored["details_json"])["identity_review_url"], "https://north.example/events")
            staged = connection.execute("SELECT payload_json, result FROM source_run_rows WHERE run_id = ?",
                                        (self.cvent_run,)).fetchone()
            self.assertEqual((json.loads(staged["payload_json"]), staged["result"]), (self.event, "review"))
            account = connection.execute("SELECT stage, revision, owner_id FROM account_work").fetchone()
            self.assertEqual((account["stage"], account["revision"]), ("working", 7))
            self.assertIsNotNone(account["owner_id"])
            self.assertEqual(connection.execute("SELECT unlocked FROM people").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT action FROM audit_events").fetchone()[0], "source_review_decision")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(source_review.replay_result(connection, "cvent", self.event)[0], "unchanged")
            self.assertIsNone(source_review.replay_result(connection, "cvent", dict(self.event, proof="New evidence")))
            connection.execute("""
                INSERT INTO suppression(kind, value, reason, created_at) VALUES ('domain', 'north.example', 'customer', ?)
            """, (timestamp(),))
            self.assertEqual(source_review.replay_result(connection, "cvent", self.event)[0], "suppressed")
            connection.execute("DELETE FROM suppression WHERE kind = 'domain' AND value = 'north.example'")
            next_run = self._seed_run(connection, "cvent", dict(self.event, proof="Updated source evidence"))
        self.assertEqual(self._decision(admin, next_run, "linked", account_id=self.other_id,
                                        evidence_url="https://other.example", corrected_name="Other Organization").status_code,
                         409)
        self.assertIn(b"Annual Meeting", admin.get("/source-review").data)

    def test_exclusion_stops_eligible_replay(self):
        manager = self._sign_in("manager")
        self.assertEqual(self._decision(manager, self.directory_run, "excluded").status_code, 303)
        self.assertIn(b"North Industry Affiliate", manager.get("/source-review?state=excluded").data)
        corrected = dict(self.listing_raw, name="North Industry Association")
        (self.folder / directory_import.FILE_NAME).write_text(json.dumps([corrected]), encoding="utf-8")
        with closing(connect(self.database)) as connection, connection:
            envelope = directory_import.load_rows(self.folder)[0]
            self.assertEqual(directory_import.evaluate(connection, envelope, directory_import.DEFAULT_POLICY)[0], "imported")
            self.assertEqual(source_review.replay_result(connection, "directory", envelope)[0], "review")
            self.assertIsNone(source_review.replay_result(connection, "directory", dict(envelope, duplicate_id=True)))
        admin = self._sign_in("admin")
        sources = admin.get("/sources")
        queued = admin.post("/sources/directory/queue", data={"csrf_token": self._field(sources, "csrf_token")})
        self.assertEqual(queued.status_code, 303)
        run_id = int(queued.headers["Location"].rsplit("/", 1)[1])
        with closing(connect(self.database)) as connection:
            self.assertEqual(source_ops.work_one(connection, directory_dir=self.folder), run_id)
            row = connection.execute("SELECT result, detail FROM source_run_rows WHERE run_id = ?", (run_id,)).fetchone()
            self.assertEqual(row["result"], "review")
            self.assertIn("Excluded in source review", row["detail"])
            self.assertIsNone(connection.execute("SELECT id FROM source_records WHERE source_id = ?",
                                                 (self.listing_id,)).fetchone())


if __name__ == "__main__":
    unittest.main()
