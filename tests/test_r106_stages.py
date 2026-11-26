"""R1-06.2 account stages through the storage and enrichment paths."""

import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from account_merge_store import SqlAccountMergeStore
from app import create_app
from crm_core.account_merge import apply_merge, plan_merge
from crm_core.growth import request_enrichment
from crm_store import connect, initialize
from growth_ops import enrich_one
from growth_store import SqlGrowthStore
from test_postgres_pilot import PostgresDatabase, TEST_URL
from workflow_store import SCHEMA as WORKFLOW_SCHEMA
from workflow_store import account_rows, save_suppression_rule, transaction


OBSERVED = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
STAGES = (
    "discovered", "qualified", "disqualified", "enriched", "assigned",
    "working", "engaged", "positive", "handed_off", "nurture",
)


class AccountStageTest(unittest.TestCase):
    def setUp(self):
        self.database = self.database_target()

    def database_target(self):
        folder = Path(__file__).resolve().parents[1] / "instance" / f"account-stages-{uuid4().hex}"
        folder.mkdir(parents=True)
        self.addCleanup(folder.rmdir)
        database = folder / "test.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            self.addCleanup(database.with_name(database.name + suffix).unlink, missing_ok=True)
        return database

    def make_account(self, db, key):
        return db.execute("""
            INSERT INTO accounts(identity_key, name, domain, organization_type, root_domain)
            VALUES (?, ?, ?, 'Unknown', ?)
        """, (key, key.title(), f"{key}.example", f"{key}.example")).lastrowid

    def prepare_search(self, db, key, stage=None):
        with transaction(db):
            account_id = self.make_account(db, key)
            actor = db.execute("SELECT * FROM users WHERE role = 'manager'").fetchone()
            if stage is not None:
                owner_id = db.execute("SELECT id FROM users WHERE email = 'riya@example.test'").fetchone()[0]
                db.execute("""
                    INSERT INTO account_work(account_id, owner_id, stage, revision, assigned_at)
                    VALUES (?, ?, ?, 4, '2026-11-25T00:00:00+00:00')
                """, (account_id, owner_id, stage))
            db.execute("""
                INSERT INTO qualifications(account_id, score, signals, observed_at)
                VALUES (?, 90, '[]', ?)
            """, (account_id, OBSERVED.isoformat()))
            self.assertTrue(request_enrichment(
                SqlGrowthStore(db), account_rows(db, OBSERVED.date().isoformat(), account_id),
                None, actor, observed=OBSERVED,
            ))
        return account_id

    def account_work(self, db, account_id):
        return db.execute("SELECT * FROM account_work WHERE account_id = ?", (account_id,)).fetchone()

    def test_stage_values_default_and_reject_invalid_writes(self):
        create_app(self.database)
        with closing(connect(self.database)) as db:
            with transaction(db):
                account_id = self.make_account(db, "stage-values")
                other = self.make_account(db, "stage-invalid")
                db.execute("INSERT INTO account_work(account_id) VALUES (?)", (account_id,))
                self.assertEqual(self.account_work(db, account_id)["stage"], "discovered")
                for stage in STAGES:
                    db.execute("UPDATE account_work SET stage = ? WHERE account_id = ?", (stage, account_id))
                    self.assertEqual(self.account_work(db, account_id)["stage"], stage)
            with self.assertRaises(sqlite3.IntegrityError), transaction(db):
                db.execute("INSERT INTO account_work(account_id, stage) VALUES (?, 'unknown-stage')", (other,))
            with self.assertRaises(sqlite3.IntegrityError), transaction(db):
                db.execute("UPDATE account_work SET stage = 'unknown-stage' WHERE account_id = ?", (account_id,))
            self.assertEqual(self.account_work(db, account_id)["stage"], "nurture")

    def test_stage_migration_preserves_legacy_values_and_owner_updates(self):
        initialize(self.database)
        with closing(connect(self.database)) as db:
            db.executescript(WORKFLOW_SCHEMA)
            with transaction(db):
                survivor = self.make_account(db, "legacy-survivor")
                account_id = self.make_account(db, "legacy-stage")
                db.execute("INSERT INTO account_work(account_id, stage) VALUES (?, 'historic-custom')", (account_id,))
        create_app(self.database, demo=True)
        create_app(self.database)
        with closing(connect(self.database)) as db:
            self.assertEqual(self.account_work(db, account_id)["stage"], "historic-custom")
            with transaction(db):
                owner_id = db.execute("SELECT id FROM users WHERE role = 'sdr' ORDER BY id LIMIT 1").fetchone()[0]
                db.execute("""
                    INSERT INTO account_work(account_id, owner_id, stage, revision)
                    VALUES (?, ?, 'historic-custom', 1)
                    ON CONFLICT(account_id) DO UPDATE SET
                        owner_id = excluded.owner_id, stage = excluded.stage, revision = excluded.revision
                """, (account_id, owner_id))
            self.assertEqual(self.account_work(db, account_id)["owner_id"], owner_id)
            self.assertEqual(self.account_work(db, account_id)["stage"], "historic-custom")
            with self.assertRaises(sqlite3.IntegrityError), transaction(db):
                db.execute("UPDATE account_work SET stage = 'new-custom' WHERE account_id = ?", (account_id,))
            with transaction(db):
                actor_id = db.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()[0]
                store = SqlAccountMergeStore(db)
                plan = plan_merge(store, [survivor, account_id], observed=OBSERVED)
                self.assertEqual(apply_merge(store, plan, actor_id=actor_id, observed=OBSERVED), survivor)
            self.assertEqual(self.account_work(db, survivor)["owner_id"], owner_id)
            self.assertEqual(self.account_work(db, survivor)["stage"], "historic-custom")

    def test_successful_discovery_advances_early_stages_and_replay_preserves_work(self):
        create_app(self.database, demo=True)
        provider = Mock(return_value={"name": "Sam Example", "title": "Director of Events"})
        clock = lambda: OBSERVED
        with closing(connect(self.database)) as db:
            discovered = self.prepare_search(db, "discovered-company")
            self.assertIsNone(self.account_work(db, discovered))
            self.assertIsNotNone(enrich_one(db, provider, clock=clock))
            self.assertEqual(self.account_work(db, discovered)["stage"], "enriched")
            qualified = self.prepare_search(db, "qualified-company", "qualified")
            before = self.account_work(db, qualified)
            self.assertIsNotNone(enrich_one(db, provider, clock=clock))
            updated = self.account_work(db, qualified)
            self.assertEqual(updated["stage"], "enriched")
            self.assertEqual(updated["revision"], 5)
            self.assertEqual(updated["owner_id"], before["owner_id"])
            self.assertEqual(updated["assigned_at"], before["assigned_at"])
            self.assertIsNone(enrich_one(db, provider, clock=clock))
            self.assertEqual(self.account_work(db, qualified)["revision"], 5)
            self.assertEqual(db.execute("SELECT count(*) FROM people").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 2)
        self.assertEqual(provider.call_count, 2)

    def test_later_stage_in_flight_and_email_unlock_preserve_current_work(self):
        create_app(self.database, demo=True)
        with closing(connect(self.database)) as db:
            account_id = self.prepare_search(db, "engaged-company", "qualified")

            def provider(job, idempotency_key):
                with closing(connect(self.database)) as other, transaction(other):
                    other.execute("""
                        UPDATE account_work SET stage = 'engaged', revision = 5 WHERE account_id = ?
                    """, (account_id,))
                return {"name": "Sam Example", "title": "Director of Events"}

            self.assertIsNotNone(enrich_one(db, provider, clock=lambda: OBSERVED))
            state = self.account_work(db, account_id)
            self.assertEqual((state["stage"], state["revision"]), ("engaged", 5))
            person_id = db.execute("SELECT id FROM people WHERE account_id = ?", (account_id,)).fetchone()[0]
            manager = db.execute("SELECT * FROM users WHERE role = 'manager'").fetchone()
            with transaction(db):
                self.assertTrue(request_enrichment(
                    SqlGrowthStore(db), account_rows(db, OBSERVED.date().isoformat(), account_id),
                    person_id, manager, observed=OBSERVED,
                ))
            unlock = Mock(return_value={"email": "contact@engaged-company.example"})
            self.assertIsNotNone(enrich_one(db, unlock, clock=lambda: OBSERVED))
            self.assertEqual(tuple(self.account_work(db, account_id)), tuple(state))

    def test_suppression_and_malformed_results_preserve_stage(self):
        create_app(self.database, demo=True)
        with closing(connect(self.database)) as db:
            held = self.prepare_search(db, "held-company", "qualified")
            invalid = self.prepare_search(db, "invalid-company", "qualified")
            admin_id = db.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()[0]
            with transaction(db):
                save_suppression_rule(db, "account", str(held), "manual", admin_id, OBSERVED)
            provider = Mock(return_value={"name": " ", "title": "Director of Events"})
            self.assertIsNotNone(enrich_one(db, provider, clock=lambda: OBSERVED))
            provider.assert_not_called()
            self.assertIsNotNone(enrich_one(db, provider, clock=lambda: OBSERVED))
            provider.assert_called_once()
            self.assertEqual(self.account_work(db, held)["stage"], "qualified")
            self.assertEqual(self.account_work(db, invalid)["stage"], "qualified")
            self.assertEqual(db.execute("SELECT count(*) FROM people").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 0)
            states = db.execute("SELECT state FROM credit_jobs ORDER BY id").fetchall()
            self.assertEqual([row[0] for row in states], ["blocked", "failed"])


@unittest.skipUnless(TEST_URL, "Set CRM_TEST_POSTGRES_URL for disposable PostgreSQL.")
class PostgresAccountStageTest(PostgresDatabase, AccountStageTest):
    pass
