from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4
import unittest

from app import create_app
from crm_store import connect
from import_demo import SOURCE_PARENT, import_demo
from workflow_store import DEMO_PASSWORD


class RenderedForm(HTMLParser):
    def __init__(self, action):
        super().__init__()
        self.action = action
        self.found = False
        self.inside = False
        self.hidden = {}

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "form":
            self.inside = attrs.get("action") == self.action and attrs.get("method") == "post"
            self.found |= self.inside
        if self.inside and tag == "input" and attrs.get("type") == "hidden" and attrs.get("name"):
            self.hidden[attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False


class RepWorkflowTest(unittest.TestCase):
    def setUp(self):
        instance = Path(__file__).resolve().parents[1] / "instance"
        self.test_dir = instance / f"workflow-test-{uuid4().hex}"
        self.test_dir.mkdir(parents=True)
        self.addCleanup(self.remove_test_files)
        self.database = self.test_dir / "demo.sqlite3"
        result = import_demo(self.database, SOURCE_PARENT / "cvent-scrapper", SOURCE_PARENT / "association")
        self.aarp = next(row["id"] for row in result["accounts"] if row["identity_key"] == "aarp")
        self.ctaa = next(row["id"] for row in result["accounts"] if row["identity_key"] == "ctaa")
        self.account_url = f"/accounts/{self.aarp}"
        self.assignment_action = f"/assignment/{self.aarp}"
        self.activity_action = f"{self.account_url}/activities"
        self.clock = datetime(2026, 11, 20, 20, 15, tzinfo=timezone.utc)
        self.app = create_app(self.database, demo=True)
        self.app.config.update(TESTING=True, CRM_CLOCK=lambda: self.clock)
        with closing(connect(self.database)) as connection, connection:
            connection.execute("""
                INSERT INTO people(account_id, name, email, origin)
                VALUES (?, 'Jamie Demo', 'jamie@example.test', 'demo')
            """, (self.aarp,))

    def remove_test_files(self):
        for path in self.test_dir.iterdir():
            path.unlink()
        self.test_dir.rmdir()

    def form_data(self, response, action):
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        form = RenderedForm(action)
        form.feed(response.get_data(as_text=True))
        self.assertTrue(form.found, f"Missing POST form for {action}")
        self.assertTrue(form.hidden.get("csrf_token"), f"Missing CSRF field for {action}")
        return form.hidden

    def login(self, username, landing):
        client = self.app.test_client()
        data = self.form_data(client.get("/login"), "/login")
        response = client.post("/login", data={
            **data, "email": f"{username}@example.test", "password": DEMO_PASSWORD,
        })
        self.assertEqual((response.status_code, response.location), (303, landing))
        return client

    def user_id(self, username):
        with closing(connect(self.database, read_only=True)) as connection:
            return connection.execute("SELECT id FROM users WHERE email = ?", (f"{username}@example.test",)).fetchone()[0]

    def assign(self, manager, username):
        form = self.form_data(manager.get("/assignment"), self.assignment_action)
        return manager.post(self.assignment_action, data={**form, "owner_id": self.user_id(username)})

    def test_manager_assigns_and_rep_logs_one_call_with_tomorrow_followup(self):
        manager = self.login("manager", "/assignment")
        rep = self.login("riya", "/today")
        other_rep = self.login("jordan", "/today")

        waiting = b"Ask your manager to assign your first account."
        self.assertIn(waiting, rep.get("/today").data)
        self.assertIn(waiting, rep.get("/my-accounts").data)
        self.assertNotIn(waiting, manager.get("/today").data)
        self.assertIn(b"No actions due tomorrow.", rep.get("/today?bucket=tomorrow").data)
        self.assertEqual(rep.get(self.account_url).status_code, 403)
        self.assertEqual(rep.get("/assignment").status_code, 403)
        self.assertEqual(self.app.test_client().get("/api/events").status_code, 401)
        pool = rep.get("/my-accounts?filter=pool&search=AARP")
        self.assertEqual(pool.status_code, 200)
        self.assertIn(b"AARP", pool.data)
        self.assertNotIn(b'aarp.org', pool.data)
        self.assertNotIn(f'href="{self.account_url}"'.encode(), pool.data)
        self.assertNotIn(b"Jamie Demo", pool.data)
        self.assertNotIn(b'aria-label="Account sources"', pool.data)
        self.assertEqual(self.assign(manager, "riya").status_code, 303)
        accounts = rep.get("/my-accounts")
        self.assertIn(b"AARP", accounts.data)
        today = rep.get("/today").data
        self.assertIn(b"Newly assigned", today)
        self.assertNotIn(waiting, today)
        self.assertNotIn(waiting, accounts.data)
        self.assertIn(b"No accounts match these filters.", rep.get("/my-accounts?search=does-not-match").data)

        account = rep.get(self.account_url)
        for page in (accounts, account):
            self.assertEqual(page.data.count(b'<li class="source-label">Cvent</li>'), 1)
            self.assertEqual(page.data.count(b'<li class="source-label">Directory of Associations</li>'), 1)
        self.assertIn(b"Why this account matters", account.data)
        self.assertIn(b"AARP WY 2026 Volunteer Summit", account.data)
        self.assertIn(b"32,000,000", account.data)
        self.assertIn(b'name="due_tomorrow"', account.data)
        self.assertEqual(other_rep.get(self.account_url).status_code, 403)
        self.assertEqual(other_rep.get(f"/api/events?account_id={self.aarp}").status_code, 403)
        self.assertEqual(other_rep.get("/api/events").get_json()["events"], [])
        self.assertEqual(rep.get(f"/accounts/{self.ctaa}").status_code, 403)

        data = self.form_data(account, self.activity_action)
        data.update(channel="call", outcome="no_answer", note="Left a message in the local demo.",
                    due_on="2026-11-22", next_step="Call again tomorrow")
        self.assertEqual(rep.post(self.activity_action, data=data).status_code, 303)
        self.assertEqual(rep.post(self.activity_action, data=data).status_code, 303)
        self.assertEqual(rep.post(self.activity_action, data={**data, "note": "A different save"}).status_code, 409)
        today = rep.get("/today").data
        self.assertNotIn(b"Call again tomorrow", today)
        self.assertNotIn(b"Newly assigned", today)
        tomorrow = rep.get("/today?bucket=tomorrow")
        self.assertIn(b"2026-11-22", tomorrow.data)
        self.assertEqual(tomorrow.data.count(b"Call again tomorrow"), 1)
        self.assertNotIn(b"Call again tomorrow", other_rep.get("/today?bucket=tomorrow").data)
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM activities").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

        self.clock = datetime(2026, 11, 21, 20, 15, tzinfo=timezone.utc)
        self.assertIn(b"Call again tomorrow", rep.get("/today").data)

    def test_today_clears_inbound_only_after_a_later_outbound_to_the_same_contact(self):
        manager = self.login("manager", "/assignment")
        rep = self.login("riya", "/today")
        self.assertEqual(self.assign(manager, "riya").status_code, 303)
        contact_action = f"{self.account_url}/contacts"
        contact = self.form_data(rep.get(self.account_url), contact_action)
        self.assertEqual(rep.post(contact_action, data={**contact, "name": "Sam Demo"}).status_code, 303)
        with closing(connect(self.database, read_only=True)) as connection:
            jamie, sam = (row[0] for row in connection.execute(
                "SELECT id FROM people WHERE account_id = ? ORDER BY name", (self.aarp,),
            ))

        def log_email(outcome, person_id=""):
            data = self.form_data(rep.get(self.account_url), self.activity_action)
            self.assertEqual(rep.post(self.activity_action, data={
                **data, "channel": "email", "outcome": outcome, "person_id": person_id,
            }).status_code, 303)

        log_email("sent", jamie)
        log_email("replied", jamie)
        log_email("sent", sam)
        log_email("replied", sam)
        log_email("replied")
        log_email("sent")
        self.clock -= timedelta(minutes=1)
        log_email("sent", jamie)
        self.clock += timedelta(minutes=1)
        today = rep.get("/today").data
        self.assertIn(b"Replied from Jamie Demo", today)
        self.assertEqual(today.count(b"Unanswered reply"), 3)

        log_email("sent", jamie)
        today = rep.get("/today")
        self.assertNotIn(b"Replied from Jamie Demo", today.data)
        self.assertIn(b"Replied from Sam Demo", today.data)
        self.assertEqual(today.data.count(b"Unanswered reply"), 2)
        with closing(connect(self.database, read_only=True)) as connection:
            unattributed_reply = connection.execute("""
                SELECT id FROM activities WHERE account_id = ? AND direction = 'inbound' AND person_id IS NULL
            """, (self.aarp,)).fetchone()[0]
        acknowledge = f"/replies/{unattributed_reply}/acknowledge"
        self.assertEqual(rep.post(acknowledge, data=self.form_data(today, acknowledge)).status_code, 303)
        self.assertEqual(rep.get("/today").data.count(b"Unanswered reply"), 1)

    def test_directory_follow_ups_from_account_and_contact_use_the_rep_queue(self):
        manager = self.login("manager", "/assignment")
        rep = self.login("riya", "/today")
        peer = self.login("jordan", "/today")
        account_url = f"/accounts/{self.ctaa}"
        assignment = f"/assignment/{self.ctaa}"
        assignment_form = self.form_data(manager.get("/assignment"), assignment)
        owner_id = self.user_id("riya")
        self.assertEqual(manager.post(assignment, data={**assignment_form, "owner_id": owner_id}).status_code, 303)

        endpoint = f"{account_url}/tasks"
        fields = self.form_data(rep.get(account_url), endpoint)
        action = {**fields, "title": "Research the convention", "due_on": "2026-11-21"}
        peer_form = self.form_data(peer.get("/today"), "/logout")
        self.assertEqual(peer.post(endpoint, data={**action, "csrf_token": peer_form["csrf_token"]}).status_code, 403)
        with closing(connect(self.database, read_only=True)) as connection:
            other_contact = connection.execute("SELECT id FROM people WHERE account_id = ?", (self.aarp,)).fetchone()[0]
        self.assertEqual(rep.post(endpoint, data={**action, "person_id": other_contact}).status_code, 400)
        self.assertEqual(rep.post(endpoint, data={**action, "due_on": "2026-11-20"}).status_code, 400)
        self.assertEqual(rep.post(endpoint, data=action).status_code, 303)
        self.assertEqual(rep.post(endpoint, data=action).status_code, 303)
        self.assertEqual(rep.post(endpoint, data={**action, "title": "A different task"}).status_code, 409)

        add_contact = f"{account_url}/contacts"
        contact_form = self.form_data(rep.get(account_url), add_contact)
        self.assertEqual(rep.post(add_contact, data={**contact_form, "name": "Convention coordinator"}).status_code, 303)
        with closing(connect(self.database, read_only=True)) as connection:
            person_id = connection.execute("SELECT id FROM people WHERE account_id = ?", (self.ctaa,)).fetchone()[0]
        contact_url = f"{account_url}/contacts/{person_id}"
        pinned_form = self.form_data(rep.get(contact_url), endpoint)
        self.assertEqual(pinned_form["person_id"], str(person_id))
        follow_up = {**pinned_form, "title": "Ask the coordinator", "due_on": "2026-11-22"}
        created = rep.post(endpoint, data=follow_up)
        self.assertEqual((created.status_code, created.location), (303, contact_url))
        self.assertEqual(rep.get("/today").data.count(b"Research the convention"), 1)
        self.assertNotIn(b"Ask the coordinator", rep.get("/today").data)
        self.assertEqual(rep.get("/today?bucket=tomorrow").data.count(b"Ask the coordinator"), 1)
        self.assertNotIn(b"Ask the coordinator", peer.get("/today?bucket=tomorrow").data)

        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE people SET do_not_contact = 1 WHERE id = ?", (person_id,))
        follow_up["action_key"] = uuid4().hex
        self.assertEqual(rep.post(endpoint, data=follow_up).status_code, 403)
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM activities").fetchone()[0], 0)
            tasks = connection.execute("""
                SELECT person_id, owner_id, created_by, activity_id FROM tasks ORDER BY id
            """).fetchall()
            self.assertEqual([tuple(row) for row in tasks], [
                (None, owner_id, owner_id, None), (person_id, owner_id, owner_id, None),
            ])
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM audit_events WHERE action = 'task_created' AND actor_id = ?", (owner_id,),
            ).fetchone()[0], 2)

    def test_coverage_follow_up_stays_with_the_permanent_owner_and_checks_revision(self):
        manager = self.login("manager", "/assignment")
        rep = self.login("riya", "/today")
        coverer = self.login("jordan", "/today")
        self.assertEqual(self.assign(manager, "riya").status_code, 303)
        linked_form = self.form_data(rep.get(self.account_url), self.activity_action)
        self.assertEqual(rep.post(self.activity_action, data={
            **linked_form, "channel": "call", "outcome": "no_answer",
            "next_step": "Call the organizer again", "due_on": "2026-11-22",
        }).status_code, 303)
        coverage = f"{self.account_url}/coverages"
        coverage_form = self.form_data(manager.get(self.account_url), coverage)
        coverer_id = self.user_id("jordan")
        self.assertEqual(manager.post(coverage, data={
            **coverage_form, "rep_id": coverer_id,
            "starts_at": "2026-11-21T01:00", "ends_at": "2026-11-21T02:00",
        }).status_code, 303)

        endpoint = f"{self.account_url}/tasks"
        fields = self.form_data(coverer.get(self.account_url), endpoint)
        action = {**fields, "title": "Confirm the planning agenda", "due_on": "2026-11-22"}
        self.assertEqual(coverer.post(endpoint, data={**action, "due_on": "2026-11-20"}).status_code, 400)
        self.assertEqual(coverer.post(endpoint, data=action).status_code, 303)
        self.assertIn(b"Confirm the planning agenda", rep.get("/today?bucket=tomorrow").data)
        self.assertNotIn(b"Confirm the planning agenda", coverer.get("/today?bucket=tomorrow").data)
        with closing(connect(self.database, read_only=True)) as connection:
            linked, standalone = connection.execute("""
                SELECT id, owner_id, created_by, activity_id FROM tasks ORDER BY id
            """).fetchall()
        owner_id = self.user_id("riya")
        self.assertEqual((standalone["owner_id"], standalone["created_by"], standalone["activity_id"]),
                         (owner_id, coverer_id, None))
        self.assertEqual(linked["owner_id"], owner_id)
        self.assertIsNotNone(linked["activity_id"])

        manager_fields = self.form_data(manager.get(self.account_url), endpoint)
        self.assertEqual(self.assign(manager, "jordan").status_code, 303)
        self.assertEqual(manager.post(endpoint, data={
            **manager_fields, "title": "A stale request", "due_on": "2026-11-22",
        }).status_code, 409)
        self.assertEqual(rep.get(self.account_url).status_code, 403)
        with closing(connect(self.database, read_only=True)) as connection:
            tasks = connection.execute("SELECT id, owner_id, created_by, activity_id FROM tasks ORDER BY id").fetchall()
        self.assertEqual([tuple(row) for row in tasks], [
            (linked["id"], coverer_id, linked["created_by"], linked["activity_id"]),
            (standalone["id"], coverer_id, coverer_id, None),
        ])

    def test_concurrent_assignment_rejects_stale_revision_and_transfer_keeps_task(self):
        manager = self.login("manager", "/assignment")
        administrator = self.login("admin", "/sources")
        data_manager = self.form_data(manager.get("/assignment"), self.assignment_action)
        data_admin = self.form_data(administrator.get("/assignment"), self.assignment_action)
        riya_id, jordan_id = self.user_id("riya"), self.user_id("jordan")

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(manager.post, self.assignment_action, data={**data_manager, "owner_id": riya_id})
            second = pool.submit(administrator.post, self.assignment_action, data={**data_admin, "owner_id": jordan_id})
            statuses = (first.result().status_code, second.result().status_code)
        self.assertCountEqual(statuses, [303, 409])
        winner, replacement = ("riya", "jordan") if statuses[0] == 303 else ("jordan", "riya")

        old_rep = self.login(winner, "/today")
        new_rep = self.login(replacement, "/today")
        early = self.form_data(old_rep.get(self.account_url), self.activity_action)
        self.assertEqual(old_rep.post(self.activity_action, data={
            **early, "channel": "call", "outcome": "no_answer", "due_on": "2026-11-22", "next_step": "Call the organizer",
        }).status_code, 303)
        stale = self.form_data(old_rep.get(self.account_url), self.activity_action)
        self.clock += timedelta(seconds=1)
        self.assertEqual(self.assign(manager, replacement).status_code, 303)
        self.assertEqual(old_rep.post(self.activity_action, data={
            **stale, "channel": "call", "outcome": "no_answer", "due_on": "2026-11-22",
        }).status_code, 403)
        self.assertIn(b"Call the organizer", new_rep.get(self.account_url).data)
        self.assertNotIn(b"Newly assigned", old_rep.get("/today").data)
        self.assertIn(b"Newly assigned", new_rep.get("/today").data)
        self.clock += timedelta(days=1)
        self.assertIn(b"Newly assigned", new_rep.get("/today").data)
        with closing(connect(self.database, read_only=True)) as connection:
            task_owner = connection.execute("SELECT owner_id FROM tasks").fetchone()[0]
            original_actor = connection.execute("SELECT actor_id FROM activities").fetchone()[0]
            changes = connection.execute("SELECT count(*) FROM audit_events WHERE action = 'assignment'").fetchone()[0]
        self.assertEqual(task_owner, self.user_id(replacement))
        self.assertEqual(original_actor, self.user_id(winner))
        self.assertEqual(changes, 2)


if __name__ == "__main__":
    unittest.main()
