"""R1-02.7 acceptance for the Admin merge screen."""

import re
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app import create_app
from crm_store import connect
from workflow_store import DEMO_PASSWORD
from workflow_web import staff_required


class AccountMergeUiTest(unittest.TestCase):
    clock = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)

    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1] / "instance" / f"r102-merge-ui-{uuid4().hex}"
        self.folder.mkdir(parents=True)
        self.addCleanup(self.remove_files)
        self.database = self.database_target()
        self.app = create_app(self.database, demo=True)
        self.app.config.update(TESTING=True, CRM_CLOCK=lambda: self.clock)
        with closing(connect(self.database)) as connection, connection:
            self.first = connection.execute("""
                INSERT INTO accounts(identity_key, name, domain, organization_type)
                VALUES ('earlier', 'Earlier Organization', 'primary.example.org', 'Association')
            """).lastrowid
            self.second = connection.execute("""
                INSERT INTO accounts(identity_key, name, domain, organization_type)
                VALUES ('later', 'Later Organization', 'secondary.example.net', 'Association')
            """).lastrowid
            self.contact = connection.execute("""
                INSERT INTO people(account_id, name, email, unlocked, email_verified)
                VALUES (?, 'Saved Contact', 'saved@example.net', 1, 1)
            """, (self.second,)).lastrowid
            connection.execute("""
                INSERT INTO source_records(source, source_id, source_url, source_file, account_id, details_json, raw_json)
                VALUES ('directory', 'association-1', 'https://directory.example.org/association-1', 'saved.json',
                        ?, '{"name":"Later Organization"}', '{"original":"saved"}')
            """, (self.second,))
            self.evidence = connection.execute("SELECT id FROM source_records WHERE source_id = 'association-1'").fetchone()[0]

    def database_target(self):
        return self.folder / "test.sqlite3"

    def remove_files(self):
        for path in self.folder.iterdir():
            path.unlink()
        self.folder.rmdir()

    def sign_in(self, role):
        client = self.app.test_client()
        self.assertEqual(client.get("/login").status_code, 200)
        self.assertEqual(self.post(client, "/login", email=f"{role}@example.test", password=DEMO_PASSWORD).status_code, 303)
        self.assertEqual(client.get("/", follow_redirects=True).status_code, 200)
        return client

    @staticmethod
    def post(client, route, **data):
        with client.session_transaction() as session:
            token = session["csrf"]
        return client.post(route, data={"csrf_token": token, **data})

    def preview(self, client):
        response = self.post(client, "/account-merges/preview", first_account_id=self.second,
                             second_account_id=self.first)
        self.assertEqual(response.status_code, 200)
        return response

    def preview_token(self, response):
        match = re.search(rb'name="merge_preview" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def test_admin_previews_then_confirms_once_and_old_id_keeps_survivor_permissions(self):
        admin = self.sign_in("admin")
        index = admin.get("/account-merges")
        self.assertEqual(index.status_code, 200)
        self.assertIn(b"Merge accounts", index.data)
        self.assertIn(b'href="/account-merges"', index.data)
        self.assertIn(b'Earlier Organization - primary.example.org', index.data)
        self.assertIn(b'Later Organization - secondary.example.net', index.data)
        preview = self.preview(admin)
        for text in (b"Account to keep", b"Account to merge", b"Contacts", b"Confirm merge"):
            self.assertIn(text, preview.data)
        token = self.preview_token(preview)
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM account_merges").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT account_id FROM people WHERE id = ?",
                                                (self.contact,)).fetchone()[0], self.second)
            self.assertEqual(connection.execute("SELECT account_id FROM source_records WHERE id = ?",
                                                (self.evidence,)).fetchone()[0], self.second)
        merged = self.post(admin, "/account-merges/confirm", merge_preview=token)
        self.assertEqual(merged.status_code, 303)
        self.assertEqual(merged.location, f"/accounts/{self.first}")
        self.assertIn(b"Saved Contact", admin.get(merged.location).data)
        self.assertNotIn(b'Later Organization - secondary.example.net', admin.get("/account-merges").data)
        replay = self.post(admin, "/account-merges/confirm", merge_preview=token)
        self.assertEqual(replay.status_code, 409)
        old_url = f"/accounts/{self.second}"
        redirected = admin.get(old_url)
        self.assertIn(redirected.status_code, (302, 303, 308))
        self.assertEqual(redirected.location, merged.location)
        old_contact = f"{old_url}/contacts/{self.contact}"
        contact_redirect = admin.get(old_contact)
        self.assertEqual(contact_redirect.status_code, 302)
        self.assertEqual(contact_redirect.location, f"/accounts/{self.first}/contacts/{self.contact}")
        rep = self.sign_in("riya")
        self.assertEqual(rep.get(old_url).status_code, 403)
        self.assertEqual(rep.get(old_contact).status_code, 403)
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual([tuple(row) for row in connection.execute("SELECT from_id, to_id FROM account_merges")],
                             [(self.second, self.first)])
            self.assertEqual(tuple(connection.execute("SELECT id, account_id, unlocked, email_verified FROM people").fetchone()),
                             (self.contact, self.first, 1, 1))
            self.assertEqual(tuple(connection.execute("SELECT account_id, raw_json FROM source_records").fetchone()),
                             (self.first, '{"original":"saved"}'))
            audits = connection.execute("""
                SELECT e.account_id, u.email FROM audit_events e JOIN users u ON u.id = e.actor_id
                WHERE action = 'account_merge'
            """).fetchall()
            self.assertEqual([tuple(row) for row in audits], [(self.first, "admin@example.test")])

    def test_manager_previews_and_confirms_with_a_preview_bound_to_their_sign_in(self):
        manager = self.sign_in("manager")
        self.assertIn(b'href="/account-merges"', manager.get("/", follow_redirects=True).data)
        self.assertEqual(manager.get("/account-merges").status_code, 200)
        token = self.preview_token(self.preview(manager))
        admin = self.sign_in("admin")
        self.assertEqual(self.post(admin, "/account-merges/confirm", merge_preview=token).status_code, 403)
        confirmed = self.post(manager, "/account-merges/confirm", merge_preview=token)
        self.assertEqual(confirmed.status_code, 303)
        self.assertEqual(confirmed.location, f"/accounts/{self.first}")
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual([tuple(row) for row in connection.execute("SELECT from_id, to_id FROM account_merges")],
                             [(self.second, self.first)])
            actors = connection.execute("""
                SELECT u.email FROM audit_events e JOIN users u ON u.id = e.actor_id WHERE e.action = 'account_merge'
            """).fetchall()
            self.assertEqual([row["email"] for row in actors], ["manager@example.test"])

    def test_confirmation_rechecks_operator_activation_after_request_authorization(self):
        manager = self.sign_in("manager")
        token = self.preview_token(self.preview(manager))

        def deactivate_after_authorization():
            operator = staff_required()
            with closing(connect(self.database)) as connection, connection:
                connection.execute("UPDATE users SET active = 0 WHERE id = ?", (operator["id"],))
            return operator

        with patch("account_merges_web.staff_required", side_effect=deactivate_after_authorization):
            denied = self.post(manager, "/account-merges/confirm", merge_preview=token)
        self.assertEqual(denied.status_code, 403)
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM account_merges").fetchone()[0], 0)

    def test_sdr_ae_and_missing_csrf_cannot_use_merge_forms(self):
        for role in ("riya", "ae"):
            with self.subTest(role=role):
                client = self.sign_in(role)
                self.assertEqual(client.get("/account-merges").status_code, 403)
                self.assertNotIn(b'href="/account-merges"', client.get("/", follow_redirects=True).data)
                self.assertEqual(self.post(client, "/account-merges/preview", first_account_id=self.first,
                                           second_account_id=self.second).status_code, 403)
                self.assertEqual(self.post(client, "/account-merges/confirm", merge_preview="untrusted").status_code, 403)
        admin = self.sign_in("admin")
        self.assertEqual(admin.post("/account-merges/confirm", data={"merge_preview": "untrusted"}).status_code, 400)
        same_account = self.post(admin, "/account-merges/preview", first_account_id=self.first,
                                 second_account_id=self.first)
        self.assertEqual(same_account.status_code, 400)

    def test_tampered_or_stale_preview_cannot_repoint_history(self):
        admin = self.sign_in("admin")
        token = self.preview_token(self.preview(admin))
        tampered = self.post(admin, "/account-merges/confirm", merge_preview=f"{token}invalid")
        self.assertEqual(tampered.status_code, 400)
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE accounts SET name = 'Later Revised Organization' WHERE id = ?", (self.second,))
        stale = self.post(admin, "/account-merges/confirm", merge_preview=token)
        self.assertEqual(stale.status_code, 409)
        self.assertIn(b"preview", stale.data.lower())
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM account_merges").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT account_id FROM people WHERE id = ?",
                                                (self.contact,)).fetchone()[0], self.second)

    def test_conflicting_active_owners_are_shown_without_a_confirm_control(self):
        with closing(connect(self.database)) as connection, connection:
            rep_ids = [user[0] for user in connection.execute("SELECT id FROM users WHERE role = 'sdr' ORDER BY id LIMIT 2")]
            connection.executemany("""
                INSERT INTO account_work(account_id, owner_id, stage) VALUES (?, ?, 'assigned')
            """, zip((self.first, self.second), rep_ids))
        preview = self.preview(self.sign_in("admin"))
        self.assertIn(b"Resolve before merging", preview.data)
        self.assertIn(b"owner", preview.data.lower())
        self.assertNotIn(b"Confirm merge", preview.data)
        self.assertNotIn(b'name="merge_preview"', preview.data)
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM account_merges").fetchone()[0], 0)
