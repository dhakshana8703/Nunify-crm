import json
import re
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app import create_app
from crm_store import connect
from workflow_store import DEMO_PASSWORD


class ContactWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / "instance" / f"test-contact-{uuid4().hex}"
        self.directory.mkdir()
        self.addCleanup(self.clean_up)
        self.database = self.directory / "contacts.sqlite3"
        self.app = create_app(self.database, demo=True)
        self.app.testing = True
        self.app.config["CRM_CLOCK"] = lambda: datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
        with closing(connect(self.database)) as connection, connection:
            owners = dict(connection.execute("SELECT email, id FROM users"))
            self.account = self.create_account(connection, "Atlas Research", owners["riya@example.test"])
            self.other_account = self.create_account(connection, "Pacific Forum", owners["jordan@example.test"])
        self.rep = self.sign_in("riya")
        self.peer = self.sign_in("jordan")
        self.manager = self.sign_in("manager")
        self.account_url = f"/accounts/{self.account}"

    def clean_up(self):
        for file in self.directory.iterdir():
            file.unlink()
        self.directory.rmdir()

    def create_account(self, connection, name, owner_id):
        key = name.lower().replace(" ", "-")
        account_id = connection.execute("""
            INSERT INTO accounts(identity_key, name, domain, organization_type) VALUES (?, ?, ?, 'Association')
        """, (key, name, key + ".test")).lastrowid
        connection.execute("INSERT INTO account_work(account_id, owner_id, revision) VALUES (?, ?, 1)",
                           (account_id, owner_id))
        return account_id

    def sign_in(self, user):
        client = self.app.test_client()
        page = client.get("/login")
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)[1].decode()
        self.assertEqual(client.post("/login", data={
            "email": f"{user}@example.test", "password": DEMO_PASSWORD, "csrf_token": csrf,
        }).status_code, 303)
        self.assertEqual(client.get("/people").status_code, 200)
        return client

    def post(self, client, path, **fields):
        with client.session_transaction() as session:
            csrf = session["csrf"]
        return client.post(path, data={"csrf_token": csrf, "revision": 1, **fields})

    def add_contact(self, name, **fields):
        response = self.post(self.rep, self.account_url + "/contacts", name=name, **fields)
        self.assertEqual(response.status_code, 303)
        with closing(connect(self.database, read_only=True)) as connection:
            return connection.execute("SELECT id FROM people WHERE account_id = ? AND name = ?",
                                      (self.account, name)).fetchone()[0]

    def test_contact_page_scopes_details_timeline_and_tasks(self):
        target = self.add_contact("Leena Shah", title="Programme director", phone="+91 20 1234 5678",
                                  email="leena@example.test", linkedin_url="http://uk.linkedin.com/in/LEENA-SHAH/?trk=page",
                                  persona="P2")
        other = self.add_contact("Second Contact")
        for person, note, step in ((target, "Discussed speaker scheduling", "Send session plan"),
                                   (other, "Other contact conversation", "Other contact task")):
            self.assertEqual(self.post(self.rep, self.account_url + "/activities", channel="call", outcome="connected",
                                      note=note, person_id=person, due_on="2026-11-21", next_step=step,
                                      action_key=f"call-{person}").status_code, 303)
        path = f"{self.account_url}/contacts/{target}"
        self.assertIn(path.encode(), self.rep.get("/people").data)
        self.assertIn(path.encode(), self.rep.get(self.account_url).data)
        response = self.rep.get(path)
        self.assertEqual(response.status_code, 200)
        for label in (b"Atlas Research", b"Leena Shah", b"Programme director", b"P2", b"leena@example.test",
                      b"Manual entry", b"+91 20 1234 5678", b"https://www.linkedin.com/in/leena-shah",
                      b"Discussed speaker scheduling", b"Send session plan"):
            self.assertIn(label, response.data)
        self.assertNotIn(b"Other contact conversation", response.data)
        self.assertNotIn(b"Other contact task", response.data)
        self.assertEqual(self.peer.get(path).status_code, 403)
        self.assertEqual(self.manager.get(f"/accounts/{self.other_account}/contacts/{target}").status_code, 404)
        selected = re.search(rb'<select name="person_id">(.*?)</select>',
                             self.rep.get(f"{self.account_url}?person_id={target}").data, re.S)[1]
        self.assertIn(f'<option value="{target}" selected>'.encode(), selected)

    def test_create_edit_deduplication_revisions_and_scope(self):
        target = self.add_contact("Leena Shah", email="leena@example.test", linkedin_url="linkedin.com/in/Leena-Shah")
        other = self.add_contact("Second Contact", email="second@example.test")
        base = self.account_url + "/contacts"
        self.assertEqual(self.post(self.rep, base, name="Duplicate Email", email="LEENA@EXAMPLE.TEST").status_code, 409)
        self.assertEqual(self.post(self.rep, base, name="Duplicate Profile",
                                  linkedin_url="https://uk.linkedin.com/in/leena-shah/#bio").status_code, 409)
        self.assertEqual(self.post(self.rep, base, name="Untrusted Profile",
                                  linkedin_url="https://linkedin.com.example.test/in/leena-shah").status_code, 400)
        self.assertEqual(self.post(self.manager, f"/accounts/{self.other_account}/contacts", name="Shared Person",
                                  linkedin_url="https://www.linkedin.com/in/leena-shah").status_code, 303)
        path = f"{base}/{target}"
        self.assertEqual(self.post(self.peer, path, details_revision=0, phone="999").status_code, 403)
        self.assertEqual(self.post(self.rep, path, details_revision=0, revision=0, phone="999").status_code, 409)
        self.assertEqual(self.post(self.rep, f"{base}/{other}", details_revision=0,
                                  linkedin_url="https://www.linkedin.com/in/leena-shah").status_code, 409)
        self.assertEqual(self.post(self.rep, path, details_revision=0, phone="+91 5555 0001", title="Operations lead").status_code, 303)
        self.assertEqual(self.post(self.rep, path, details_revision=0, title="Stale edit").status_code, 409)
        with closing(connect(self.database, read_only=True)) as connection:
            saved = connection.execute("SELECT title, phone, email, origin, details_revision FROM people WHERE id = ?",
                                       (target,)).fetchone()
            audit = connection.execute("SELECT actor_id, details FROM audit_events WHERE action = 'contact_edit'").fetchone()
            owner = connection.execute("SELECT owner_id FROM account_work WHERE account_id = ?", (self.account,)).fetchone()[0]
        self.assertEqual(tuple(saved), ("Operations lead", "+91 5555 0001", "leena@example.test", "manual", 1))
        self.assertEqual(audit["actor_id"], owner)
        self.assertEqual(json.loads(audit["details"]), {"person_id": target, "fields": ["title", "phone"]})

    def test_suppression_and_source_tied_email_survive_edits(self):
        target = self.add_contact("Saved Event Contact", email="event@example.test", phone="+91 5555 0002",
                                  linkedin_url="https://www.linkedin.com/in/event-contact")
        path = f"{self.account_url}/contacts/{target}"
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE people SET origin = 'cvent', unlocked = 1, email_verified = 1 WHERE id = ?", (target,))
        self.assertNotIn(b'name="email"', self.rep.get(path).data)
        self.assertEqual(self.post(self.rep, path, details_revision=0, email="replacement@example.test").status_code, 409)
        self.assertEqual(self.post(self.rep, path, details_revision=0, title="Updated event role").status_code, 303)
        self.assertEqual(self.post(self.rep, self.account_url + "/activities", channel="call", outcome="connected",
                                  person_id=target, action_key="call-fresh-outreach-status").status_code, 303)
        self.assertEqual(self.post(self.rep, path + "/status", details_revision=1, status="wrong_person",
                                  expected_status="not_contacted").status_code, 409)
        self.assertEqual(self.post(self.rep, path + "/status", details_revision=1, status="wrong_person",
                                  expected_status="connected").status_code, 303)
        self.assertIn(b"Wrong person", self.rep.get(path).data)
        self.assertEqual(self.post(self.rep, path + "/status", details_revision=2, status="do_not_contact",
                                  expected_status="wrong_person").status_code, 303)
        self.assertEqual(self.rep.get(path).status_code, 404)
        blocked = self.rep.get(path + "?show_suppressed=1")
        self.assertEqual(blocked.status_code, 200)
        self.assertIn(b"Suppressed contact", blocked.data)
        for hidden in (b"event@example.test", b"+91 5555 0002", b"/in/event-contact", b"Edit details"):
            self.assertNotIn(hidden, blocked.data)
        self.assertEqual(self.peer.get(path + "?show_suppressed=1").status_code, 403)
        self.assertEqual(self.post(self.rep, path + "?show_suppressed=1", details_revision=3,
                                  email="replacement@example.test").status_code, 403)
        self.assertEqual(self.post(self.rep, path + "/status", details_revision=3, status="not_contacted",
                                  expected_status="do_not_contact").status_code, 403)
        with closing(connect(self.database, read_only=True)) as connection:
            saved = connection.execute("SELECT email, unlocked, email_verified, do_not_contact FROM people WHERE id = ?", (target,)).fetchone()
            rule = connection.execute("SELECT reason FROM suppression WHERE kind = 'email' AND value = 'event@example.test'").fetchone()
        self.assertEqual(tuple(saved), ("event@example.test", 1, 1, 1))
        self.assertEqual(rule["reason"], "Marked do not contact")
