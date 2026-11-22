import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import directory_import
from crm_store import SCHEMA
from cvent_import import COMPANY_FILE, DOMAIN_MAP_FILE, FILE_NAME, evaluate, load_rows
from workflow_store import initialize as initialize_workflow


def event(organizer, registration_host="web.cvent.com", **extra):
    event_id = str(uuid4())
    return {
        "event_uuid": event_id,
        "cvent_url": f"https://web.cvent.com/event/{event_id}/summary",
        "resolved_url": f"https://{registration_host}/registration",
        "client_domain": registration_host,
        "organizer": organizer,
        "event_name": "Saved conference",
        "start_date": "2027-01-14",
        "creator_email": "fixture-contact@example.test",
        **extra,
    }


class CventImportTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / "instance" / f"test-cvent-import-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        initialize_workflow(self.connection)
        self.addCleanup(self.connection.close)

    def cleanup(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def load(self, rows, *, companies=None, reviewed=None):
        (self.directory / FILE_NAME).write_text(json.dumps(rows), encoding="utf-8")
        if companies is not None:
            (self.directory / COMPANY_FILE).write_text(json.dumps(companies), encoding="utf-8")
        if reviewed is not None:
            source = "KNOWN_DOMAIN_NAMES = " + repr(reviewed) + "\nraise RuntimeError('Do not execute legacy code')\n"
            (self.directory / DOMAIN_MAP_FILE).write_text(source, encoding="utf-8")
        return load_rows(self.directory)

    def status(self, row, *, promote=False):
        return evaluate(self.connection, row, {}, promote=promote)[0]

    def test_joins_corroborated_domains_and_filters_registration_hosts_and_contacts(self):
        rows = self.load([
            event("Event Host Only", "register.eventbrite.com"),
            event("Conference Platform Only", "somebrand.cventevents.com"),
            event("Independent Industry Forum", "events.forum.test"),
            event("Reviewed Professionals Society"),
            event("Cvent", "spring.cventevents.com"),
            event("Official Education Group", company_domain="group.test"),
            event("Nonmatching Report Company", "registration.other.test"),
        ], companies=[
            {"company_name": "Independent Industry Forum", "domain": "forum.test",
             "p1_list": ["private fixture"]},
            {"company_name": "Nonmatching Report Company", "domain": "mismatch.test"},
        ], reviewed={"professionals.test": "Reviewed Professionals Society"})
        self.assertEqual([self.status(row) for row in rows],
                         ["review", "review", "imported", "imported", "suppressed", "imported", "review"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
        self.assertNotIn("creator_email", rows[2]["raw"])
        self.assertNotIn("p1_list", rows[2]["raw"])
        for row in rows:
            self.status(row, promote=True)
        domains = [row["domain"] for row in self.connection.execute("SELECT domain FROM accounts ORDER BY domain")]
        self.assertEqual(domains, ["forum.test", "group.test", "professionals.test"])
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_replay_uses_existing_directory_account_and_preserves_ownership(self):
        account_id = self.connection.execute("""
            INSERT INTO accounts (identity_key, name, domain, organization_type, owner)
            VALUES ('finance', 'Independent Financial Association', 'finance.test', 'Association', 'Owner')
        """).lastrowid
        self.connection.execute("""
            INSERT INTO source_records (source, source_id, source_url, source_file, account_id, details_json, raw_json)
            VALUES ('directory','listing','https://directory.test/listing','saved.json',?,'{}','{}')
        """, (account_id,))
        saved = event("Independent Financial Association")
        row, = self.load([saved])
        self.assertEqual(self.status(row, promote=True), "imported")
        self.assertEqual(self.status(row, promote=True), "unchanged")
        linked = self.connection.execute("SELECT id, account_id FROM source_records WHERE source = 'cvent'").fetchone()
        self.assertEqual(linked["account_id"], account_id)
        saved["event_name"] = "New event title"
        changed, = self.load([saved])
        self.assertEqual(self.status(changed, promote=True), "imported")
        self.assertEqual(self.connection.execute("SELECT owner FROM accounts").fetchone()[0], "Owner")
        updated = self.connection.execute("SELECT id, account_id FROM source_records WHERE source = 'cvent'").fetchone()
        self.assertEqual(tuple(updated), tuple(linked))
        saved["organizer"] = "Different Organizer"
        conflicting, = self.load([saved])
        self.assertEqual(self.status(conflicting, promote=True), "review")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)

    def test_saved_alias_link_is_independent_of_import_order(self):
        cvent, = self.load([event("Independent Financial Association (IFA)", company_domain="finance.test")])
        listing = {
            "website": "https://www.finance.test/", "type": "National", "category": "Financial",
            "member_count": "2,000", "source_url": f"https://directoryofassociations.com/view/?di={uuid4()}",
        }
        for name, second_result in (
            ("Independent Financial Association", "imported"),
            ("International Financial Alliance (IFA)", "review"),
            ("Independent Financial Association (ABC)", "review"),
        ):
            (self.directory / directory_import.FILE_NAME).write_text(
                json.dumps([{**listing, "name": name}]), encoding="utf-8",
            )
            directory, = directory_import.load_rows(self.directory)
            cvent_input = (cvent, evaluate, {})
            directory_input = (directory, directory_import.evaluate, directory_import.DEFAULT_POLICY)
            for first, second in ((cvent_input, directory_input), (directory_input, cvent_input)):
                with (
                    self.subTest(directory_name=name, first=first[0]["source"]),
                    closing(sqlite3.connect(":memory:")) as db,
                ):
                    db.row_factory = sqlite3.Row
                    db.execute("PRAGMA foreign_keys = ON")
                    db.executescript(SCHEMA)
                    initialize_workflow(db)
                    first_row, promote_first, first_policy = first
                    self.assertEqual(promote_first(db, first_row, first_policy, promote=True)[0], "imported")
                    account_id = db.execute("SELECT id FROM accounts").fetchone()[0]
                    db.execute(
                        "INSERT INTO account_work (account_id, stage, revision) VALUES (?, 'working', 1)",
                        (account_id,),
                    )

                    second_row, check_second, second_policy = second
                    self.assertEqual(check_second(db, second_row, second_policy)[0], second_result)
                    self.assertEqual(check_second(db, second_row, second_policy, promote=True)[0], second_result)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)
                    expected_rows = (first_row, second_row) if second_result == "imported" else (first_row,)
                    expected_evidence = {
                        row["source"]: (account_id, row["source_id"], row["raw"]) for row in expected_rows
                    }
                    evidence = {
                        row["source"]: (row["account_id"], row["source_id"], json.loads(row["raw_json"]))
                        for row in db.execute("SELECT source, account_id, source_id, raw_json FROM source_records")
                    }
                    self.assertEqual(evidence, expected_evidence)
                    self.assertEqual(
                        tuple(db.execute("SELECT stage, revision FROM account_work").fetchone()), ("working", 1),
                    )
                    self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_leaves_conflicting_ids_and_domains_in_review(self):
        shared = event("Named Agency", company_domain="shared.test")
        rows = self.load([shared, event("Different Client", company_domain="shared.test"),
                          event("Mismatched URL", cvent_url="https://not-cvent.test/event/unknown"), shared])
        self.assertEqual([self.status(row, promote=True) for row in rows], ["review", "review", "invalid", "review"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)

    def test_unreliable_dates_stay_in_evidence_but_not_next_event(self):
        rows = self.load([
            event("Independent Society", company_domain="society.test", end_date="2026-01-16"),
            event("Independent Society", company_domain="society.test", end_date="2031-01-16"),
            event("Independent Society", company_domain="society.test", end_date="2027-01-16"),
        ])
        for row in rows:
            self.assertEqual(self.status(row, promote=True), "imported")
        for row in rows[:2]:
            self.assertIn("date_review", row["details"])
            self.assertTrue(row["raw"]["end_date"])
            projected = self.connection.execute(
                "SELECT start_date_iso FROM cvent_read WHERE source_id = ?", (row["source_id"],),
            ).fetchone()
            self.assertEqual(projected[0], "")
        self.assertEqual(rows[2]["details"]["start_date_iso"], "2027-01-14")
