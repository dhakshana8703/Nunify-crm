import hashlib
import hmac
import json
import re
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app import create_app
from crm_store import connect, import_snapshot
from workflow_store import DEMO_PASSWORD, timestamp


class CampaignsTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / "instance" / f"test-campaigns-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(self.remove_files)
        self.database = self.directory / "campaigns.sqlite3"
        self.account_id = import_snapshot(self.database, {
            "aarp": {"name": "AARP", "domain": "aarp.org", "organization_type": "Association"},
        }, [{
            "source": "cvent", "source_id": "demo-conference", "source_url": "https://example.test/conference",
            "source_file": "campaign-evidence.json", "identity_key": "aarp", "reason": None,
            "details": {"event_name": "Annual Conference", "start_date_iso": "2027-04-20"},
            "raw": {"event_name": "Annual Conference"},
        }])["accounts"][0]["id"]
        self.app = create_app(self.database, demo=True)
        self.app.testing = True
        self.now = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
        self.app.config["CRM_CLOCK"] = lambda: self.now
        self.secret = "test-sequencer-authentication"
        self.app.config["CRM_SEQ_WEBHOOK_SECRET"] = self.secret
        with closing(connect(self.database)) as connection, connection:
            owner_id = connection.execute("SELECT id FROM users WHERE email = 'riya@example.test'").fetchone()[0]
            connection.execute("""
                INSERT INTO account_work (account_id, owner_id, revision, stage, assigned_at)
                VALUES (?, ?, 1, 'assigned', ?)
            """, (self.account_id, owner_id, timestamp(self.now)))
            self.person_id = connection.execute("""
                INSERT INTO people(account_id, name, title, email, persona, origin)
                VALUES (?, 'Jamie Demo', 'Director of Events', 'jamie@example.test', 'P1', 'demo')
            """, (self.account_id,)).lastrowid

    def remove_files(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def csrf(self, page):
        token = re.search(rb'name="csrf_token"\s+value="([^"]+)"', page.data)
        self.assertIsNotNone(token)
        return token[1].decode()

    def login(self, email):
        client = self.app.test_client()
        token = self.csrf(client.get("/login"))
        result = client.post("/login", data={"email": email, "password": DEMO_PASSWORD, "csrf_token": token})
        self.assertIn(result.status_code, (302, 303))
        return client

    def post_event(self, client, member_id, event_id, event_type, *, note=None, signed=True):
        event = {"member_id": member_id, "event_id": event_id, "type": event_type}
        if note is not None:
            event["note"] = note
        body = json.dumps(event).encode()
        headers = {"X-Nunify-Signature": "sha256=" + hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()} if signed else {}
        return client.post("/api/sequencer/events", data=body, content_type="application/json", headers=headers)

    def test_manager_saves_view_and_rep_enrollment_and_webhooks_are_scoped(self):
        manager = self.login("manager@example.test")
        rep = self.login("riya@example.test")
        other_rep = self.login("jordan@example.test")
        inbox = self.app.test_client()
        manager_token = self.csrf(manager.get("/campaigns"))
        view = manager.post("/campaigns/views", data={
            "csrf_token": manager_token, "name": "Association accounts", "organization_type": "Association",
            "platform": "cvent",
        })
        self.assertEqual(view.status_code, 303)
        self.assertIn(b"AARP", manager.get(view.location).data)
        self.assertEqual(rep.get(view.location).status_code, 403)
        campaign = manager.post("/campaigns", data={
            "csrf_token": manager_token, "name": "Fall conference planning", "view_id": view.location.rsplit("/", 1)[-1],
        })
        second = manager.post("/campaigns", data={"csrf_token": manager_token, "name": "A separate campaign"})
        self.assertEqual(campaign.status_code, 303)
        self.assertEqual(second.status_code, 303)
        self.assertIn(b"Live sequencer connection pending", rep.get(campaign.location).data)
        payload = {"csrf_token": self.csrf(rep.get(campaign.location)), "person_id": self.person_id}
        enroll = campaign.location + "/members"
        self.assertEqual(rep.post(enroll, data=payload).status_code, 403)
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE people SET unlocked = 1, email_verified = 1 WHERE id = ?", (self.person_id,))
        self.assertEqual(other_rep.post(enroll, data=dict(payload, csrf_token=self.csrf(other_rep.get(campaign.location)))).status_code, 403)
        self.assertEqual(rep.post(enroll, data=payload).status_code, 303)
        self.assertEqual(rep.post(enroll, data=payload).status_code, 303)
        self.assertEqual(rep.post(second.location + "/members", data=payload).status_code, 409)
        self.assertIn(b"Jamie Demo", manager.get(campaign.location).data)
        self.assertNotIn(b"Jamie Demo", other_rep.get(campaign.location).data)
        with closing(connect(self.database)) as connection:
            member_id = connection.execute("SELECT id FROM campaign_members").fetchone()[0]
        self.assertEqual(self.post_event(inbox, member_id, "reply-1", "replied", signed=False).status_code, 401)
        first_reply = self.post_event(inbox, member_id, "reply-1", "replied", note="Tell us how registration works.")
        self.assertEqual(first_reply.status_code, 201)
        repeated = self.post_event(inbox, member_id, "reply-1", "replied", note="Tell us how registration works.")
        self.assertEqual(repeated.get_json(), {"status": "already_recorded"})
        self.assertEqual(self.post_event(inbox, member_id, "reply-1", "unsubscribed").status_code, 409)
        self.assertIn(b"Unanswered reply", rep.get("/today").data)
        self.assertNotIn(b"Unanswered reply", other_rep.get("/today").data)
        self.assertIn(b"Paused", rep.get(campaign.location).data)
        self.assertEqual(self.post_event(inbox, member_id, "opt-out-1", "unsubscribed").status_code, 201)
        self.assertEqual(rep.post(second.location + "/members", data=payload).status_code, 403)
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM activities").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sequence_events").fetchone()[0], 2)
            actor = connection.execute("""
                SELECT u.name, u.active FROM activities a JOIN users u ON u.id = a.actor_id
                WHERE a.outcome = 'replied'
            """).fetchone()
            self.assertEqual(tuple(actor), ("Sequencer integration", 0))
            contact = connection.execute("SELECT status, do_not_contact FROM people WHERE id = ?", (self.person_id,)).fetchone()
            self.assertEqual(tuple(contact), ("opted_out", 1))
            rules = connection.execute("SELECT reason, source FROM suppression WHERE kind = 'email'").fetchall()
            self.assertEqual([tuple(rule) for rule in rules], [("opt_out", "sequencer")])


if __name__ == "__main__":
    unittest.main()
