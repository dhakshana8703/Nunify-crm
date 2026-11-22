import json
import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

from crm_store import SCHEMA
from directory_import import DEFAULT_POLICY, FILE_NAME, evaluate, load_rows
from workflow_store import initialize as initialize_workflow


def listing(name, host, *, key=None, category="Medicine - Healthcare", members="1,000-4,999"):
    return {
        "name": name,
        "website": f"https://{host}/" if host else "",
        "source_url": f"https://directoryofassociations.com/site.asp?di={key or uuid4()}",
        "type": "National",
        "category": category,
        "member_count": members,
        "contact_person": " Listed Coordinator ",
        "contact_title": "Events Manager",
        "phone": "555-0101",
        "conventions_events": "Annual convention",
        "contact_email": "unverified@example.test",
    }


class DirectoryImportTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / "instance" / f"test-directory-import-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(self._cleanup)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        initialize_workflow(self.connection)
        self.addCleanup(self.connection.close)

    def _cleanup(self):
        path = self.directory / FILE_NAME
        path.unlink(missing_ok=True)
        self.directory.rmdir()

    def _load(self, rows):
        (self.directory / FILE_NAME).write_text(json.dumps(rows), encoding="utf-8")
        return load_rows(self.directory)

    def _status(self, row, *, promote=False, policy=DEFAULT_POLICY):
        return evaluate(self.connection, row, policy, promote=promote)[0]

    def test_keeps_filtered_ambiguous_suppressed_and_invalid_listings_in_staging(self):
        rows = self._load([
            listing("Medical Society", "society.test"),
            listing("Society Chapter", "chapter.society.test"),
            listing("Local Hobby Club", "club.test", category="Business Clubs"),
            listing("Business Society", "small.test", members="999"),
            listing("Financial Council", "financial.test", category="Financial"),
            listing("Suppressed Company", "register.cventevents.com"),
            listing("No Site", ""),
            {"name": "Malformed", "source_url": "site.example.test"},
        ])
        self.assertEqual([self._status(row) for row in rows],
                         ["review", "review", "staged", "staged", "imported", "suppressed", "review", "invalid"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
        self.assertEqual(rows[4]["raw"]["contact_person"], " Listed Coordinator ")
        self.assertEqual(rows[4]["details"]["contact_person"], "Listed Coordinator")
        self.assertEqual(rows[4]["details"]["contact_title"], "Events Manager")
        self.assertEqual(rows[4]["details"]["phone"], "555-0101")
        self.assertEqual(rows[4]["details"]["conventions_events"], "Annual convention")
        self.assertNotIn("contact_email", rows[4]["raw"])
        self.assertEqual(self._status(rows[4], promote=True), "imported")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM people").fetchone()[0], 0)
        self.assertEqual(self._status(rows[4], promote=True), "unchanged")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)

    def test_regular_evidence_refresh_preserves_account_and_rejects_identity_change(self):
        data = listing("Financial Council", "financial.test", category="Financial")
        original = {key: value for key, value in data.items()
                    if key not in {"contact_person", "contact_title", "phone", "conventions_events"}}
        row, = self._load([original])
        self.assertEqual(self._status(row, promote=True), "imported")
        linked = self.connection.execute("SELECT source_id, account_id FROM source_records").fetchone()
        self.connection.execute("UPDATE accounts SET owner = 'Original rep' WHERE id = ?", (linked["account_id"],))
        data["member_count"] = "2,000"
        row, = self._load([data])
        self.assertEqual(self._status(row, promote=True), "imported")
        self.assertEqual(self.connection.execute("SELECT owner FROM accounts").fetchone()[0], "Original rep")
        refreshed = self.connection.execute("SELECT source_id, account_id, raw_json FROM source_records").fetchone()
        self.assertEqual(tuple(refreshed)[:2], tuple(linked))
        self.assertEqual(json.loads(refreshed["raw_json"])["member_count"], "2,000")
        self.assertEqual(json.loads(refreshed["raw_json"])["phone"], "555-0101")
        self.assertEqual(self._status(row, promote=True), "unchanged")
        data["website"] = "https://different.test/"
        changed, = self._load([data])
        self.assertEqual(self._status(changed, promote=True), "review")
        self.assertEqual(self.connection.execute("SELECT domain FROM accounts").fetchone()[0], "financial.test")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 1)

    def test_existing_demo_selection_obeys_policy_on_fresh_saved_import(self):
        source_dir = Path(__file__).resolve().parents[2] / "association"
        national_id = "20BF96A7-8948-42A9-806C-C8880869AE3E"
        division_id = "74031B9D-44C3-4CCF-8123-CC77414C2220"
        records = {row["source_id"]: row for row in load_rows(source_dir)
                   if row["source_id"] in {national_id, division_id}}
        national = records[national_id]
        self.assertEqual(national["raw"]["name"], "American Association of Retired Persons")
        self.assertEqual(national["details"]["category"], "Retirement")
        self.assertTrue(national["shared_host"])
        self.assertNotEqual(national["raw"]["source_url"], records[division_id]["raw"]["source_url"])
        self.assertEqual(self._status(national, promote=True), "staged")
        legacy_preview = dict(national, identity_key="aarp", shared_host=False)
        self.assertEqual(self._status(legacy_preview, promote=True), "staged")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
