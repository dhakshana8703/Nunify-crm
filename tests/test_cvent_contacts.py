import json
import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

from crm_store import SCHEMA as CRM_SCHEMA
from cvent_contacts import evidence_for_account, import_contacts, initialize
from workflow_store import initialize as initialize_workflow


class CventContactImportTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / "instance" / f"test-cvent-contacts-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.source = self.directory / "events.json"
        self.addCleanup(self.cleanup)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(CRM_SCHEMA)
        initialize_workflow(self.connection)
        self.addCleanup(self.connection.close)
        self.account_id = self.account("partner.test")
        self.admin_id = self.user("admin")
        self.manager_id = self.user("manager")

    def cleanup(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def user(self, role):
        with self.connection:
            return self.connection.execute("""
                INSERT INTO users(name, email, role, timezone, password_hash)
                VALUES ('Fixture', ?, ?, 'UTC', 'unused')
            """, (f"{role}@example.test", role)).lastrowid

    def account(self, domain):
        with self.connection:
            return self.connection.execute("""
                INSERT INTO accounts(identity_key, name, domain, organization_type)
                VALUES (?, 'Fixture Organization', ?, 'Association')
            """, (domain, domain)).lastrowid

    def event(self, name="Taylor James", email="taylor.james@partner.test", account_id=None, **overrides):
        event_id = str(uuid4())
        source_url = f"https://web.cvent.com/event/{event_id}/summary"
        with self.connection:
            self.connection.execute("""
                INSERT INTO source_records(source, source_id, source_url, source_file, account_id, details_json, raw_json)
                VALUES ('cvent', ?, ?, 'events.json', ?, '{"event_name":"Saved Conference"}', '{}')
            """, (event_id, source_url, account_id or self.account_id))
        return {"event_uuid": event_id, "cvent_url": source_url, "contact_name": name, "contact_email": email, **overrides}

    def save(self, *rows):
        self.source.write_text(json.dumps(rows), encoding="utf-8")

    def apply_import(self, actor_id=None):
        return import_contacts(self.connection, self.source, apply=True, actor_id=actor_id or self.admin_id)

    def rows(self, table):
        return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_dry_run_validates_explicit_single_work_contact_and_persisted_event_identity(self):
        duplicate = self.event()
        self.save(
            self.event(),
            self.event("Robin Ford", "r.ford@events.partner.test"),
            self.event(email="outside@unrelated.test"),
            self.event(email="registration@partner.test"),
            self.event(email="taylor.james@partner.test, another@partner.test"),
            self.event(name="Taylor James, Robin Ford"),
            self.event(email="", creator_email="creator@partner.test"),
            self.event(name="Event Planning Team"),
            self.event(cvent_url="https://changed.test/"),
            self.event(email="Taylor <taylor.james@partner.test>"),
            duplicate, duplicate,
            {"event_uuid": str(uuid4()), "contact_name": "Fran Cole", "contact_email": "fran.cole@partner.test"},
            {"event_uuid": "broken"},
        )
        before = self.connection.total_changes
        report = import_contacts(self.connection, self.source)
        self.assertEqual(report["eligible_people"], 2)
        self.assertEqual(report["mismatched_contact_domain_rows"], 1)
        self.assertEqual(report["ambiguous_contact_rows"], 6)
        self.assertEqual(report["mismatched_source_rows"], 1)
        self.assertEqual(report["duplicate_linked_event_rows"], 2)
        self.assertEqual(report["unlinked_event_rows"], 1)
        self.assertEqual(report["invalid_event_rows"], 1)
        self.assertEqual(self.connection.total_changes, before)
        self.assertEqual(evidence_for_account(self.connection, self.account_id), [])

    def test_suppression_and_prior_blocked_contacts_win_during_apply(self):
        excluded = self.account("blocked.test")
        other = self.account("other.test")
        with self.connection:
            self.connection.executemany("""
                INSERT INTO suppression(kind, value, reason, created_at) VALUES (?, ?, ?, '2026-11-20')
            """, [
                ("account", str(excluded), "manual"),
                ("email", "skip@partner.test", "opted_out"),
                ("domain", "*.closed.partner.test", "competitor"),
            ])
            self.connection.execute("""
                INSERT INTO people(account_id, name, email, do_not_contact) VALUES (?, 'Prior Contact', ?, 1)
            """, (other, "declined@partner.test"))
            self.connection.execute("""
                INSERT INTO people(account_id, name, email, status) VALUES (?, 'Prior Contact', ?, 'bounced')
            """, (self.account_id, "bounced@partner.test"))
        self.save(
            self.event(email="fixture@blocked.test", account_id=excluded),
            self.event(email="Skip@Partner.Test"),
            self.event(email="first@closed.partner.test"),
            self.event(email="declined@partner.test"),
            self.event(email="bounced@partner.test"),
            self.event(email="safe@partner.test"),
        )
        report = self.apply_import()
        self.assertEqual(report["suppressed_contact_rows"], 5)
        self.assertEqual(report["new_people"], 1)
        self.assertEqual(self.rows("people"), 3)
        self.assertEqual(self.rows("cvent_contact_evidence"), 1)

    def test_apply_preserves_existing_people_and_paid_history_creates_provenance_and_replays(self):
        with self.connection:
            existing = self.connection.execute("""
                INSERT INTO people(account_id, name, title, email, status, persona, origin, unlocked, email_verified)
                VALUES (?, 'Kept Name', 'Kept Title', 'Taylor.James@Partner.Test', 'replied', 'decision_maker', 'apollo', 1, 1)
            """, (self.account_id,)).lastrowid
            self.connection.execute("""
                INSERT INTO activities(account_id, person_id, actor_id, channel, outcome, direction, occurred_at)
                VALUES (?, ?, ?, 'email', 'replied', 'inbound', '2026-11-20')
            """, (self.account_id, existing, self.admin_id))
        original = tuple(self.connection.execute("SELECT * FROM people WHERE id = ?", (existing,)).fetchone())
        self.save(
            self.event(),
            self.event("Taylor  James", "TAYLOR.JAMES@PARTNER.TEST"),
            self.event("Robin Ford", "r.ford@events.partner.test"),
            self.event("Jamie Dawson", "conflict@partner.test"),
            self.event("Lou Carter", "conflict@partner.test"),
        )
        report = self.apply_import()
        self.assertEqual(report["existing_people"], 1)
        self.assertEqual(report["conflicting_contact_rows"], 2)
        self.assertEqual(report["new_people"], 1)
        self.assertEqual(report["new_evidence"], 3)
        self.assertEqual(tuple(self.connection.execute("SELECT * FROM people WHERE id = ?", (existing,)).fetchone()), original)
        self.assertEqual(self.connection.execute("SELECT person_id FROM activities").fetchone()[0], existing)
        added = self.connection.execute("SELECT origin, unlocked, email_verified, status FROM people WHERE id <> ?", (existing,))
        self.assertEqual(tuple(added.fetchone()), ("cvent", 0, 0, "not_contacted"))
        self.assertEqual({row["event_name"] for row in evidence_for_account(self.connection, self.account_id)}, {"Saved Conference"})
        replay = self.apply_import()
        self.assertEqual(replay["new_people"], 0)
        self.assertEqual(replay["new_evidence"], 0)
        self.assertEqual(self.rows("audit_events"), 1)
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_apply_requires_admin_and_rolls_back_contacts_when_evidence_write_fails(self):
        self.save(self.event())
        with self.assertRaises(PermissionError):
            self.apply_import(self.manager_id)
        self.assertEqual(self.rows("people"), 0)
        with self.connection:
            initialize(self.connection)
            self.connection.execute("""
                CREATE TRIGGER block_evidence BEFORE INSERT ON cvent_contact_evidence
                BEGIN SELECT RAISE(ABORT, 'fixture failure'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply_import()
        self.assertEqual(self.rows("people"), 0)
        self.assertEqual(self.rows("cvent_contact_evidence"), 0)
        self.assertEqual(self.rows("audit_events"), 0)


if __name__ == "__main__":
    unittest.main()
