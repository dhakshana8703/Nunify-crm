import json
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app import create_app
from cvent_import import FILE_NAME
from crm_store import connect
from source_ops import work_one
from test_workflow import RenderedForm
from workflow_store import DEMO_PASSWORD


class AdminSDRFlowTest(unittest.TestCase):
    def setUp(self):
        instance = Path(__file__).resolve().parents[1] / "instance"
        self.scratch = instance / f"admin-sdr-flow-{uuid4().hex}"
        self.scratch.mkdir(parents=True)
        self.addCleanup(self.remove_test_files)
        self.database = self.scratch / "test.sqlite3"
        self.app = create_app(self.database, demo=True)
        self.clock = datetime(2027, 1, 1, 20, tzinfo=timezone.utc)
        self.app.config.update(TESTING=True, CRM_CLOCK=lambda: self.clock)

    def remove_test_files(self):
        for file in self.scratch.iterdir():
            file.unlink()
        self.scratch.rmdir()

    def form(self, client, page, action):
        response = client.get(page)
        self.assertEqual(response.status_code, 200)
        form = RenderedForm(action)
        form.feed(response.get_data(as_text=True))
        self.assertTrue(form.found, f"Missing POST form for {action}")
        self.assertTrue(form.hidden.get("csrf_token"))
        return form.hidden

    def login(self, username, landing):
        client = self.app.test_client()
        response = client.post("/login", data={
            **self.form(client, "/login", "/login"),
            "email": f"{username}@example.test", "password": DEMO_PASSWORD,
        })
        self.assertEqual((response.status_code, response.location), (303, landing))
        return client

    def work_run(self, run_path):
        with closing(connect(self.database)) as db:
            self.assertEqual(work_one(db, cvent_dir=self.scratch), int(run_path.rsplit("/", 1)[1]))

    def preview(self, admin):
        action = "/sources/cvent/queue"
        response = admin.post(action, data=self.form(admin, "/sources", action))
        self.assertEqual(response.status_code, 303)
        run_path = response.location
        self.assertIn(b"Queued", admin.get(run_path).data)
        self.work_run(run_path)
        self.assertIn(b"Promote eligible events", admin.get(run_path).data)
        return run_path

    def promote(self, admin, run_path):
        action = run_path + "/queue"
        response = admin.post(action, data=self.form(admin, run_path, action))
        self.assertEqual((response.status_code, response.location), (303, run_path))
        self.work_run(run_path)
        self.assertIn(b"Partial", admin.get(run_path).data)

    def test_saved_source_replay_preserves_assignment_and_follow_up(self):
        event_id = "10000000-0000-4000-8000-000000000001"
        review_id = "10000000-0000-4000-8000-000000000002"
        (self.scratch / FILE_NAME).write_text(json.dumps([
            {"event_uuid": event_id, "cvent_url": f"https://web.cvent.com/event/{event_id}/summary",
             "organizer": "Example Trade Association", "company_domain": "trade.example",
             "event_name": "Example Annual Meeting", "start_date": "2027-01-14"},
            {"event_uuid": review_id, "cvent_url": f"https://web.cvent.com/event/{review_id}/summary",
             "organizer": "Unverified Local Chapter", "client_domain": "web.cvent.com",
             "event_name": "Unverified Meeting"},
        ]), encoding="utf-8")
        admin = self.login("admin", "/sources")
        manager = self.login("manager", "/assignment")
        rep = self.login("riya", "/today")
        peer = self.login("jordan", "/today")

        run_path = self.preview(admin)
        self.assertEqual(rep.get(run_path + "/status").status_code, 403)
        with closing(connect(self.database, read_only=True)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)

        review = f"/source-review/{run_path.rsplit('/', 1)[1]}/2"
        held = admin.post(review, data={**self.form(admin, review, review), "decision": "held",
                                         "reason": "Organizer website still needs confirmation."})
        self.assertEqual(held.status_code, 303)
        self.promote(admin, run_path)
        self.assertIn(b"Held", admin.get(review).data)
        with closing(connect(self.database, read_only=True)) as db:
            account_id, = db.execute("SELECT id FROM accounts").fetchone()
            riya_id = db.execute("SELECT id FROM users WHERE email = 'riya@example.test'").fetchone()[0]
            evidence_before = db.execute("SELECT id, source, source_id, account_id FROM source_records").fetchall()
            self.assertEqual([tuple(row)[1:] for row in evidence_before], [("cvent", event_id, account_id)])

        account_path = f"/accounts/{account_id}"
        self.assertEqual(rep.get(account_path).status_code, 403)
        assign_page = f"/assignment?company={account_id}"
        assign_action = f"/assignment/{account_id}?company={account_id}"
        assigned = manager.post(assign_action, data={**self.form(manager, assign_page, assign_action),
                                                      "owner_id": riya_id})
        self.assertEqual(assigned.status_code, 303)
        self.assertIn(b"Newly assigned", rep.get("/today").data)
        account = rep.get(account_path)
        self.assertIn(b'<li class="source-label">Cvent</li>', account.data)
        self.assertIn(b"Example Annual Meeting", account.data)
        self.assertIn(b'name="due_tomorrow" value="1"', account.data)
        self.assertEqual(peer.get(account_path).status_code, 403)

        action = account_path + "/activities"
        activity = {**self.form(rep, account_path, action), "channel": "call", "outcome": "no_answer",
                    "due_tomorrow": "1", "next_step": "Call after the holiday"}
        # The rep leaves the form open across midnight in Asia/Kolkata.
        self.clock = datetime(2027, 1, 2, 20, tzinfo=timezone.utc)
        self.assertEqual(rep.post(action, data=activity).status_code, 303)
        self.assertEqual(rep.post(action, data=activity).status_code, 303)
        self.assertEqual(rep.post(action, data={**activity, "next_step": "Changed on replay"}).status_code, 409)

        replay = self.preview(admin)
        self.promote(admin, replay)
        self.assertEqual(rep.get("/today?bucket=tomorrow").data.count(b"Call after the holiday"), 1)
        self.assertIn(b'<li class="source-label">Cvent</li>', rep.get(account_path).data)
        self.assertIn(b"Call after the holiday", manager.get(account_path).data)
        self.assertNotIn(b"Call after the holiday", peer.get("/today?bucket=tomorrow").data)
        self.clock = datetime(2027, 1, 3, 20, tzinfo=timezone.utc)
        self.assertEqual(rep.post(action, data=activity).status_code, 303)
        with closing(connect(self.database, read_only=True)) as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT id FROM accounts")], [(account_id,)])
            evidence = db.execute("SELECT id, source, source_id, account_id FROM source_records").fetchall()
            self.assertEqual(evidence, evidence_before)
            self.assertEqual(db.execute("SELECT owner_id FROM account_work").fetchone()[0], riya_id)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM activities").fetchone()[0], 1)
            task = db.execute("SELECT account_id, owner_id, due_on FROM tasks").fetchall()
            self.assertEqual([tuple(row) for row in task], [(account_id, riya_id, "2027-01-04")])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM credit_jobs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
