import json
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import account_names
from app import create_app
from crm_store import connect, import_snapshot
from workflow_store import DEMO_PASSWORD, timestamp


ACCOUNT = {
    "lake": {"name": "Lakeshore", "domain": "lakeshore.example", "organization_type": "Association"},
    "unlinked": {"name": "Unverified Organization", "domain": "unverified.example", "organization_type": "Company"},
}


class AccountNamesTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1] / "instance" / f"account-names-{uuid4().hex}"
        self.folder.mkdir(parents=True)
        self.addCleanup(self._remove_files)
        self.database = self.folder / "names.sqlite3"
        self.cvent = self._event("annual", "Lakeshore Engineering Council", "Annual Conference")
        self.noisy = self._event("registration", "Annual General Meeting", "Annual General Meeting")
        self.slug = self._event("slug", "lakeshoreeventregistration", "Spring Event")
        self.directory = {
            "source": "directory", "source_id": "directory-organization", "source_file": "directory.json",
            "source_url": "https://directoryofassociations.com/profile", "identity_key": "lake", "reason": None,
            "details": {"name": "Lakeshore Engineering Association", "host": "lakeshore.example"},
            "raw": {"Name": "Lakeshore Engineering Association"},
        }
        import_snapshot(self.database, ACCOUNT, [self.cvent, self.noisy, self.slug, self.directory])
        self.app = create_app(self.database, demo=True)
        self.app.testing = True
        with closing(connect(self.database)) as connection, connection:
            self.account_id = connection.execute("SELECT id FROM accounts WHERE identity_key = 'lake'").fetchone()[0]
            self.rep_id = connection.execute("SELECT id FROM users WHERE role = 'sdr' ORDER BY id LIMIT 1").fetchone()[0]
            connection.execute("""
                INSERT INTO account_work(account_id, owner_id, stage, revision) VALUES (?, ?, 'working', 7)
            """, (self.account_id, self.rep_id))
            connection.execute("""
                INSERT INTO people(account_id, name, email, unlocked) VALUES (?, 'Test Person', 'person@example.test', 1)
            """, (self.account_id,))
            connection.executemany("""
                INSERT INTO imported_source_evidence
                    (source_slug, external_id, account_id, source_url, observed_at, payload_json)
                VALUES ('upload', ?, ?, ?, ?, ?)
            """, [
                ("site-title", self.account_id, "https://lakeshore.example/about", timestamp(),
                 json.dumps({"og:site_name": "Lakeshore Council for Engineering"})),
                ("registration-title", self.account_id, "https://cvent.com/register", timestamp(),
                 json.dumps({"website_title": "Registration Page Title"})),
            ])

    def _remove_files(self):
        for path in self.folder.iterdir():
            path.unlink()
        self.folder.rmdir()

    @staticmethod
    def _event(record_id, organizer, event_name):
        return {
            "source": "cvent", "source_id": record_id, "source_file": "events.json",
            "source_url": f"https://web.cvent.com/event/{record_id}/summary", "identity_key": "lake", "reason": None,
            "details": {"organizer_company": organizer, "event_name": event_name, "evidence_keys": "[]"},
            "raw": {"organizer": organizer, "title": event_name},
        }

    def _login(self, actor):
        client = self.app.test_client()
        client.get("/login")
        with client.session_transaction() as session:
            login_token = session["csrf"]
        response = client.post("/login", data={
            "email": f"{actor}@example.test", "password": DEMO_PASSWORD, "csrf_token": login_token,
        })
        self.assertEqual(response.status_code, 303)
        return client

    def _context(self):
        with closing(connect(self.database, read_only=True)) as connection:
            return account_names.account_context(connection, self.account_id, {"role": "admin"})

    def _save(self, client, info, **changes):
        client.get("/", follow_redirects=True)
        with client.session_transaction() as session:
            csrf = session["csrf"]
        return client.post(f"/accounts/{self.account_id}/name", data={
            "csrf_token": csrf, "version": info["version"], **changes,
        })

    def test_local_proposals_are_scoped_and_reject_event_and_registration_titles(self):
        manager, rep = self._login("manager"), self._login("riya")
        response = manager.get("/account-names?state=different&search=lakeshore.example")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Lakeshore Engineering Council", response.data)
        self.assertNotIn(b"Unverified Organization", response.data)
        self.assertEqual(rep.get("/account-names").status_code, 403)
        info = self._context()
        self.assertEqual(info["source"], "unreviewed")
        self.assertEqual([candidate["name"] for candidate in info["candidates"]], [
            "Lakeshore Engineering Council", "Lakeshore Engineering Association", "Lakeshore Council for Engineering",
        ])
        self.assertEqual(self._save(manager, info, decision="approve", candidate=info["preferred"]["key"]).status_code, 403)
        self.assertEqual(self._save(rep, info, decision="approve", candidate=info["preferred"]["key"]).status_code, 403)

    def test_admin_approval_and_manual_edit_keep_audit_evidence_and_survive_refresh(self):
        admin = self._login("admin")
        before = self._context()
        page = admin.get(f"/accounts/{self.account_id}")
        self.assertIn(b"Use this name", page.data)
        self.assertIn(b"Edit account name", page.data)
        self.assertEqual(admin.post(f"/accounts/{self.account_id}/name", data={
            "version": before["version"], "decision": "approve", "candidate": before["preferred"]["key"],
        }).status_code, 400)
        self.assertEqual(self._save(admin, before, decision="approve", candidate=before["preferred"]["key"]).status_code, 303)
        approved = self._context()
        self.assertEqual((approved["name"], approved["source"], approved["reference"]),
                         ("Lakeshore Engineering Council", "organizer", "cvent:annual"))
        self.assertEqual(self._save(admin, before, decision="manual", name="Outdated Name",
                                    reason="Old form").status_code, 409)
        self.assertEqual(self._save(admin, approved, decision="manual", name="Lakeshore Engineering Council Ltd",
                                    reason="Verified registered organization name").status_code, 303)
        refresh = self._event("annual", "Lakeshore Engineering Group", "Annual Conference")
        import_snapshot(self.database, ACCOUNT, [refresh])
        manual = self._context()
        self.assertEqual((manual["name"], manual["source"]), ("Lakeshore Engineering Council Ltd", "manual"))
        self.assertEqual(self._save(admin, manual, decision="approve", candidate=manual["preferred"]["key"]).status_code, 409)
        with closing(connect(self.database, read_only=True)) as connection:
            stage = connection.execute("SELECT owner_id, stage, revision FROM account_work WHERE account_id = ?",
                                       (self.account_id,)).fetchone()
            self.assertEqual(tuple(stage), (self.rep_id, "working", 7))
            self.assertEqual(connection.execute("SELECT unlocked FROM people WHERE account_id = ?",
                                                (self.account_id,)).fetchone()[0], 1)
            records = connection.execute("""
                SELECT account_id, raw_json FROM source_records WHERE source_id = 'annual'
            """).fetchone()
            self.assertEqual(records["account_id"], self.account_id)
            self.assertEqual(json.loads(records["raw_json"]), refresh["raw"])
            audit_rows = connection.execute("""
                SELECT details FROM audit_events WHERE account_id = ? AND action = 'account_name_changed' ORDER BY id
            """, (self.account_id,)).fetchall()
            evidence, correction = [json.loads(row["details"]) for row in audit_rows]
            self.assertEqual(evidence["before"]["name"], "Lakeshore")
            self.assertEqual(evidence["source_url"], self.cvent["source_url"])
            self.assertEqual(correction["reason"], "Verified registered organization name")

    def test_conflicting_linked_organizer_names_require_an_explicit_choice(self):
        other = self._event("disagreement", "Lakeshore National Society", "National Conference")
        import_snapshot(self.database, ACCOUNT, [other])
        info = self._context()
        self.assertEqual(info["state"], "conflicting")
        self.assertIsNone(info["preferred"])
        admin = self._login("admin")
        response = admin.get(f"/accounts/{self.account_id}")
        self.assertIn(b"The linked records give different organization names", response.data)
        self.assertIn(b"Lakeshore National Society", response.data)

    def test_exact_provenance_backfill_keeps_display_names_and_replays_without_duplicates(self):
        records = [
            dict(self._event("punctuation", "Unverified Organization Inc", "Workshop"),
                 identity_key="unlinked"),
            dict(self._event("protected", "Protected Organization", "Annual Meeting"), identity_key="protected"),
        ]
        import_snapshot(self.database, {
            **ACCOUNT,
            "protected": {"name": "Protected Organization", "domain": "protected.example",
                          "organization_type": "Association"},
        }, records)
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE accounts SET name = 'Lakeshore Engineering Council' WHERE identity_key = 'lake'")
            connection.execute("""
                UPDATE accounts SET name = 'Unverified Organization, Inc.' WHERE identity_key = 'unlinked'
            """)
            connection.execute("UPDATE accounts SET name_source = 'manual' WHERE identity_key = 'protected'")
            before = [tuple(row) for row in connection.execute("SELECT id, name FROM accounts ORDER BY id")]
        admin = self._login("admin")
        page = admin.get("/account-names")
        self.assertIn(b"1 existing name matches linked source evidence exactly", page.data)
        with admin.session_transaction() as session:
            csrf = session["csrf"]
        post = {"csrf_token": csrf}
        for _ in range(2):
            self.assertEqual(admin.post("/account-names/record-provenance", data=post).status_code, 303)
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual([tuple(row) for row in connection.execute("SELECT id, name FROM accounts ORDER BY id")],
                             before)
            records = connection.execute("SELECT identity_key, name_source, name_source_ref FROM accounts")
            self.assertEqual({row["identity_key"]: (row["name_source"], row["name_source_ref"]) for row in records}, {
                "lake": ("organizer", "cvent:annual"), "unlinked": ("unreviewed", None), "protected": ("manual", None),
            })
            self.assertEqual(connection.execute("""
                SELECT COUNT(*) FROM audit_events WHERE action = 'account_name_source_recorded'
            """).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
