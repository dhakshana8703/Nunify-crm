"""R1-08.3 and R1-08.5 through signed-in CRM screens and API."""

import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app import create_app
from crm_store import connect
from test_postgres_pilot import PostgresDatabase, TEST_URL
from workflow_store import DEMO_PASSWORD, transaction


OBSERVED = datetime(2026, 11, 20, 18, 30, tzinfo=timezone.utc)


class CreditReportingTest(unittest.TestCase):
    def setUp(self):
        self.database = self.database_target()
        self.app = create_app(self.database, demo=True)
        self.app.testing = True
        self.app.config["CRM_CLOCK"] = lambda: OBSERVED
        with closing(connect(self.database)) as db:
            self.users = {
                row["email"].split("@")[0]: row["id"]
                for row in db.execute("SELECT id, email FROM users")
            }

    def database_target(self):
        folder = Path(__file__).resolve().parents[1] / "instance" / f"credit-reporting-{uuid4().hex}"
        folder.mkdir(parents=True)
        self.addCleanup(folder.rmdir)
        path = folder / "credits.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            self.addCleanup(path.with_name(path.name + suffix).unlink, missing_ok=True)
        return path

    def login(self, name):
        client = self.app.test_client()
        self.assertEqual(client.get("/login").status_code, 200)
        with client.session_transaction() as session:
            token = session["csrf"]
        result = client.post("/login", data={
            "email": f"{name}@example.test", "password": DEMO_PASSWORD, "csrf_token": token,
        })
        self.assertEqual(result.status_code, 303)
        return client

    def seed_ledger(self):
        with closing(connect(self.database)) as db, transaction(db):
            account_id = db.execute("""
                INSERT INTO accounts(identity_key, name, domain, root_domain, organization_type)
                VALUES ('credit-account', 'Credit Account', 'credits.example', 'credits.example', 'Unknown')
            """).lastrowid
            db.execute("UPDATE users SET timezone = 'Asia/Kolkata' WHERE id = ?", (self.users["riya"],))
            for row in (
                (self.users["riya"], "email_unlock", 0.1, "2026-11-20", "2026-11-20T18:29:00+00:00", "apollo", "before"),
                (self.users["riya"], "email_unlock", 1.275, "2026-11-21", OBSERVED.isoformat(), "apollo", "after"),
                (self.users["jordan"], "org_match", 2.5, "2026-11-20", OBSERVED.isoformat(), "historical", "attributed"),
                (None, "phone_unlock", 6.125, "2026-11-21", OBSERVED.isoformat(), "historical", "unattributed"),
                (None, "opening_balance_adjustment", -0.5, "2026-11-21", OBSERVED.isoformat(), "adjustment", "opening"),
                (self.users["riya"], "people_search", 7, "2026-11-21", OBSERVED.isoformat(), "offline", "simulated"),
            ):
                user_id, action, _amount, day, at, source, reference = row
                job_id, person_id = None, None
                linked_account_id = account_id if source in {"apollo", "offline"} else None
                if linked_account_id is not None:
                    if source == "apollo":
                        person_id = db.execute("INSERT INTO people(account_id, name) VALUES (?, ?)",
                                               (account_id, reference)).lastrowid
                    job_id = db.execute("""
                        INSERT INTO credit_jobs(account_id, person_id, actor_id, action, state,
                                                on_day, budget_day, request_key, created_at)
                        VALUES (?, ?, ?, ?, 'done', ?, '2026-11-20', ?, ?)
                    """, (account_id, person_id, user_id, action, day, reference, at)).lastrowid
                db.execute("""
                    INSERT INTO credit_ledger(job_id, account_id, person_id, user_id, action, credits,
                                              on_day, at, source, reference)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (job_id, linked_account_id, person_id, *row))
            db.execute("""
                INSERT INTO credit_jobs(account_id, actor_id, action, state, on_day, budget_day, request_key, created_at)
                VALUES (?, ?, 'email_unlock', 'queued', '2026-11-21', '2026-11-20', 'pending', ?)
            """, (account_id, self.users["riya"], OBSERVED.isoformat()))
            db.execute("""
                INSERT INTO imported_source_evidence(source_slug, external_id, account_id, source_url, observed_at, payload_json)
                VALUES ('cvent', 'event:credit-account', ?, '', ?, '{}')
            """, (account_id, OBSERVED.isoformat()))

    def test_empty_header_operations_and_team_display_zero(self):
        page = self.login("admin").get("/operations")
        self.assertEqual(page.status_code, 200)
        for location in ("header", "operations", "team"):
            with self.subTest(location=location):
                self.assertIn(f'id="credit-{location}-total">0<'.encode(), page.data)

    def test_credit_api_enforces_sign_in_and_roles(self):
        path = "/api/credits/daily"
        self.assertEqual(self.app.test_client().get(path).status_code, 401)
        rep = self.login("riya")
        self.assertEqual(rep.get(path).status_code, 200)
        self.assertEqual(rep.get(path, query_string={"user_id": self.users["jordan"]}).status_code, 403)
        self.assertEqual(self.login("ae").get(path).status_code, 403)
        manager = self.login("manager")
        self.assertEqual(manager.get(path, query_string={"user_id": self.users["riya"]}).status_code, 200)
        self.assertEqual(manager.get(path, query_string={"user_id": self.users["admin"]}).status_code, 403)
        admin = self.login("admin")
        self.assertEqual(admin.get(path, query_string={"user_id": self.users["manager"]}).status_code, 200)
        self.assertEqual(admin.get(path, query_string={"user_id": 1_000_000}).status_code, 404)
        with closing(connect(self.database)) as db, transaction(db):
            db.execute("UPDATE users SET active = 0 WHERE id = ?", (self.users["jordan"],))
        self.assertEqual(admin.get(path, query_string={"user_id": self.users["jordan"]}).status_code, 404)
        for query in ({"user_id": ""}, {"user_id": "1.0"}, {"user_id": "9" * 32}, {"date": "2026-2-7"},
                      {"date": "20261121"}, {"date": "2026-02-30"}):
            with self.subTest(query=query):
                self.assertEqual(admin.get(path, query_string=query).status_code, 400)

    def test_header_operations_team_and_funnel_exclude_offline_credit(self):
        self.seed_ledger()
        for actor in ("admin", "manager"):
            page = self.login(actor).get("/operations")
            self.assertEqual(page.status_code, 200)
            for location in ("header", "operations", "team"):
                with self.subTest(actor=actor, location=location):
                    self.assertIn(f'id="credit-{location}-total">9.5<'.encode(), page.data)
            self.assertIn(b'Unattributed history and adjustments - 5.625 credits', page.data)
            self.assertIn(b'0 delivered, 1.375 credits', page.data)
        rep_home = self.login("riya").get("/today")
        self.assertIn(b'id="credit-header-total">1.375<', rep_home.data)
        self.assertNotIn(b"9.5", rep_home.data)
        ae_home = self.login("ae").get("/handoffs")
        self.assertEqual(ae_home.status_code, 200)
        self.assertNotIn(b"credit-header-total", ae_home.data)

    def test_credit_api_uses_recorded_user_day_and_confirmed_postings(self):
        self.seed_ledger()
        admin = self.login("admin")
        path = "/api/credits/daily"
        for day, expected in ((None, "1.275"), ("2026-11-20", "0.1"), ("2026-11-22", "0")):
            query = {"user_id": self.users["riya"]}
            if day:
                query["date"] = day
            response = admin.get(path, query_string=query)
            with self.subTest(query=query):
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json, {"user_id": self.users["riya"], "date": day or "2026-11-21",
                                                 "credits": expected})
        other_zone = admin.get(path, query_string={"user_id": self.users["jordan"]})
        self.assertEqual(other_zone.json, {"user_id": self.users["jordan"], "date": "2026-11-20", "credits": "2.5"})

    def test_fractional_exact_totals_survive_large_sums(self):
        self.seed_ledger()
        with closing(connect(self.database)) as db, transaction(db):
            account_id = db.execute("SELECT id FROM accounts WHERE identity_key = 'credit-account'").fetchone()[0]
            for index in range(100):
                db.execute("""
                    INSERT INTO credit_ledger(account_id, user_id, action, credits, on_day, at, source, reference)
                    VALUES (?, ?, 'org_match', ?, '2026-11-21', ?, 'historical', ?)
                """, (account_id, self.users["riya"], "999999999.999999", OBSERVED.isoformat(), f"large-{index}"))
        admin = self.login("admin")
        page = admin.get("/operations")
        self.assertIn(b'id="credit-header-total">100000000009.4999<', page.data)
        self.assertIn(b"0 delivered, 100000000001.3749 credits", page.data)
        daily = admin.get("/api/credits/daily", query_string={"user_id": self.users["riya"]})
        self.assertEqual(daily.json["credits"], "100000000001.2749")

    def test_team_queue_shares_team_credits_with_operations(self):
        self.seed_ledger()
        for actor in ("admin", "manager"):
            page = self.login(actor).get("/today")
            self.assertEqual(page.status_code, 200)
            self.assertIn(b'id="credit-header-total">9.5<', page.data)
            self.assertIn(b'id="credit-team-total">9.5<', page.data)
            self.assertIn(b'<strong>Riya Rao</strong> - 1.375 credits', page.data)
        self.assertNotIn(b"credit-team-total", self.login("riya").get("/today").data)

    def test_staff_sees_reconciliation_hold_without_retry_control(self):
        self.seed_ledger()
        with closing(connect(self.database)) as db, transaction(db):
            db.execute("""
                UPDATE credit_jobs SET state = 'failed', reconciliation_required = 1, error = 'Receipt unavailable.'
                WHERE request_key = 'pending'
            """)
            job_id = db.execute("SELECT id FROM credit_jobs WHERE request_key = 'pending'").fetchone()[0]
        page = self.login("admin").get("/operations?filter=failed")
        self.assertIn(b"Needs reconciliation", page.data)
        self.assertIn(b"Receipt unavailable.", page.data)
        self.assertNotIn(f'/operations/credits/{job_id}/retry'.encode(), page.data)


@unittest.skipUnless(TEST_URL, "Set CRM_TEST_POSTGRES_URL to run the PostgreSQL rehearsal.")
class PostgresCreditReportingTest(PostgresDatabase, CreditReportingTest):
    pass
