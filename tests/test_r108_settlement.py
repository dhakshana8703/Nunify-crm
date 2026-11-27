"""R1-08.1 and R1-08.2 on disposable CRM databases."""

import os
import sqlite3
import unittest
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import worker
from account_merge_store import SqlAccountMergeStore
from app import create_app
from crm_core.account_merge import plan_merge
from crm_core.growth import GrowthConflict, request_enrichment, retry_enrichment
from crm_store import connect
from growth_ops import enrich_one
from growth_store import SqlGrowthStore, initialize
from legacy_contact_store import APOLLO_LEADS_SCHEMA
import test_r106_stages
from test_postgres_pilot import PostgresDatabase, TEST_URL
from workflow_store import save_suppression_rule, transaction


OBSERVED = test_r106_stages.OBSERVED


class CreditSettlementTest(unittest.TestCase):
    database_target = test_r106_stages.AccountStageTest.database_target
    make_account = test_r106_stages.AccountStageTest.make_account
    prepare_search = test_r106_stages.AccountStageTest.prepare_search

    def setUp(self):
        self.database = self.database_target()
        create_app(self.database, demo=True)

    def prepare_live(self, db, key, *, unlock=False):
        account_id = self.prepare_search(db, key)
        if unlock:
            with transaction(db):
                person = db.execute("""
                    INSERT INTO people(account_id, name, origin, apollo_person_id)
                    VALUES (?, 'Sam Example', 'apollo', 'example-id')
                """, (account_id,)).lastrowid
                db.execute("UPDATE credit_jobs SET action = 'email_unlock', person_id = ? WHERE account_id = ?",
                           (person, account_id))
        return account_id

    def provider(self, payload=None, *, error=None):
        return Mock(requires_confirmation=True, return_value=payload, side_effect=error)

    def test_r108_zero_confirmation_records_no_charge(self):
        with closing(connect(self.database)) as db:
            self.prepare_live(db, "free-search")
            provider = self.provider({"credits": 0})
            job = enrich_one(db, provider, clock=lambda: OBSERVED)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 0)
            self.assertEqual(SqlGrowthStore(db).credit_job(job)["state"], "done")

    def test_r108_fractional_charge_survives_failed_publication_and_cannot_retry(self):
        with closing(connect(self.database)) as db:
            account = self.prepare_live(db, "confirmed-charge", unlock=True)
            provider = self.provider({"credits": "0.75", "email": "sam@confirmed-charge.example"})
            with patch.object(SqlGrowthStore, "save_unlocked_email", side_effect=sqlite3.OperationalError):
                job_id = enrich_one(db, provider, clock=lambda: OBSERVED)
            store = SqlGrowthStore(db)
            row = db.execute("SELECT * FROM credit_ledger WHERE job_id = ?", (job_id,)).fetchone()
            self.assertEqual(Decimal(str(row["credits"])), Decimal("0.75"))
            self.assertEqual(row["account_id"], account)
            self.assertEqual(row["source"], "apollo")
            with self.assertRaisesRegex(GrowthConflict, "reconcil"):
                retry_enrichment(store, {"id": store.credit_job(job_id)["actor_id"]}, job_id, OBSERVED)
            self.assertIsNone(enrich_one(db, provider, clock=lambda: OBSERVED + timedelta(minutes=3)))
            provider.assert_called_once()

    def test_r108_lost_response_never_sends_paid_job_twice(self):
        with closing(connect(self.database)) as db:
            self.prepare_live(db, "unknown-charge", unlock=True)
            provider = self.provider(error=TimeoutError)
            job_id = enrich_one(db, provider, clock=lambda: OBSERVED)
        with closing(connect(self.database)) as db:
            store = SqlGrowthStore(db)
            with self.assertRaisesRegex(GrowthConflict, "reconcil"):
                retry_enrichment(store, {"id": store.credit_job(job_id)["actor_id"]}, job_id, OBSERVED)
            self.assertIsNone(enrich_one(db, provider, clock=lambda: OBSERVED + timedelta(minutes=3)))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 0)
        provider.assert_called_once()

    def test_r108_late_confirmation_posts_once_and_suppression_prevents_disclosure(self):
        with closing(connect(self.database)) as db:
            account = self.prepare_live(db, "suppressed-in-flight", unlock=True)
            store = SqlGrowthStore(db)
            replay = self.provider(error=AssertionError("Do not dispatch again"))
            later = OBSERVED + timedelta(minutes=4)

            def reveal(job, *, idempotency_key):
                with closing(connect(self.database)) as other, transaction(other):
                    save_suppression_rule(other, "domain", job["domain"], "opt_out", job["actor_id"], later,
                                          source="manual", reason_note="")
                with closing(connect(self.database)) as other:
                    self.assertIsNone(enrich_one(other, replay, clock=lambda: later))
                return {"credits": "0.125", "email": f"sam@{job['domain']}", "email_verified": True}

            provider = Mock(requires_confirmation=True, side_effect=reveal)
            job_id = enrich_one(db, provider, clock=lambda: OBSERVED)
            job = store.credit_job(job_id)
            self.assertEqual(job["state"], "blocked")
            self.assertEqual(job["reconciliation_required"], 0)
            self.assertIn("Suppression", job["error"])
            self.assertFalse(store.person(account, job["person_id"])["email"])
            postings = db.execute("SELECT credits FROM credit_ledger WHERE job_id = ?", (job_id,)).fetchall()
            self.assertEqual([Decimal(str(row[0])) for row in postings], [Decimal("0.125")])
            self.assertIsNone(enrich_one(db, provider, clock=lambda: later))
            replay.assert_not_called()
            provider.assert_called_once()

    def test_r108_saved_key_does_not_activate_the_offline_worker(self):
        with closing(connect(self.database)) as db:
            self.prepare_live(db, "offline-mode")
            self.prepare_live(db, "requires-apollo", unlock=True)
        args = ["worker.py", "--offline", "--once", "--db", str(self.database)]
        with (patch("sys.argv", args), patch.dict(os.environ, {"APOLLO_API_KEY": "unused-fixture-key"}),
              patch("apollo_provider.ApolloProvider") as apollo):
            worker.main()
        apollo.assert_not_called()
        with closing(connect(self.database)) as db:
            self.assertEqual([tuple(r) for r in db.execute("SELECT action, state FROM credit_jobs ORDER BY id")],
                             [("people_search", "done"), ("email_unlock", "queued")])
            self.assertEqual([tuple(r) for r in db.execute("SELECT source, credits FROM credit_ledger")],
                             [("offline", 1)])

    def test_r108_explicit_worker_mode_links_search_and_unlock(self):
        with closing(connect(self.database)) as db:
            account_id = self.prepare_live(db, "explicit-worker")
        args = ["worker.py", "--live-apollo", "--once", "--db", str(self.database)]
        provider = Mock(requires_confirmation=True, side_effect=[
            {"credits": 0, "name": "Sam Example", "title": "Head of Events", "apollo_person_id": "exact-person"},
            {"credits": "0.375", "email": "sam@explicit-worker.example", "email_verified": True},
        ])
        with (patch("sys.argv", args), patch.dict(os.environ, {"APOLLO_API_KEY": "fixture-key"}),
              patch("apollo_provider.ApolloProvider", return_value=provider) as adapter):
            worker.main()
            with closing(connect(self.database)) as db, transaction(db):
                person = db.execute("SELECT * FROM people WHERE account_id = ?", (account_id,)).fetchone()
                self.assertEqual(person["apollo_person_id"], "exact-person")
                self.assertFalse(person["unlocked"])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 0)
                actor = db.execute("SELECT * FROM users WHERE role = 'manager'").fetchone()
                store = SqlGrowthStore(db)
                account = store.account(account_id, OBSERVED.date().isoformat())
                self.assertTrue(request_enrichment(store, account, person["id"], actor, observed=OBSERVED))
            worker.main()
        self.assertEqual(adapter.call_count, 2)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(provider.call_args.args[0]["apollo_person_id"], "exact-person")
        with closing(connect(self.database)) as db:
            receipt = db.execute("SELECT * FROM credit_ledger").fetchone()
            self.assertEqual(receipt["source"], "apollo")
            self.assertEqual(Decimal(str(receipt["credits"])), Decimal("0.375"))
            unlocked = SqlGrowthStore(db).person(account_id, receipt["person_id"])
            self.assertEqual(unlocked["email"], "sam@explicit-worker.example")
            self.assertEqual(unlocked["email_verified"], 1)

    def test_r108_search_reuses_previously_unlocked_cvent_person(self):
        with closing(connect(self.database)) as db:
            account_id = self.prepare_live(db, "imported-identity")
            with transaction(db):
                db.execute(APOLLO_LEADS_SCHEMA)
                person_id = db.execute("""
                    INSERT INTO people(account_id, name, email, unlocked, origin)
                    VALUES (?, 'Saved Sam', 'sam@imported-identity.example', 1, 'apollo_cvent')
                """, (account_id,)).lastrowid
                db.execute("""
                    INSERT INTO cvent_apollo_leads(apollo_id, person_id, legacy_company_id, company_domain, imported_at)
                    VALUES ('saved-person-id', ?, 1, 'imported-identity.example', ?)
                """, (person_id, OBSERVED.isoformat()))
            provider = self.provider({
                "credits": 0, "name": "Sam Example", "title": "Head of Events", "apollo_person_id": "saved-person-id",
            })
            enrich_one(db, provider, clock=lambda: OBSERVED)
            people = db.execute("SELECT * FROM people WHERE account_id = ?", (account_id,)).fetchall()
            self.assertEqual([person["id"] for person in people], [person_id])
            self.assertEqual(people[0]["apollo_person_id"], "saved-person-id")
            self.assertEqual(people[0]["unlocked"], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 0)

    def test_r108_upgrade_preserves_offline_ids_and_accepts_verified_history(self):
        with closing(connect(self.database)) as db:
            self.prepare_live(db, "legacy-upgrade")
            job = db.execute("SELECT * FROM credit_jobs").fetchone()
            with transaction(db):
                db.execute("DROP TABLE credit_ledger")
                db.execute("""
                    CREATE TABLE credit_ledger (
                        id INTEGER PRIMARY KEY,
                        job_id INTEGER NOT NULL UNIQUE REFERENCES credit_jobs(id),
                        user_id INTEGER NOT NULL REFERENCES users(id),
                        account_id INTEGER NOT NULL REFERENCES accounts(id),
                        person_id INTEGER REFERENCES people(id),
                        action TEXT NOT NULL,
                        credits INTEGER NOT NULL CHECK (credits > 0),
                        on_day TEXT NOT NULL,
                        at TEXT NOT NULL
                    )
                """)
                db.execute("""
                    INSERT INTO credit_ledger(job_id, user_id, account_id, action, credits, on_day, at)
                    VALUES (?, ?, ?, 'people_search', 1, ?, ?)
                """, (job["id"], job["actor_id"], job["account_id"], job["on_day"], job["created_at"]))
            initialize(db)
            initialize(db)
            self.assertEqual(tuple(db.execute("SELECT id, job_id, source FROM credit_ledger").fetchone()),
                             (1, job["id"], "offline"))
            with transaction(db):
                for action, amount, source in (
                    ("org_match", "0.25", "historical"), ("phone_unlock", "1.5", "historical"),
                    ("opening_balance_adjustment", "-0.5", "adjustment"),
                ):
                    db.execute("""
                        INSERT INTO credit_ledger(action, credits, on_day, at, source, reference)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (action, amount, job["on_day"], job["created_at"], source, action))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 4)
            self.assertFalse(db.execute("SELECT 1 FROM credit_ledger WHERE user_id IS NULL AND job_id IS NOT NULL").fetchone())

    def test_r108_merge_blocks_a_repeated_provider_identity(self):
        with closing(connect(self.database)) as db:
            with transaction(db):
                accounts = [self.make_account(db, key) for key in ("identity-first", "identity-other")]
                for account in accounts:
                    db.execute("""
                        INSERT INTO people(account_id, name, origin, apollo_person_id)
                        VALUES (?, 'Sam Example', 'apollo', 'same-person-id')
                    """, (account,))
            conflicts = plan_merge(SqlAccountMergeStore(db), accounts, observed=OBSERVED)["conflicts"]
            self.assertTrue(any("Apollo person ID" in conflict for conflict in conflicts))

    def test_r108_fractional_quota_usage_is_exact(self):
        with closing(connect(self.database)) as db, transaction(db):
            actor = db.execute("SELECT id FROM users WHERE role = 'sdr'").fetchone()[0]
            day = OBSERVED.date().isoformat()
            db.executemany("""
                INSERT INTO credit_ledger(user_id, action, credits, on_day, at, source, reference)
                VALUES (?, 'email_unlock', 999999999.999999, ?, ?, 'historical', ?)
            """, [(actor, day, OBSERVED.isoformat(), f"credit-{index}") for index in range(100)])
            self.assertEqual(SqlGrowthStore(db).credit_usage(actor, day), Decimal("99999999999.999900"))

    def test_r108_queued_sdr_job_rechecks_quota_on_its_next_day(self):
        with closing(connect(self.database)) as db:
            account_id = self.prepare_live(db, "overnight-queue")
            self.prepare_live(db, "another-queued-company")
            with transaction(db):
                actor = db.execute("SELECT id FROM users WHERE role = 'sdr' ORDER BY id LIMIT 1").fetchone()[0]
                db.execute("UPDATE credit_jobs SET actor_id = ? WHERE account_id = ?", (actor, account_id))
                db.execute("INSERT INTO account_work(account_id, owner_id) VALUES (?, ?)", (account_id, actor))
            store = SqlGrowthStore(db)
            later = OBSERVED + timedelta(days=1)
            day = later.date().isoformat()
            self.assertEqual(store.credit_usage(actor, day), 0)
            provider = Mock(return_value={"name": "Sam Example", "title": "Head of Events"})
            job_id = enrich_one(db, provider, clock=lambda: later)
            self.assertEqual(store.credit_job(job_id)["on_day"], day)
            self.assertEqual(store.credit_usage(actor, day), 1)
            provider.assert_called_once()


@unittest.skipUnless(TEST_URL, "Set CRM_TEST_POSTGRES_URL for disposable PostgreSQL tests.")
class PostgresCreditSettlementTest(PostgresDatabase, CreditSettlementTest):
    pass
