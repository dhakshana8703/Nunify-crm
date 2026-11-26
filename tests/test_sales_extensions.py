import re
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from app import create_app
from crm_store import connect, import_snapshot
from sales_extensions import DeliveryFailure, OfflineAEProvider, deliver_one
from workflow_store import DEMO_PASSWORD, timestamp


class SalesExtensionsTest(unittest.TestCase):
    def setUp(self):
        instance = Path(__file__).resolve().parents[1] / "instance"
        instance.mkdir(exist_ok=True)
        self.test_directory = instance / f"test-sales-{uuid4().hex}"
        self.test_directory.mkdir()
        self.addCleanup(self.remove_test_files)
        self.database = self.test_directory / "workflow.sqlite3"
        self.account_id = import_snapshot(self.database, {
            "aarp": {"name": "AARP", "domain": "aarp.org", "organization_type": "Association"},
        }, [])["accounts"][0]["id"]
        self.app = create_app(self.database, demo=True)
        self.app.testing = True
        self.now = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
        self.app.config["CRM_CLOCK"] = lambda: self.now
        with closing(connect(self.database)) as connection, connection:
            self.users = {row["email"]: row["id"] for row in connection.execute("SELECT id, email FROM users")}
            self.person_id = connection.execute("""
                INSERT INTO people(account_id, name, email, origin)
                VALUES (?, 'Test event contact', 'planner@example.test', 'demo')
            """, (self.account_id,)).lastrowid
            connection.execute("""
                INSERT INTO account_work (account_id, owner_id, revision, stage, assigned_at)
                VALUES (?, ?, 1, 'assigned', ?)
                ON CONFLICT (account_id) DO UPDATE SET owner_id = excluded.owner_id, revision = 1,
                                                    stage = 'assigned', assigned_at = excluded.assigned_at
            """, (self.account_id, self.users["riya@example.test"], timestamp(self.now)))

    def remove_test_files(self):
        for path in self.test_directory.iterdir():
            path.unlink()
        self.test_directory.rmdir()

    def login(self, email):
        client = self.app.test_client()
        response = client.get("/login")
        csrf = self.csrf(response.data)
        signed_in = client.post("/login", data={"email": email, "password": DEMO_PASSWORD, "csrf_token": csrf})
        self.assertIn(signed_in.status_code, (302, 303))
        return client

    def csrf(self, data):
        token = re.search(rb'name="csrf_token"\s+value="([^"]+)"', data)
        self.assertIsNotNone(token, data.decode()[:500])
        return token[1].decode()

    def test_timed_coverage_works_for_a_separate_rep_and_preserves_owner(self):
        account_url = f"/accounts/{self.account_id}"
        manager = self.login("manager@example.test")
        owner = self.login("riya@example.test")
        cover = self.login("jordan@example.test")
        self.assertEqual(cover.get(account_url).status_code, 403)
        values = {
            "csrf_token": self.csrf(manager.get(account_url).data), "revision": 1,
            "rep_id": self.users["jordan@example.test"], "starts_at": "2026-11-20T15:00",
            "ends_at": "2026-11-20T16:00",
        }
        forbidden = dict(values, csrf_token=self.csrf(owner.get(account_url).data))
        self.assertEqual(owner.post(account_url + "/coverages", data=forbidden).status_code, 403)
        self.assertEqual(manager.post(account_url + "/coverages", data=values).status_code, 303)
        self.assertEqual(manager.post(account_url + "/coverages", data=values).status_code, 409)
        covered_account = cover.get(account_url)
        self.assertEqual(covered_account.status_code, 200)
        self.assertIn(b"Jordan Lee covers for Riya Rao", covered_account.data)
        self.assertIn(b"Active", covered_account.data)
        logged = cover.post(account_url + "/activities", data={
            "csrf_token": self.csrf(covered_account.data), "revision": 1, "action_key": "cover-inbound-1",
            "person_id": self.person_id, "channel": "email", "outcome": "replied",
            "note": "I recorded the inbound reply while Riya was away.",
            "next_step": "Send requested registration options", "due_on": "2026-11-21",
        })
        self.assertEqual(logged.status_code, 303)
        covered_queue = cover.get("/today").data
        self.assertIn(b"Unanswered reply", covered_queue)
        self.assertNotIn(b"Send requested registration options", cover.get("/today?bucket=tomorrow").data)

        self.now += timedelta(minutes=30)
        self.assertEqual(cover.get(account_url).status_code, 403)
        self.assertEqual(owner.get(account_url).status_code, 200)
        self.assertIn(b"Send requested registration options", owner.get("/today?bucket=tomorrow").data)
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual(connection.execute("SELECT owner_id FROM account_work").fetchone()[0],
                             self.users["riya@example.test"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM coverages").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT actor_id FROM audit_events WHERE action = 'coverage_created'").fetchone()[0],
                             self.users["manager@example.test"])
            self.assertEqual(connection.execute("SELECT actor_id FROM activities").fetchone()[0],
                             self.users["jordan@example.test"])
            self.assertEqual(connection.execute("SELECT owner_id FROM tasks").fetchone()[0], self.users["riya@example.test"])

    def test_handoff_retry_reuses_a_receipt_after_provider_accepted_request(self):
        account_url = f"/accounts/{self.account_id}"
        rep = self.login("riya@example.test")
        manager = self.login("manager@example.test")
        ae = self.login("ae@example.test")
        other_rep = self.login("jordan@example.test")
        with closing(connect(self.database)) as connection, connection:
            connection.execute("""
                INSERT INTO activities (account_id, person_id, actor_id, channel, outcome, direction, note, occurred_at)
                VALUES (?, ?, ?, 'email', 'replied', 'inbound', 'They asked about event registration.', ?)
            """, (self.account_id, self.person_id, self.users["riya@example.test"], timestamp(self.now)))
        values = {
            "csrf_token": self.csrf(rep.get(account_url).data), "revision": 1,
            "person_id": self.person_id, "ae_id": self.users["ae@example.test"], "outcome": "demo_requested",
            "context": "The director needs to see the event registration flow.",
            "next_step": "Prepare a product demo for the buyer and their ops lead.",
        }
        queued = rep.post(account_url + "/handoffs", data=values)
        self.assertEqual(queued.status_code, 303)
        detail = urlsplit(queued.location).path
        self.assertEqual(rep.post(account_url + "/handoffs", data=values).location, queued.location)
        self.assertIn(b"Pending delivery", manager.get(detail).data)
        self.assertEqual(ae.get(detail).status_code, 403)
        self.assertEqual(other_rep.get(detail).status_code, 403)

        with closing(connect(self.database)) as connection:
            offline = OfflineAEProvider(connection)

            class AcceptedThenUnreachable:
                def deliver(self, key, payload):
                    offline.deliver(key, payload)
                    raise DeliveryFailure("The demo provider accepted the request before a timeout.")

            self.assertEqual(deliver_one(connection, AcceptedThenUnreachable())["status"], "failed")

        failed = manager.get(detail)
        self.assertIn(b"accepted the request before a timeout", failed.data)
        self.assertIn(b"Retry delivery", failed.data)
        self.assertNotIn(b"Retry delivery", rep.get(detail).data)
        self.assertEqual(rep.post(detail + "/retry", data={"csrf_token": values["csrf_token"]}).status_code, 403)
        self.assertEqual(manager.post(detail + "/retry", data={"csrf_token": self.csrf(failed.data)}).status_code, 303)

        with closing(connect(self.database)) as connection:
            self.assertEqual(deliver_one(connection, OfflineAEProvider(connection))["status"], "delivered")
            self.assertIsNone(deliver_one(connection, OfflineAEProvider(connection)))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM offline_handoff_receipts").fetchone()[0], 1)
            row = connection.execute("SELECT attempts, status FROM handoffs").fetchone()
            self.assertEqual((row["attempts"], row["status"]), (2, "delivered"))
            self.assertEqual(connection.execute("SELECT stage FROM account_work").fetchone()[0], "handed_off")
        delivered = ae.get(detail)
        self.assertIn(b"Delivered", delivered.data)
        self.assertIn(b"Prepare a product demo", delivered.data)
        self.assertIn(b"They asked about event registration", delivered.data)

    def test_meeting_qualification_and_suppression_before_delivery(self):
        account_url = f"/accounts/{self.account_id}"
        rep = self.login("riya@example.test")
        manager = self.login("manager@example.test")
        values = {
            "csrf_token": self.csrf(rep.get(account_url).data), "revision": 1,
            "person_id": self.person_id, "ae_id": self.users["ae@example.test"], "outcome": "meeting_booked",
            "meeting_at": "2026-11-25T16:00", "context": "The contact confirmed Wednesday's meeting.",
            "next_step": "Show the demo registration page at the meeting.",
        }
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE people SET persona = ''")
        self.assertEqual(rep.post(account_url + "/handoffs", data=values).status_code, 400)
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE people SET persona = 'P1'")
        queued = rep.post(account_url + "/handoffs", data=values)
        self.assertEqual(queued.status_code, 303)
        with closing(connect(self.database)) as connection, connection:
            connection.execute("""
                INSERT INTO suppression (kind, value, reason, reason_note, created_at)
                VALUES ('account', ?, 'manual', 'Manual review', ?)
            """, (str(self.account_id), timestamp(self.now)))

        calls = []

        class ProviderSpy:
            def deliver(self, key, payload):
                calls.append((key, payload))
                return "test-receipt"

        with closing(connect(self.database)) as connection:
            self.assertEqual(deliver_one(connection, ProviderSpy())["status"], "failed")
            self.assertEqual(connection.execute("SELECT attempts FROM handoffs").fetchone()[0], 0)
        self.assertEqual(calls, [])
        failed = manager.get(queued.location)
        self.assertIn(b"Outreach became restricted", failed.data)
        self.assertIn(b"Retry delivery", failed.data)


if __name__ == "__main__":
    unittest.main()
