import json
import re
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app import create_app
from crm_store import connect
from suppression_projection import refresh_account_suppression
from workflow_store import DEMO_PASSWORD


class SuppressionVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1] / "instance" / f"suppression-visibility-{uuid4().hex}"
        self.folder.mkdir(parents=True)
        self.addCleanup(self.remove_files)
        self.database = self.folder / "test.sqlite3"
        self.app = create_app(self.database, demo=True)
        self.app.config.update(TESTING=True, CRM_CLOCK=lambda: datetime(2026, 11, 20, 12, tzinfo=timezone.utc))
        with closing(connect(self.database)) as connection, connection:
            self.users = dict(connection.execute("SELECT email, id FROM users"))
            self.organizer = self.add_account(connection, "Eligible Organizer", "organizer.example")
            self.blocked = self.add_account(connection, "Platform Staff Account", "division.cventevents.com")
            self.held = self.add_account(connection, "Manually Held Account", "held.example")
            self.other = self.add_account(connection, "Other Rep Account", "other.example")
            self.available = self.add_account(connection, "Unassigned Organizer", "unassigned.example")
            refresh_account_suppression(connection, (self.blocked,))
            self.riya = self.users["riya@example.test"]
            self.jordan = self.users["jordan@example.test"]
            connection.executemany("""
                INSERT INTO account_work(account_id, owner_id, stage, revision) VALUES (?, ?, 'assigned', 1)
            """, [(self.organizer, self.riya), (self.blocked, self.riya), (self.other, self.jordan)])
            self.valid_person = self.add_contact(connection, self.organizer, "Eligible Contact",
                                                 "valid@organizer.example", origin="cvent")
            self.competitor = self.add_contact(connection, self.organizer, "Platform Staff Contact", "staff@division.cvent.com")
            self.opted_out = self.add_contact(connection, self.organizer, "Email Rule Contact", "held@organizer.example")
            self.add_contact(connection, self.blocked, "Account Restricted Contact", "staff@example.test")
            self.add_contact(connection, self.other, "Other Rep Contact", "other@example.test")
            self.add_contact(connection, self.organizer, "Opted-Out Contact", "optout@organizer.example", do_not_contact=True)
            self.event_url = "https://web.cvent.com/event/organizer"
            event_record_id = connection.execute("""
                INSERT INTO source_records(source, source_id, source_url, source_file, account_id, details_json, raw_json)
                VALUES ('cvent', 'organizer-event', ?, 'saved.json', ?, ?, '{}')
            """, (self.event_url, self.organizer, json.dumps({
                "event_name": "Eligible Organizer Conference", "organizer_company": "Eligible Organizer",
                "client_domain": "event.cventevents.com", "start_date_iso": "2027-01-07", "evidence_keys": [],
            }))).lastrowid
            connection.execute("""
                INSERT INTO cvent_contact_evidence(person_id, source_record_id, observed_at)
                VALUES (?, ?, '2026-11-18T09:00:00Z')
            """, (self.valid_person, event_record_id))
            connection.execute("""
                INSERT INTO activities(account_id, person_id, actor_id, channel, outcome, direction, note, occurred_at)
                VALUES (?, ?, ?, 'note', 'noted', 'internal', 'Preserved outreach history', '2026-11-19T09:00:00Z')
            """, (self.organizer, self.opted_out, self.riya))
            self.task_id = connection.execute("""
                INSERT INTO tasks(account_id, person_id, owner_id, title, due_on)
                VALUES (?, ?, ?, 'Review old outreach', '2026-11-20')
            """, (self.organizer, self.opted_out, self.riya)).lastrowid
        admin = self.sign_in("admin")
        token = self.csrf(admin.get("/suppression"))
        for kind, value, reason_note in (
            ("account", self.held, "Account opted out"),
            ("email", "held@organizer.example", "Address opted out"),
        ):
            self.assertEqual(admin.post("/suppression", data={
                "csrf_token": token, "kind": kind, "value": value, "reason": "opt_out", "reason_note": reason_note,
            }).status_code, 303)

    def remove_files(self):
        for path in self.folder.iterdir():
            path.unlink()
        self.folder.rmdir()

    def add_account(self, connection, name, domain):
        return connection.execute("""
            INSERT INTO accounts(identity_key, name, domain, organization_type) VALUES (?, ?, ?, 'Corporate')
        """, (domain, name, domain)).lastrowid

    def add_contact(self, connection, account_id, name, email, *, do_not_contact=False, origin="apollo_offline"):
        return connection.execute("""
            INSERT INTO people(account_id, name, email, do_not_contact, origin)
            VALUES (?, ?, ?, ?, ?)
        """, (account_id, name, email, do_not_contact, origin)).lastrowid

    def csrf(self, response):
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))[1]

    def sign_in(self, actor):
        user = self.app.test_client()
        self.assertEqual(user.post("/login", data={
            "email": f"{actor}@example.test", "password": DEMO_PASSWORD,
            "csrf_token": self.csrf(user.get("/login")),
        }).status_code, 303)
        return user

    def test_default_views_scope_suppression_and_keep_event_evidence_and_history(self):
        manager = self.sign_in("manager")
        rep = self.sign_in("riya")
        ae = self.sign_in("ae")
        assignments = manager.get("/assignment")
        self.assertIn(b"Eligible Organizer", assignments.data)
        self.assertNotIn(b"Platform Staff Account", assignments.data)
        self.assertNotIn(b"Manually Held Account", assignments.data)
        inspection = manager.get("/assignment?show_suppressed=1")
        self.assertIn(b"Platform Staff Account", inspection.data)
        self.assertIn(b"Competitor", inspection.data)
        self.assertIn(b"Account opted out", inspection.data)
        self.assertNotIn(f'action="/assignment/{self.blocked}"'.encode(), inspection.data)
        owner_stage = manager.get(f"/assignment?owner={self.riya}&stage=assigned")
        self.assertIn(b"Eligible Organizer", owner_stage.data)
        self.assertNotIn(b"Other Rep Account", owner_stage.data)
        self.assertNotIn(b"Unassigned Organizer", owner_stage.data)
        self.assertNotIn(b"Platform Staff Account", owner_stage.data)

        self.assertNotIn(b"Platform Staff Account", rep.get("/my-accounts").data)
        owned_inspection = rep.get("/my-accounts?show_suppressed=1")
        self.assertIn(b"Platform Staff Account", owned_inspection.data)
        self.assertNotIn(b"Other Rep Account", owned_inspection.data)
        pool = rep.get("/my-accounts?filter=pool&show_suppressed=1")
        self.assertIn(b"Unassigned Organizer", pool.data)
        self.assertNotIn(b"Manually Held Account", pool.data)
        self.assertEqual(ae.get("/people?show_suppressed=1").status_code, 403)

        default_people = rep.get("/people")
        self.assertIn(b"Eligible Contact", default_people.data)
        self.assertTrue(b"<p>Cvent event</p>" in default_people.data, "Cvent contact origin is not labeled.")
        self.assertIn(b"Email unverified", default_people.data)
        self.assertNotIn(b"Platform Staff Contact", default_people.data)
        self.assertNotIn(b"Email Rule Contact", default_people.data)
        self.assertNotIn(b"Opted-Out Contact", default_people.data)
        inspected_people = rep.get("/people?show_suppressed=1")
        self.assertIn(b"Platform Staff Contact", inspected_people.data)
        self.assertIn(b"Account Restricted Contact", inspected_people.data)
        self.assertIn(b"Email Rule Contact", inspected_people.data)
        self.assertIn(b"Address opted out", inspected_people.data)
        self.assertIn(b"Contact marked do not contact", inspected_people.data)
        self.assertNotIn(b"Other Rep Contact", inspected_people.data)
        self.assertNotIn(b"held@organizer.example", inspected_people.data)
        self.assertIn(b"Other Rep Contact", manager.get("/people").data)

        detail_url = f"/accounts/{self.organizer}"
        detail = rep.get(detail_url)
        self.assertIn(b"Eligible Organizer Conference", detail.data)
        self.assertIn(b"event.cventevents.com", detail.data)
        self.assertIn(b"Preserved outreach history", detail.data)
        self.assertNotIn(b"Platform Staff Contact", detail.data)
        contacts_section = detail.get_data(as_text=True).split('id="contacts-heading"', 1)[1].split("</section>", 1)[0]
        self.assertIn(f'href="{self.event_url}"', contacts_section)
        self.assertIn('datetime="2026-11-18T09:00:00Z"', contacts_section)
        self.assertNotIn(b"Review old outreach", rep.get("/today").data)
        self.assertIn(b"Review old outreach", detail.data)
        visible_contacts = rep.get(detail_url + "?show_suppressed=1")
        self.assertIn(b"Platform Staff Contact", visible_contacts.data)
        self.assertNotIn(f'<option value="{self.competitor}">'.encode(), visible_contacts.data)

    def test_suppression_blocks_direct_assignment_outreach_and_unlock(self):
        manager = self.sign_in("manager")
        rep = self.sign_in("riya")
        token = self.csrf(manager.get("/assignment"))
        self.assertEqual(manager.post(f"/assignment/{self.held}", data={
            "csrf_token": token, "owner_id": self.jordan, "revision": 0,
        }).status_code, 409)
        self.assertEqual(manager.post("/assignment/bulk", data={
            "csrf_token": token, "owner_id": self.jordan, "account": [f"{self.available}:0", f"{self.held}:0"],
        }).status_code, 409)
        self.assertEqual(manager.post("/assignment/round-robin", data={
            "csrf_token": token, "rep_id": self.jordan, "cap": 5, "account": f"{self.held}:0",
        }).status_code, 409)
        detail_url = f"/accounts/{self.organizer}"
        rep_token = self.csrf(rep.get(detail_url))
        self.assertEqual(rep.post(detail_url + "/activities", data={
            "csrf_token": rep_token, "revision": 1, "channel": "email", "outcome": "sent",
            "person_id": self.competitor, "action_key": "blocked-activity",
        }).status_code, 403)
        self.assertEqual(rep.post(detail_url + "/enrich", data={
            "csrf_token": rep_token, "person_id": self.competitor,
        }).status_code, 409)
        self.assertEqual(rep.post(f"/tasks/{self.task_id}", data={
            "csrf_token": rep_token, "action": "snooze",
        }).status_code, 403)
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT owner_id FROM account_work WHERE account_id = ?", (self.available,),
            ).fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM credit_jobs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM activities WHERE request_key = 'blocked-activity'"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
