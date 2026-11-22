import io
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from app import create_app
from crm_store import connect, import_snapshot
from directory_source import load_directory_records
from growth_ops import OfflineApollo, enrich_one
from import_demo import ACCOUNTS, SOURCE_PARENT, load_cvent_records
from workflow_store import DEMO_PASSWORD


class GrowthOpsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_cvent_records(SOURCE_PARENT / "cvent-scrapper")
        cls.records += load_directory_records(SOURCE_PARENT / "association")

    def setUp(self):
        instance = Path(__file__).resolve().parents[1] / "instance"
        self.directory = instance / f"growth-test-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(self._clean_files)
        self.database = self.directory / "growth.sqlite3"
        result = import_snapshot(self.database, ACCOUNTS, self.records)
        self.aarp = next(row["id"] for row in result["accounts"] if row["identity_key"] == "aarp")
        self.app = create_app(self.database, demo=True)
        self.app.testing = True
        self.clock = datetime(2026, 11, 20, 18, 29, tzinfo=timezone.utc)
        self.app.config["CRM_CLOCK"] = self._now
        self.manager = self._login("manager", "/assignment")
        self.rep = self._login("riya", "/my-accounts")
        self.admin = self._login("admin", "/operations")

    def _clean_files(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def _now(self):
        return self.clock

    def _login(self, actor, landing):
        client = self.app.test_client()
        self.assertEqual(client.get("/login").status_code, 200)
        response = self._post(client, "/login", email=f"{actor}@example.test", password=DEMO_PASSWORD)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(client.get(landing).status_code, 200)
        return client

    def _post(self, client, url, **data):
        with client.session_transaction() as session:
            token = session["csrf"]
        return client.post(url, data={"csrf_token": token, **data})

    def _assign_and_qualify(self):
        with closing(connect(self.database)) as connection:
            self.owner = connection.execute("SELECT id FROM users WHERE email = 'riya@example.test'").fetchone()[0]
        assigned = self._post(self.manager, f"/assignment/{self.aarp}", owner_id=self.owner, revision=0)
        self.assertEqual(assigned.status_code, 303)
        evidence = io.BytesIO(b"<html><h1>Annual Meeting</h1><a href='https://web.cvent.com/event/demo'>Register</a></html>")
        qualified = self._post(self.manager, f"/accounts/{self.aarp}/qualify",
                               evidence=(evidence, "saved-event.html"), evidence_url="https://aarp.org/events")
        self.assertEqual(qualified.status_code, 303)
        self.assertIn(b"Registration platform - Cvent", self.manager.get(qualified.location).data)
        return f"/accounts/{self.aarp}/enrich"

    def _worker(self, provider):
        with closing(connect(self.database)) as connection:
            return enrich_one(connection, provider, clock=self._now)

    def test_local_midnight_resets_quota_without_repeating_provider_spend(self):
        route = self._assign_and_qualify()
        self.assertEqual(self.rep.get("/operations").status_code, 403)
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE users SET daily_credit_quota = 1 WHERE id = ?", (self.owner,))
        self.assertEqual(self._post(self.rep, route).status_code, 303)
        self.assertEqual(self._post(self.rep, route).status_code, 303)
        offline = Mock(side_effect=OfflineApollo())
        self.assertEqual(self._worker(offline), 1)
        self.assertIsNone(self._worker(offline))
        self.assertEqual(offline.call_count, 1)
        with closing(connect(self.database)) as connection:
            contact = connection.execute("SELECT id FROM people WHERE origin = 'apollo_offline'").fetchone()[0]
        denied = self._post(self.rep, route, person_id=contact)
        self.assertEqual(denied.status_code, 409)
        self.assertIn(b"resets at local midnight", denied.data)
        self.clock += timedelta(minutes=1)
        self.assertEqual(self._post(self.rep, route, person_id=contact).status_code, 303)
        self.assertEqual(self._worker(offline), 2)
        with closing(connect(self.database)) as connection:
            ledger = connection.execute("SELECT on_day, credits FROM credit_ledger ORDER BY id").fetchall()
            self.assertEqual([tuple(row) for row in ledger], [("2026-11-20", 1), ("2026-11-21", 1)])
            unlocked = connection.execute("SELECT unlocked, email_verified FROM people WHERE id = ?", (contact,)).fetchone()
            self.assertEqual(tuple(unlocked), (1, 1))

    def test_suppression_added_after_queue_blocks_worker_before_vendor(self):
        route = self._assign_and_qualify()
        self.assertEqual(self._post(self.rep, route).status_code, 303)
        self.assertEqual(self._post(self.admin, "/suppression", kind="account", value=self.aarp,
                                    reason="Test outreach restriction").status_code, 303)
        offline = Mock(side_effect=OfflineApollo())
        self.assertEqual(self._worker(offline), 1)
        offline.assert_not_called()
        with closing(connect(self.database)) as connection:
            job = connection.execute("SELECT state, error FROM credit_jobs").fetchone()
            self.assertEqual(job["state"], "blocked")
            self.assertIn("suppression changed", job["error"].casefold())
            self.assertEqual(connection.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 0)

    def test_manager_can_queue_a_midrange_fit_score(self):
        route = self._assign_and_qualify()
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE qualifications SET score = 45 WHERE account_id = ?", (self.aarp,))
        unapproved = self._post(self.rep, route)
        self.assertEqual(unapproved.status_code, 403)
        self.assertIn(b"manager must approve", unapproved.data)
        self.assertEqual(self._post(self.manager, route).status_code, 303)
        with closing(connect(self.database)) as connection:
            queued = connection.execute("""
                SELECT u.role, j.state FROM credit_jobs j JOIN users u ON u.id = j.actor_id WHERE j.account_id = ?
            """, (self.aarp,)).fetchone()
            self.assertEqual(tuple(queued), ("manager", "queued"))

    def test_accepted_provider_result_survives_failed_local_write_and_retry(self):
        route = self._assign_and_qualify()
        self.assertEqual(self._post(self.rep, route).status_code, 303)
        with closing(connect(self.database)) as connection, connection:
            connection.execute("""
                CREATE TRIGGER simulate_failed_local_write BEFORE INSERT ON people
                WHEN NEW.origin = 'apollo_offline'
                BEGIN SELECT RAISE(ABORT, 'Synthetic test failure'); END
            """)
        accepted = {}
        requests = []

        def idempotent_provider(job, idempotency_key):
            requests.append(idempotency_key)
            if idempotency_key not in accepted:
                accepted[idempotency_key] = OfflineApollo()(job, idempotency_key)
            return accepted[idempotency_key]

        self.assertEqual(self._worker(idempotent_provider), 1)
        with closing(connect(self.database)) as connection, connection:
            self.assertEqual(connection.execute("SELECT state FROM credit_jobs").fetchone()[0], "failed")
            self.assertEqual(connection.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 0)
            connection.execute("DROP TRIGGER simulate_failed_local_write")
        self.assertIn(b"Provider result pending reconciliation", self.manager.get("/operations").data)
        self.assertEqual(self._post(self.manager, "/operations/credits/1/retry").status_code, 303)
        self.assertEqual(self._worker(idempotent_provider), 1)
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(accepted), 1)
        with closing(connect(self.database)) as connection:
            self.assertEqual(tuple(connection.execute("SELECT state, attempts FROM credit_jobs").fetchone()), ("done", 2))
            self.assertEqual(connection.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT title FROM people WHERE origin = 'apollo_offline'").fetchone()[0],
                             "Director of Meetings")
