"""R1-06 persisted evidence through production intake, merge and worker entry points."""

import unittest
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from app import create_app
from account_merge_store import SqlAccountMergeStore
from crm_core.account_merge import apply_merge, plan_merge
from crm_core.ingest import evaluate_intake, evaluate_saved
from crm_store import connect
from growth_store import SqlGrowthStore
from ingest_store import SourceStore
from test_postgres_pilot import TEST_URL, PostgresDatabase
from test_source_core import POLICY, event, listing
from workflow_store import DEMO_PASSWORD, account_rows, transaction


BUSINESS_ZONE = ZoneInfo("Asia/Kolkata")


class AccountFactsTest(unittest.TestCase):
    def setUp(self):
        self.database = self.database_target()
        self.app = create_app(self.database, demo=True)
        self.now = datetime.now(timezone.utc)
        self.today = self.now.astimezone(BUSINESS_ZONE).date()

    def database_target(self):
        folder = Path(__file__).resolve().parents[1] / "instance" / f"account-facts-{uuid4().hex[:12]}"
        folder.mkdir(parents=True)
        self.addCleanup(self.clean_up, folder)
        return folder / "facts.sqlite3"

    @staticmethod
    def clean_up(folder):
        for path in folder.iterdir():
            path.unlink()
        folder.rmdir()

    @staticmethod
    def record(source_id, when, domain="trade.example", name="Example Trade Association"):
        item = event(domain, name, source_id)
        item["details"]["start_date_iso"] = when.isoformat() if isinstance(when, date) else when
        item["details"]["country"] = "India"
        return item

    def promote(self, store, record):
        self.assertEqual(evaluate_saved(store, "cvent", record, {}, promote=True)[0], "imported")

    @staticmethod
    def account(connection, domain="trade.example"):
        return connection.execute("SELECT * FROM accounts WHERE root_domain = ?", (domain,)).fetchone()

    def test_cvent_default_and_daily_fact_rollover_with_corrected_dates(self):
        from worker import run_pass

        future = self.today + timedelta(days=3)
        with closing(connect(self.database)) as connection:
            with transaction(connection):
                store = SourceStore(connection)
                self.promote(store, self.record("today", self.today))
                self.promote(store, self.record("future", future))
                self.promote(store, self.record("past", self.today - timedelta(days=2)))
                self.promote(store, self.record("invalid", "9999-02-31"))
                organization = self.account(connection)
                self.assertEqual(organization["organization_type"], "Unknown")
                self.assertEqual(organization["next_event_date"], self.today.isoformat())
                self.assertEqual(organization["events_per_year"], 2)
                self.assertIsNone(organization["country"])
                self.promote(store, self.record("future", future + timedelta(days=1)))
                self.assertEqual(evaluate_saved(
                    store, "cvent", self.record("future", future + timedelta(days=1)), {}, promote=True,
                )[0], "unchanged")
            next_day = self.today + timedelta(days=1)
            midnight = datetime.combine(next_day, time.min, BUSINESS_ZONE)
            with patch("workflow_store.utcnow", return_value=midnight):
                run_pass(connection, None, None, connectors={})
                run_pass(connection, None, None, connectors={})
            organization = self.account(connection)
            self.assertEqual(organization["facts_as_of"], next_day.isoformat())
            self.assertEqual(organization["next_event_date"], (future + timedelta(days=1)).isoformat())
            self.assertEqual(organization["events_per_year"], 2)
            with transaction(connection):
                SqlGrowthStore(connection).save_qualification(organization["id"], 45, [], self.now)
            self.assertEqual(self.account(connection)["facts_as_of"], next_day.isoformat())
            self.assertEqual(self.account(connection)["next_event_date"], (future + timedelta(days=1)).isoformat())
            after_last_event = (future + timedelta(days=2)).isoformat()
            self.assertIsNone(account_rows(connection, after_last_event, organization["id"])["next_event"])

    def test_approved_roster_sets_platform_without_event_and_directory_supplies_country(self):
        with closing(connect(self.database)) as connection, transaction(connection):
            store = SourceStore(connection)
            directory = listing()
            directory["details"]["country"] = "US"
            self.assertEqual(evaluate_saved(store, "directory", directory, POLICY, promote=True)[0], "imported")
            directory_account = self.account(connection)
            self.assertEqual(directory_account["organization_type"], "Association")
            SqlGrowthStore(connection).save_qualification(
                directory_account["id"], 65, [{"label": "Registration platform - Swoogo"}], self.now,
            )
            detected = self.account(connection)
            self.assertEqual((detected["platform"], detected["platform_detail"], detected["platform_confidence"]),
                             ("other_detected", "Swoogo", "detected"))
            payload = {"name": "Example Trade Association", "domain": "trade.example", "source_id": "company:92"}
            self.assertEqual(evaluate_intake(store, "cvent", payload)[0], "imported")
            profile = self.account(connection)
            self.assertEqual((profile["platform"], profile["platform_confidence"]), ("cvent", "confirmed"))
            self.assertEqual((profile["members"], profile["country"], profile["region"]), (2500, "United States", "US"))
            self.assertIsNone(profile["next_event_date"])
            self.assertIsNone(profile["events_per_year"])
            org_id = profile["id"]
        client = self.app.test_client()
        client.get("/login")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        self.assertEqual(client.post("/login", data={
            "email": "manager@example.test", "password": DEMO_PASSWORD, "csrf_token": csrf,
        }).status_code, 303)
        page = client.get(f"/accounts/{org_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Confirmed by Cvent company evidence", page.data)
        self.assertIn(b"Sales region</dt><dd>US", page.data)

    def test_merge_refreshes_survivor_and_failing_intake_leaves_facts_unchanged(self):
        with closing(connect(self.database)) as connection:
            with transaction(connection):
                store = SourceStore(connection)
                self.promote(store, self.record("later", self.today + timedelta(days=5)))
                self.promote(store, self.record("early", self.today + timedelta(days=1), "other.example", "Other Group"))
                winner, other = (self.account(connection, domain)["id"] for domain in ("trade.example", "other.example"))
                admin = connection.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()[0]
                adapter = SqlAccountMergeStore(connection)
                plan = plan_merge(adapter, [winner, other], observed=self.now)
                self.assertEqual(apply_merge(adapter, plan, actor_id=admin, observed=self.now), winner)
                self.assertEqual(self.account(connection)["next_event_date"], (self.today + timedelta(days=1)).isoformat())
            second = self.record("failing", self.today)
            with self.assertRaisesRegex(RuntimeError, "fixture refresh failure"), transaction(connection):
                with patch("ingest_store.refresh_account_facts", side_effect=RuntimeError("fixture refresh failure")):
                    self.promote(store, second)
            self.assertIsNone(store.saved_record("cvent", "failing"))
            self.assertEqual(self.account(connection)["next_event_date"], (self.today + timedelta(days=1)).isoformat())


@unittest.skipUnless(TEST_URL, "Use CRM_TEST_POSTGRES_URL for a disposable PostgreSQL test database.")
class PostgresAccountFactsTest(PostgresDatabase, AccountFactsTest):
    pass


if __name__ == "__main__":
    unittest.main()
