"""Manager follow-up discovery through the saved task and Team queue forms."""

import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from app import create_app
from crm_store import connect
from test_postgres_pilot import PostgresDatabase, TEST_URL
from test_workflow import RenderedForm
from workflow_store import DEMO_PASSWORD, save_suppression_rule, transaction


OBSERVED = datetime(2026, 11, 27, 6, tzinfo=timezone.utc)
ALL_OPEN = "/today?bucket=all_open"
TOMORROW_TASK = "Call the organizer tomorrow"
LATER_TASK = "Send the 30 November agenda"
PEER_TASK = "Peer draft follow-up"


class TeamFollowUpQueueTest(unittest.TestCase):
    def setUp(self):
        self.database = self.database_target()
        self.app = create_app(self.database, demo=True)
        self.app.config.update(TESTING=True, CRM_CLOCK=lambda: OBSERVED)
        with closing(connect(self.database)) as db, transaction(db):
            self.users = {row["email"].split("@")[0]: dict(row) for row in db.execute(
                "SELECT id, name, email FROM users",
            )}
            self.company = self.add_account(db, "meeting-circle", "Meeting Circle")
            self.peer_company = self.add_account(db, "planner-group", "Planner Group")

    def database_target(self):
        folder = Path(__file__).resolve().parents[1] / "instance" / f"team-follow-ups-{uuid4().hex}"
        folder.mkdir(parents=True)
        self.addCleanup(folder.rmdir)
        path = folder / "queue.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            self.addCleanup(path.with_name(path.name + suffix).unlink, missing_ok=True)
        return path

    def add_account(self, db, key, name):
        domain = f"{key}.example"
        return db.execute("""
            INSERT INTO accounts(identity_key, name, domain, root_domain)
            VALUES (?, ?, ?, ?)
        """, (key, name, domain, domain)).lastrowid

    def form_data(self, response, action):
        self.assertEqual(response.status_code, 200)
        form = RenderedForm(action)
        form.feed(response.get_data(as_text=True))
        self.assertTrue(form.found, f"Missing POST form for {action}")
        self.assertTrue(form.hidden.get("csrf_token"), f"Missing CSRF for {action}")
        return form.hidden

    def login(self, username, landing):
        client = self.app.test_client()
        response = client.post("/login", data={
            **self.form_data(client.get("/login"), "/login"),
            "email": self.users[username]["email"], "password": DEMO_PASSWORD,
        })
        self.assertEqual((response.status_code, response.location), (303, landing))
        return client

    def assign(self, manager, account_id, username):
        action = f"/assignment/{account_id}"
        response = manager.post(action, data={
            **self.form_data(manager.get("/assignment"), action), "owner_id": self.users[username]["id"],
        })
        self.assertEqual(response.status_code, 303)

    def create_follow_up(self, rep, account_id, title, due):
        page = f"/accounts/{account_id}"
        action = f"{page}/tasks"
        response = rep.post(action, data={
            **self.form_data(rep.get(page), action), "title": title, "due_on": due,
        })
        self.assertEqual(response.status_code, 303)

    def selected_queue(self, username):
        return f"{ALL_OPEN}&sdr={self.users[username]['id']}"

    def task_row(self, response, title):
        before, heading, after = response.data.partition(f"<h3>{title}</h3>".encode())
        self.assertTrue(heading, f"Missing follow-up for {title}")
        return before.rsplit(b"<li", 1)[-1] + after.split(b"</li>", 1)[0]

    def insert_task(self, db, account_id, owner, title, due_on, *, person_id=None, status="open"):
        db.execute("""
            INSERT INTO tasks(account_id, owner_id, person_id, title, due_on, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (account_id, self.users[owner]["id"], person_id, title, due_on, status))

    def test_mq01_manager_finds_both_saved_dates_for_selected_sdr(self):
        manager = self.login("manager", "/assignment")
        riya = self.login("riya", "/today")
        jordan = self.login("jordan", "/today")
        self.assign(manager, self.company, "riya")
        self.assign(manager, self.peer_company, "jordan")
        self.create_follow_up(riya, self.company, TOMORROW_TASK, "2026-11-28")
        self.create_follow_up(riya, self.company, LATER_TASK, "2026-11-30")
        self.create_follow_up(jordan, self.peer_company, PEER_TASK, "2026-11-28")

        page = manager.get(self.selected_queue("riya"))
        self.assertEqual(page.status_code, 200)
        self.assertIn(LATER_TASK.encode(), page.data)
        self.assertIn(TOMORROW_TASK.encode(), page.data)
        self.assertNotIn(PEER_TASK.encode(), page.data)
        self.assertIn(b'name="sdr"', page.data)
        for title, due in ((TOMORROW_TASK, "2026-11-28"), (LATER_TASK, "2026-11-30")):
            row = self.task_row(page, title)
            self.assertIn(self.users["riya"]["name"].encode(), row)
            self.assertIn(f'<time datetime="{due}">'.encode(), row)
            self.assertIn(f'href="/accounts/{self.company}"'.encode(), row)
        self.assertIn(b'href="/today?bucket=all_open"', manager.get("/assignment").data)
        self.assertIn(PEER_TASK.encode(), manager.get(f"{ALL_OPEN}&sdr=").data)

        daily = manager.get("/today?bucket=tomorrow")
        self.assertIn(TOMORROW_TASK.encode(), daily.data)
        self.assertNotIn(LATER_TASK.encode(), daily.data)

    def test_mq02_filters_by_saved_task_owner_and_hides_done_and_suppressed(self):
        manager = self.login("manager", "/assignment")
        self.assign(manager, self.company, "riya")
        with closing(connect(self.database)) as db, transaction(db):
            blocked = self.add_account(db, "blocked-company", "Blocked Company")
            db.execute("INSERT INTO account_work(account_id, owner_id) VALUES (?, ?)",
                       (blocked, self.users["riya"]["id"]))
            person_id = db.execute("""
                INSERT INTO people(account_id, name, email) VALUES (?, 'Opted-out Contact', 'contact@meeting-circle.example')
            """, (self.company,)).lastrowid
            # Legacy tasks can have a different responsible person than the account.
            self.insert_task(db, self.company, "jordan", PEER_TASK, "2026-11-30")
            self.insert_task(db, self.company, "riya", "Overdue action", "2026-11-20")
            self.insert_task(db, self.company, "riya", "Rescheduled action", "2026-12-15", status="snoozed")
            self.insert_task(db, self.company, "riya", "Completed outreach", "2026-11-27", status="done")
            self.insert_task(db, blocked, "riya", "Suppressed account task", "2026-11-28")
            self.insert_task(db, self.company, "riya", "Suppressed contact task", "2026-11-28", person_id=person_id)
            admin_id = self.users["admin"]["id"]
            save_suppression_rule(db, "account", str(blocked), "competitor", admin_id, OBSERVED)
            save_suppression_rule(db, "email", "contact@meeting-circle.example", "opt_out", admin_id, OBSERVED)

        page = manager.get(self.selected_queue("riya"))
        self.assertIn(b"Overdue", self.task_row(page, "Overdue action"))
        self.assertRegex(self.task_row(page, "Rescheduled action"), rb"(?i)(snoozed|rescheduled)")
        for title in (PEER_TASK, "Completed outreach", "Suppressed account task", "Suppressed contact task"):
            self.assertNotIn(title.encode(), page.data)
        peer_row = self.task_row(manager.get(self.selected_queue("jordan")), PEER_TASK)
        self.assertIn(self.users["jordan"]["name"].encode(), peer_row)

    def test_mq02_preserves_the_sdr_filter_when_searching_and_paging(self):
        manager = self.login("manager", "/assignment")
        self.assign(manager, self.company, "riya")
        self.assign(manager, self.peer_company, "jordan")
        with closing(connect(self.database)) as db, transaction(db):
            for index in range(26):
                due = (date(2026, 12, 1) + timedelta(days=index)).isoformat()
                self.insert_task(db, self.company, "riya", f"Planning task {index:02d}", due)
            self.insert_task(db, self.peer_company, "jordan", "Planning task 26", "2026-12-27")

        page = manager.get(f"{self.selected_queue('riya')}&search=Planning&sort=due")
        self.assertIn(b"Planning task 24", page.data)
        self.assertNotIn(b"Planning task 25", page.data)
        self.assertIn(b"Page 1 of 2", page.data)
        second = manager.get(f"{self.selected_queue('riya')}&search=Planning&sort=due&page=2")
        self.assertIn(b"Planning task 25", second.data)
        self.assertNotIn(b"Planning task 26", second.data)
        next_link = f"bucket=all_open&amp;sdr={self.users['riya']['id']}&amp;search=Planning&amp;sort=due&amp;page=2"
        self.assertIn(next_link.encode(), page.data)

    def test_mq03_staff_view_denies_sdr_and_ae(self):
        admin = self.login("admin", "/sources")
        riya = self.login("riya", "/today")
        ae = self.login("ae", "/handoffs")
        self.assertEqual(admin.get(self.selected_queue("riya")).status_code, 200)
        self.assertEqual(riya.get(self.selected_queue("riya")).status_code, 403)
        self.assertEqual(ae.get(ALL_OPEN).status_code, 403)
        self.assertNotIn(b'name="sdr"', riya.get("/today").data)

    def test_mq03_task_actions_keep_the_filter_and_transfer_follows_owner(self):
        manager = self.login("manager", "/assignment")
        riya = self.login("riya", "/today")
        self.assign(manager, self.company, "riya")
        self.create_follow_up(riya, self.company, TOMORROW_TASK, "2026-11-28")
        self.create_follow_up(riya, self.company, LATER_TASK, "2026-11-30")
        with closing(connect(self.database, read_only=True)) as db:
            task_id = db.execute("SELECT id FROM tasks WHERE title = ?", (LATER_TASK,)).fetchone()[0]

        selected = self.selected_queue("riya")
        action = f"/tasks/{task_id}"
        expected_filter = {"bucket": ["all_open"], "sdr": [str(self.users["riya"]["id"])]}
        rescheduled = manager.post(action, data={
            **self.form_data(manager.get(selected), action), "action": "snooze", "due_on": "2026-12-18",
        })
        self.assertEqual(rescheduled.status_code, 303)
        self.assertEqual(parse_qs(urlsplit(rescheduled.location).query), expected_filter)
        row = self.task_row(manager.get(rescheduled.location), LATER_TASK)
        self.assertIn(b'<time datetime="2026-12-18">', row)
        self.assertRegex(row, rb"(?i)(snoozed|rescheduled)")

        completed = manager.post(action, data={
            **self.form_data(manager.get(selected), action), "action": "complete",
        })
        self.assertEqual(completed.status_code, 303)
        self.assertEqual(parse_qs(urlsplit(completed.location).query), expected_filter)
        self.assertNotIn(LATER_TASK.encode(), manager.get(completed.location).data)

        self.assign(manager, self.company, "jordan")
        self.assertNotIn(TOMORROW_TASK.encode(), manager.get(selected).data)
        self.assertIn(TOMORROW_TASK.encode(), manager.get(self.selected_queue("jordan")).data)


@unittest.skipUnless(TEST_URL, "Set CRM_TEST_POSTGRES_URL for disposable PostgreSQL.")
class PostgresTeamFollowUpQueueTest(PostgresDatabase, TeamFollowUpQueueTest):
    pass
