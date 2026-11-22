import json
import re
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from app import create_app
from crm_store import connect, import_snapshot
from directory_source import load_directory_records
from import_demo import ACCOUNTS, AARP_EVENTS, CVENT_EXPORT, SOURCE_PARENT, import_demo, load_cvent_records
from workflow_store import DEMO_PASSWORD


CVENT_DIR = SOURCE_PARENT / "cvent-scrapper"
DIRECTORY_DIR = SOURCE_PARENT / "association"
PARENT_LISTING = "20BF96A7-8948-42A9-806C-C8880869AE3E"
STAGED_LISTINGS = {
    "3A137753-0474-45FB-B646-BE89EEC1D64A",  # Caregiving program
    "74031B9D-44C3-4CCF-8123-CC77414C2220",  # NRTA division
    "3B356874-E4E5-4B78-A008-F6AC437630BA",  # Utah state office
    "E36C32EE-7FEA-42C5-BA71-2F49EEADEA3F",  # Missing website
}


class TwoSourceDemoTest(unittest.TestCase):
    def setUp(self):
        instance = Path(__file__).resolve().parents[1] / "instance"
        self.directory = instance / f"test-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(self.remove_test_files)
        self.database = self.directory / "demo.sqlite3"

    def remove_test_files(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def saved_state(self):
        with closing(connect(self.database, read_only=True)) as connection:
            accounts = connection.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            evidence = connection.execute("SELECT * FROM source_records ORDER BY id").fetchall()
        return [dict(row) for row in accounts], [dict(row) for row in evidence]

    def test_replaying_the_saved_exports_preserves_all_ids_links_and_values(self):
        first_result = import_demo(self.database, CVENT_DIR, DIRECTORY_DIR)
        first_state = self.saved_state()
        second_result = import_demo(self.database, CVENT_DIR, DIRECTORY_DIR)
        self.assertEqual(self.saved_state(), first_state)
        self.assertEqual(first_result.pop("changed_records"), 9)
        self.assertEqual(second_result.pop("changed_records"), 0)
        self.assertEqual(second_result, first_result)
        self.assertEqual(second_result["source_records"], 9)
        self.assertEqual(second_result["linked_records"], 5)
        self.assertEqual(second_result["staged_records"], 4)
        accounts = {row["identity_key"]: row for row in second_result["accounts"]}
        self.assertEqual(set(accounts), {"aarp", "ctaa"})
        self.assertEqual((accounts["aarp"]["event_count"], accounts["aarp"]["listing_count"]), (3, 1))
        self.assertEqual((accounts["ctaa"]["event_count"], accounts["ctaa"]["listing_count"]), (0, 1))
        self.assertEqual({
            row["source_id"] for row in first_state[1] if row["account_id"] is None
        }, STAGED_LISTINGS)

    def test_refresh_changes_only_its_source_and_preserves_ownership(self):
        cvent_export = self.directory / CVENT_EXPORT
        cvent_rows = [record["raw"] for record in load_cvent_records(CVENT_DIR)]
        cvent_export.write_text(json.dumps(cvent_rows), encoding="utf-8")
        directory_rows = {}
        for record in load_directory_records(DIRECTORY_DIR):
            directory_rows.setdefault(record["source_file"], []).append(record["raw"])
        for filename, rows in directory_rows.items():
            (self.directory / filename).write_text(json.dumps(rows), encoding="utf-8")

        import_demo(self.database, self.directory, self.directory)
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE accounts SET owner = ? WHERE identity_key = 'aarp'", ("Demo SDR",))
        original_accounts, original_records = self.saved_state()
        self.assertEqual(original_accounts[0]["owner"], "Demo SDR")
        aarp_listing = next(
            row for row in directory_rows["all_associations.json"] if PARENT_LISTING in row["source_url"]
        )
        aarp_listing["member_count"] = "31,500,000"
        (self.directory / "all_associations.json").write_text(
            json.dumps(directory_rows["all_associations.json"]), encoding="utf-8",
        )
        update = import_demo(self.database, self.directory, self.directory)
        self.assertEqual(update["changed_records"], 1)
        accounts, records = self.saved_state()
        self.assertEqual(accounts, original_accounts)
        self.assertEqual(
            [row for row in records if row["source_id"] != PARENT_LISTING],
            [row for row in original_records if row["source_id"] != PARENT_LISTING],
        )
        parent = next(row for row in records if row["source_id"] == PARENT_LISTING)
        self.assertEqual(json.loads(parent["raw_json"])["member_count"], "31,500,000")
        self.assertEqual(json.loads(parent["details_json"])["members"], "31,500,000")
        self.assertEqual(parent["id"], next(row["id"] for row in original_records if row["source_id"] == PARENT_LISTING))

        cvent_rows[0]["venue_name"] = "Updated source venue"
        cvent_export.write_text(json.dumps(cvent_rows), encoding="utf-8")
        self.assertEqual(import_demo(self.database, self.directory, self.directory)["changed_records"], 1)
        refreshed_accounts, refreshed_records = self.saved_state()
        self.assertEqual(refreshed_accounts, accounts)
        self.assertEqual(
            [row for row in refreshed_records if row["source_id"] != AARP_EVENTS[0]],
            [row for row in records if row["source_id"] != AARP_EVENTS[0]],
        )
        event = next(row for row in refreshed_records if row["source_id"] == AARP_EVENTS[0])
        self.assertEqual(json.loads(event["raw_json"])["venue_name"], "Updated source venue")

    def test_unapproved_relink_rolls_back_the_whole_batch(self):
        import_demo(self.database, CVENT_DIR, DIRECTORY_DIR)
        before = self.saved_state()
        events = load_cvent_records(CVENT_DIR)
        events[0]["details"]["event_name"] = "Updated source event"
        events[1]["identity_key"] = "ctaa"
        with self.assertRaisesRegex(ValueError, "Review required"):
            import_snapshot(self.database, ACCOUNTS, events)
        self.assertEqual(self.saved_state(), before)

    def test_ui_and_legacy_read_routes_use_the_shared_account(self):
        snapshot = import_demo(self.database, CVENT_DIR, DIRECTORY_DIR)
        aarp, ctaa = snapshot["accounts"]
        client = create_app(self.database, demo=True).test_client()
        login = client.get("/login")
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', login.data)[1].decode()
        signed_in = client.post("/login", data={
            "csrf_token": csrf, "email": "manager@example.test", "password": DEMO_PASSWORD,
        })
        self.assertEqual(signed_in.status_code, 303)

        self.assertEqual(client.get("/").location, "/assignment")
        shared = client.get(f"/accounts/{aarp['id']}")
        self.assertEqual(shared.status_code, 200)
        self.assertIn(b"AARP WY 2026 Volunteer Summit", shared.data)
        self.assertIn(b"American Association of Retired Persons", shared.data)
        self.assertIn(b"32,000,000", shared.data)
        self.assertIn(PARENT_LISTING.encode(), shared.data)
        self.assertNotIn(b"National Retired Teachers Association", shared.data)

        directory_only = client.get(f"/accounts/{ctaa['id']}")
        self.assertEqual(directory_only.status_code, 200)
        self.assertIn(b"Canadian Technical Asphalt Association", directory_only.data)
        self.assertNotIn(b"AARP WY 2026 Volunteer Summit", directory_only.data)
        review = client.get("/review")
        self.assertEqual(review.status_code, 200)
        self.assertIn(b"National Retired Teachers Association", review.data)
        self.assertIn(b"Delaware Nurses Association", review.data)
        missing_domain = client.get("/review?status=no-domain&sort=id")
        self.assertIn(b"Delaware Nurses Association", missing_domain.data)
        self.assertNotIn(b"National Retired Teachers Association", missing_domain.data)

        listing = client.get(f"/api/directory/{PARENT_LISTING}").get_json()
        self.assertEqual(listing["account_id"], aarp["id"])
        self.assertEqual(listing["raw"]["member_count"], "32,000,000")
        self.assertEqual(listing["details"]["city"], "Washington")
        events = client.get("/api/events?search=Volunteer+Summit&limit=1").get_json()
        self.assertEqual(events["total"], 1)
        event = events["events"][0]
        self.assertEqual(event["company_id"], aarp["id"])
        self.assertEqual(event["source_id"], AARP_EVENTS[0])
        self.assertEqual(event["start_date_iso"], "2026-09-30")
        detail = client.get(f"/api/events/{event['id']}").get_json()
        self.assertEqual(detail["evidence"]["raw"]["organizer"], "AARP")
        self.assertEqual(detail["evidence"]["raw"]["start_date"], event["start_date"])
        self.assertEqual(client.get(f"/api/events?account_id={ctaa['id']}").get_json()["events"], [])
        self.assertEqual(client.get("/api/events/999999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
