import os
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_urlsafe
from unittest.mock import patch
from uuid import uuid4

from werkzeug.security import generate_password_hash

from app import create_app
from crm_store import connect
from pilot_web import configure_pilot_users
from test_workflow import RenderedForm


class PilotUsersTest(unittest.TestCase):
    USERS = (
        ("admin", "admin@pilot.example", "admin@nunify.com", "/sources"),
        ("manager", "manager@pilot.example", "manager@nunify.com", "/assignment"),
        ("sdr", "sdr@pilot.example", "riya@nunify.com", "/today"),
    )

    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / "instance" / f"pilot-users-test-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(self.remove_test_files)
        self.database = self.directory / "test.sqlite3"
        self.app = create_app(self.database)
        self.app.config.update(TESTING=True, CRM_CLOCK=lambda: datetime(2026, 11, 24, tzinfo=timezone.utc))
        self.passwords = {f"CRM_{role.upper()}_PASSWORD": token_urlsafe(20) for role, *_ in self.USERS}

    def remove_test_files(self):
        for file in self.directory.iterdir():
            file.unlink()
        self.directory.rmdir()

    def configure(self, passwords):
        with patch.dict(os.environ, passwords, clear=True), closing(connect(self.database)) as connection:
            configure_pilot_users(connection)

    def insert_user(self, connection, email, role, password):
        return connection.execute("""
            INSERT INTO users(name, email, role, timezone, password_hash)
            VALUES (?, ?, ?, 'Asia/Kolkata', ?)
        """, (f"Original {role}", email, role, generate_password_hash(password))).lastrowid

    def users(self):
        with closing(connect(self.database, read_only=True)) as connection:
            return {row["email"]: dict(row) for row in connection.execute("SELECT * FROM users")}

    def login(self, email, password):
        client = self.app.test_client()
        form = RenderedForm("/login")
        form.feed(client.get("/login", base_url="https://test.example").get_data(as_text=True))
        self.assertTrue(form.hidden.get("csrf_token"))
        response = client.post("/login", base_url="https://test.example", data={
            **form.hidden, "email": email, "password": password,
        })
        return client, response

    def test_fresh_pilot_provisions_named_role_logins(self):
        self.configure(self.passwords)
        users = self.users()
        self.assertEqual(set(users), {email for _, _, email, _ in self.USERS})
        for role, _, email, landing in self.USERS:
            with self.subTest(role=role):
                self.assertEqual(users[email]["role"], role)
                self.assertEqual(users[email]["daily_credit_quota"], 0)
                _, response = self.login(email, self.passwords[f"CRM_{role.upper()}_PASSWORD"])
                self.assertEqual((response.status_code, response.location), (303, landing))
                _, response = self.login(email, token_urlsafe(20))
                self.assertEqual(response.status_code, 401)

    def test_manager_can_assign_to_each_configured_sdr(self):
        reps = (
            ("riya", "CRM_SDR_PASSWORD"),
            ("arjun", "CRM_ARJUN_PASSWORD"),
            ("neha", "CRM_NEHA_PASSWORD"),
            ("kavya", "CRM_KAVYA_PASSWORD"),
        )
        self.configure(self.passwords)
        riya_id = self.users()["riya@nunify.com"]["id"]
        passwords = {**self.passwords, **{key: token_urlsafe(20) for _, key in reps[1:]}}
        self.configure(passwords)
        users = self.users()
        self.assertEqual(users["riya@nunify.com"]["id"], riya_id)
        accounts = {}
        with closing(connect(self.database)) as connection, connection:
            for name, _ in reps:
                accounts[name] = connection.execute("""
                    INSERT INTO accounts(identity_key, name, domain, organization_type)
                    VALUES (?, ?, ?, 'Association')
                """, (name, f"Sample account for {name}", f"{name}.example")).lastrowid

        manager, response = self.login("manager@nunify.com", self.passwords["CRM_MANAGER_PASSWORD"])
        self.assertEqual((response.status_code, response.location), (303, "/assignment"))
        assignment_page = manager.get("/assignment", base_url="https://test.example")
        self.assertEqual(assignment_page.status_code, 200)
        assignment_html = assignment_page.get_data(as_text=True)
        for name, _ in reps:
            with self.subTest(sdr=name):
                email = f"{name}@nunify.com"
                rep = users[email]
                self.assertEqual(rep["role"], "sdr")
                action = f"/assignment/{accounts[name]}"
                form = RenderedForm(action)
                form.feed(assignment_html)
                self.assertTrue(form.found, action)
                owner_form = assignment_html.partition(f'action="{action}"')[2].partition("</form>")[0]
                self.assertIn(f'<option value="{rep["id"]}">{rep["name"]}</option>', owner_form)
                assigned = manager.post(action, data={**form.hidden, "owner_id": rep["id"]},
                                        base_url="https://test.example")
                self.assertEqual(assigned.status_code, 303)

        self.configure(self.passwords)
        for name, key in reps:
            with self.subTest(sdr=name):
                client, response = self.login(f"{name}@nunify.com", passwords[key])
                self.assertEqual((response.status_code, response.location), (303, "/today"))
                self.assertEqual(client.get(f"/accounts/{accounts[name]}", base_url="https://test.example").status_code, 200)
                peer = "arjun" if name == "riya" else "riya"
                self.assertEqual(client.get(f"/accounts/{accounts[peer]}", base_url="https://test.example").status_code, 403)

        with closing(connect(self.database, read_only=True)) as connection:
            owners = dict(connection.execute("SELECT account_id, owner_id FROM account_work"))
        self.assertEqual(owners, {accounts[name]: users[f"{name}@nunify.com"]["id"] for name, _ in reps})

    def test_legacy_user_rename_preserves_work_and_restart_is_idempotent(self):
        previous_password = token_urlsafe(20)
        with closing(connect(self.database)) as connection, connection:
            user_ids = {
                role: self.insert_user(connection, previous_email, role, previous_password)
                for role, previous_email, _, _ in self.USERS
            }
            owner_id = user_ids["sdr"]
            account_id = connection.execute("""
                INSERT INTO accounts(identity_key, name, domain, organization_type)
                VALUES ('sample', 'Sample Organization', 'sample.example', 'Association')
            """).lastrowid
            connection.execute("""
                INSERT INTO account_work(account_id, owner_id, stage) VALUES (?, ?, 'assigned')
            """, (account_id, owner_id))
            activity_id = connection.execute("""
                INSERT INTO activities(account_id, actor_id, channel, outcome, direction, occurred_at)
                VALUES (?, ?, 'call', 'no_answer', 'outbound', '2026-11-23T10:00:00+00:00')
            """, (account_id, owner_id)).lastrowid
            connection.execute("""
                INSERT INTO tasks(account_id, owner_id, activity_id, title, due_on)
                VALUES (?, ?, ?, 'Call again', '2026-11-23')
            """, (account_id, owner_id, activity_id))

        self.configure(self.passwords)
        users = self.users()
        self.assertEqual({row["role"]: row["id"] for row in users.values()}, user_ids)
        self.assertEqual(set(users), {email for _, _, email, _ in self.USERS})
        for role, previous_email, email, landing in self.USERS:
            with self.subTest(role=role):
                password = self.passwords[f"CRM_{role.upper()}_PASSWORD"]
                _, response = self.login(previous_email, previous_password)
                self.assertEqual(response.status_code, 401)
                _, response = self.login(email, previous_password)
                self.assertEqual(response.status_code, 401)
                client, response = self.login(email, password)
                self.assertEqual((response.status_code, response.location), (303, landing))
                if role == "sdr":
                    accounts = client.get("/my-accounts", base_url="https://test.example")
                    self.assertIn(b"Sample Organization", accounts.data)
                    today = client.get("/today", base_url="https://test.example")
                    self.assertIn(b"Call again", today.data)

        self.configure(self.passwords)
        self.configure({})
        self.assertEqual(self.users(), users)
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual(connection.execute("SELECT owner_id FROM account_work").fetchone()[0], owner_id)
            self.assertEqual(connection.execute("SELECT actor_id FROM activities").fetchone()[0], owner_id)
            task = connection.execute("SELECT owner_id, activity_id FROM tasks").fetchone()
            self.assertEqual(tuple(task), (owner_id, activity_id))
            self.assertFalse(connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_missing_password_rolls_back_fresh_pilot_provisioning(self):
        passwords = {key: value for key, value in self.passwords.items() if key != "CRM_SDR_PASSWORD"}
        with self.assertRaisesRegex(ValueError, "CRM_SDR_PASSWORD"):
            self.configure(passwords)
        self.assertEqual(self.users(), {})

    def test_email_collision_preserves_existing_users(self):
        with closing(connect(self.database)) as connection, connection:
            for email in ("manager@pilot.example", "manager@nunify.com"):
                self.insert_user(connection, email, "manager", token_urlsafe(20))
        users = self.users()
        with self.assertRaises(ValueError):
            self.configure(self.passwords)
        self.assertEqual(self.users(), users)

    def test_wrong_role_preserves_existing_user(self):
        with closing(connect(self.database)) as connection, connection:
            self.insert_user(connection, "manager@nunify.com", "sdr", token_urlsafe(20))
        users = self.users()
        with self.assertRaises(ValueError):
            self.configure(self.passwords)
        self.assertEqual(self.users(), users)
