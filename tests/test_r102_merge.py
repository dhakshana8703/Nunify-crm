"""R1-02.3-.5 effects on disposable pre-change databases."""

import io
import json
import shutil
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app import create_app
from crm_store import SCHEMA, connect
from workflow_store import DEMO_PASSWORD, save_suppression_rule, suppression_reason, transaction


class AccountMergeTest(unittest.TestCase):
    observed = datetime(2026, 11, 26, 9, tzinfo=timezone.utc)

    def setUp(self):
        base = (Path(__file__).resolve().parents[1] / "instance").resolve()
        self.directory = (base / f"r102-merge-{uuid4().hex}").resolve()
        assert self.directory.parent == base and self.directory.name.startswith("r102-merge-")
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.database = self.database_target()
        with closing(connect(self.database)) as db:
            db.executescript(SCHEMA)
            with transaction(db):
                values = (("alpfa", "ALPFA", "alpfa.org"), ("alpfa-national", "ALPFA", "national.alpfa.org"),
                          ("cvent", "Cvent", "medc.cventevents.com"))
                self.winner, self.loser, self.cvent = [db.execute("""
                    INSERT INTO accounts(identity_key, name, domain, organization_type)
                    VALUES (?, ?, ?, 'Association')
                """, row).lastrowid for row in values]
        create_app(self.database, demo=True)
        with closing(connect(self.database)) as db, transaction(db):
            self.admin = db.execute("SELECT id FROM users WHERE email = 'admin@example.test'").fetchone()[0]
            self.rep = db.execute("SELECT id FROM users WHERE email = 'riya@example.test'").fetchone()[0]
            db.execute("INSERT INTO account_work(account_id, owner_id, stage) VALUES (?, ?, 'assigned')",
                       (self.loser, self.rep))
            self.person = db.execute("""
                INSERT INTO people(account_id, name, email, origin, unlocked, email_verified)
                VALUES (?, 'Example Contact', 'contact@national.alpfa.org', 'apollo', 1, 1)
            """, (self.loser,)).lastrowid
            db.execute("""
                INSERT INTO source_records(source, source_id, source_url, source_file, account_id, details_json, raw_json)
                VALUES ('cvent', 'event-alpfa', 'https://events.example/alpfa', 'saved.json', ?, '{}', '{"raw": 1}')
            """, (self.loser,))
            self.task = db.execute("""
                INSERT INTO tasks(account_id, person_id, owner_id, title, due_on)
                VALUES (?, ?, ?, 'Preserved follow-up', '2026-11-27')
            """, (self.loser, self.person, self.rep)).lastrowid
            self.job = db.execute("""
                INSERT INTO credit_jobs(account_id, person_id, actor_id, action, state, on_day, budget_day, request_key, created_at)
                VALUES (?, ?, ?, 'email_unlock', 'done', '2026-11-26', '2026-11-26', 'historic-lookup', '2026-11-26')
            """, (self.loser, self.person, self.rep)).lastrowid
            db.execute("""
                INSERT INTO credit_ledger(job_id, user_id, account_id, person_id, action, credits, on_day, at)
                VALUES (?, ?, ?, ?, 'email_unlock', 2, '2026-11-26', '2026-11-26')
            """, (self.job, self.rep, self.loser, self.person))
            self.ledger = db.execute("SELECT id FROM credit_ledger WHERE job_id = ?", (self.job,)).fetchone()[0]
            save_suppression_rule(db, "domain", "national.alpfa.org", "customer", self.admin, self.observed)

    def database_target(self):
        return self.directory / "legacy.sqlite3"

    def test_preview_then_merge_preserves_references_and_suppression_and_can_repeat(self):
        from account_merge_store import SqlAccountMergeStore
        from crm_core.account_merge import apply_merge, plan_merge, plan_root_merges

        with closing(connect(self.database, read_only=True)) as db:
            store = SqlAccountMergeStore(db)
            plan, = plan_root_merges(store, observed=self.observed)
            self.assertEqual(plan["survivor"]["id"], self.winner)
            self.assertEqual([row["id"] for row in plan["duplicates"]], [self.loser])
            self.assertEqual(plan["counts"]["credit_ledger"], 1)
            self.assertEqual(plan["counts"]["source_records"], 1)
            self.assertEqual(plan["conflicts"], [])
            self.assertEqual(suppression_reason(db, self.winner), "Customer")
            self.assertEqual(db.execute("SELECT account_id FROM people WHERE id = ?", (self.person,)).fetchone()[0], self.loser)
            self.assertFalse(db.execute("SELECT 1 FROM account_merges").fetchone())

        with closing(connect(self.database)) as db:
            with transaction(db):
                store = SqlAccountMergeStore(db)
                manual = plan_merge(store, [self.loser, self.winner], observed=self.observed)
                self.assertEqual(manual["survivor"]["id"], self.winner)
                self.assertEqual(apply_merge(store, plan, actor_id=self.admin, observed=self.observed), self.winner)
            for table, identifier in (("people", self.person), ("tasks", self.task), ("credit_jobs", self.job),
                                      ("credit_ledger", self.ledger)):
                self.assertEqual(db.execute(f"SELECT account_id FROM {table} WHERE id = ?", (identifier,)).fetchone()[0], self.winner)
            person = db.execute("SELECT unlocked, email_verified FROM people WHERE id = ?", (self.person,)).fetchone()
            self.assertEqual(tuple(person), (1, 1))
            evidence = db.execute("SELECT account_id, raw_json FROM source_records WHERE source_id = 'event-alpfa'").fetchone()
            self.assertEqual(tuple(evidence), (self.winner, '{"raw": 1}'))
            credit = db.execute("SELECT credits, job_id FROM credit_ledger WHERE id = ?", (self.ledger,)).fetchone()
            self.assertEqual(tuple(credit), (2, self.job))
            self.assertEqual(db.execute("SELECT owner_id FROM account_work WHERE account_id = ?", (self.winner,)).fetchone()[0], self.rep)
            history = db.execute("SELECT from_id, to_id FROM account_merges").fetchone()
            self.assertEqual(tuple(history), (self.loser, self.winner))
            account = db.execute("SELECT root_domain, suppressed, suppress_reason FROM accounts WHERE id = ?", (self.winner,)).fetchone()
            self.assertEqual(tuple(account), ("alpfa.org", True, "customer"))
            self.assertEqual(suppression_reason(db, self.winner), "Customer")
            self.assertEqual(suppression_reason(db, self.cvent), "Competitor")
            from crm_core.ingest import evaluate_intake
            from ingest_store import SourceStore

            for host in ("alpfa.org", "national.alpfa.org", "events.alpfa.org"):
                with self.subTest(host=host), transaction(db):
                    self.assertEqual(evaluate_intake(SourceStore(db), "upload", {
                        "name": "ALPFA", "domain": host, "source_id": f"replay-{host}",
                        "contact_name": "New Contact", "contact_email": "contact@different.example",
                    })[0], "suppressed")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM people").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM imported_source_evidence").fetchone()[0], 0)
            self.assertEqual(plan_root_merges(store, observed=self.observed), [])
            with self.assertRaises(ValueError), transaction(db):
                apply_merge(store, plan, actor_id=self.admin, observed=self.observed)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM account_merges").fetchone()[0], 1)

    def test_ownership_conflict_and_late_storage_error_leave_everything_intact(self):
        from account_merge_store import SqlAccountMergeStore
        from crm_core.account_merge import apply_merge, plan_merge

        with closing(connect(self.database)) as db:
            store = SqlAccountMergeStore(db)
            with transaction(db):
                jordan = db.execute("SELECT id FROM users WHERE email = 'jordan@example.test'").fetchone()[0]
                db.execute("INSERT INTO account_work(account_id, owner_id) VALUES (?, ?)", (self.winner, jordan))
            plan = plan_merge(store, [self.winner, self.loser], observed=self.observed)
            self.assertTrue(any("owner" in conflict.lower() for conflict in plan["conflicts"]))
            with self.assertRaises(ValueError), transaction(db):
                apply_merge(store, plan, actor_id=self.admin, observed=self.observed)
            with transaction(db):
                db.execute("UPDATE account_work SET owner_id = NULL WHERE account_id = ?", (self.winner,))
            plan = plan_merge(store, [self.winner, self.loser], observed=self.observed)
            self.assertFalse(plan["conflicts"])
            with patch("account_merge_store.refresh_account_roots", side_effect=RuntimeError("late migration failure")):
                with self.assertRaisesRegex(RuntimeError, "late migration failure"), transaction(db):
                    apply_merge(store, plan, actor_id=self.admin, observed=self.observed)
            self.assertEqual(db.execute("SELECT account_id FROM people WHERE id = ?", (self.person,)).fetchone()[0], self.loser)
            self.assertEqual(db.execute("SELECT account_id FROM credit_ledger WHERE id = ?", (self.ledger,)).fetchone()[0], self.loser)
            self.assertFalse(db.execute("SELECT 1 FROM account_merges").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM audit_events WHERE action = 'account_merge'").fetchone())

    def test_preview_refuses_history_changed_since_review(self):
        from account_merge_store import SqlAccountMergeStore
        from crm_core.account_merge import apply_merge, plan_merge

        with closing(connect(self.database)) as db:
            store = SqlAccountMergeStore(db)
            plan = plan_merge(store, [self.loser, self.winner], observed=self.observed)
            with transaction(db):
                db.execute("UPDATE tasks SET title = 'Changed after preview' WHERE id = ?", (self.task,))
            with self.assertRaisesRegex(ValueError, "changed"), transaction(db):
                apply_merge(store, plan, actor_id=self.admin, observed=self.observed)
            task = db.execute("SELECT account_id, title FROM tasks WHERE id = ?", (self.task,)).fetchone()
            self.assertEqual(tuple(task), (self.loser, "Changed after preview"))

    def test_unfinished_provider_history_on_the_retired_account_blocks_apply(self):
        from account_merge_store import SqlAccountMergeStore
        from crm_core.account_merge import apply_merge, plan_merge

        with closing(connect(self.database)) as db:
            with transaction(db):
                ae = db.execute("SELECT id FROM users WHERE role = 'ae'").fetchone()[0]
                db.execute("UPDATE credit_jobs SET state = 'failed', attempts = 1 WHERE id = ?", (self.job,))
                db.execute("""
                    INSERT INTO handoffs(account_id, person_id, ae_id, requested_by, outcome, context, next_step,
                                         payload, idempotency_key, created_at)
                    VALUES (?, ?, ?, ?, 'demo_requested', 'Existing account context', 'Arrange a demo', '{}',
                            'pending-on-legacy-id', '2026-11-26')
                """, (self.loser, self.person, ae, self.rep))
            store = SqlAccountMergeStore(db)
            plan = plan_merge(store, [self.winner, self.loser], observed=self.observed)
            self.assertTrue(any("reconciliation" in conflict.lower() for conflict in plan["conflicts"]))
            self.assertTrue(any("handoff" in conflict.lower() for conflict in plan["conflicts"]))
            with self.assertRaises(ValueError), transaction(db):
                apply_merge(store, plan, actor_id=self.admin, observed=self.observed)
            self.assertFalse(db.execute("SELECT 1 FROM account_merges").fetchone())
            self.assertEqual(db.execute("SELECT account_id FROM handoffs").fetchone()[0], self.loser)

    def test_retry_keeps_attempted_request_on_retired_account_blocked(self):
        from account_merge_store import SqlAccountMergeStore
        from crm_core.account_merge import apply_merge, plan_merge
        from crm_core.growth import remove_suppression_rule, retry_enrichment
        from growth_store import SqlGrowthStore

        with closing(connect(self.database)) as db:
            store = SqlAccountMergeStore(db)
            growth = SqlGrowthStore(db)
            actor = {"id": self.admin}
            with transaction(db):
                rule_id = db.execute("SELECT id FROM suppression WHERE value = 'national.alpfa.org'").fetchone()[0]
                remove_suppression_rule(growth, actor, rule_id, self.observed)
                db.execute("UPDATE credit_jobs SET state = 'failed', attempts = 1 WHERE id = ?", (self.job,))
            failed = plan_merge(store, [self.winner, self.loser], observed=self.observed)
            self.assertTrue(any("reconciliation" in conflict for conflict in failed["conflicts"]))

            with transaction(db):
                retry_enrichment(growth, actor, self.job, self.observed)
            retried = plan_merge(store, [self.winner, self.loser], observed=self.observed)
            with self.assertRaisesRegex(ValueError, "Resolve the merge conflicts"), transaction(db):
                apply_merge(store, retried, actor_id=self.admin, observed=self.observed)
            self.assertTrue(any("reconciliation" in conflict for conflict in retried["conflicts"]))
            request = db.execute("""
                SELECT account_id, state, attempts, request_key FROM credit_jobs WHERE id = ?
            """, (self.job,)).fetchone()
            self.assertEqual(tuple(request), (self.loser, "queued", 1, "historic-lookup"))
            ledger = db.execute("SELECT account_id, job_id, credits FROM credit_ledger WHERE id = ?",
                                (self.ledger,)).fetchone()
            self.assertEqual(tuple(ledger), (self.loser, self.job, 2))
            self.assertFalse(db.execute("SELECT 1 FROM account_merges").fetchone())

    def test_never_attempted_queued_request_can_follow_the_survivor(self):
        from account_merge_store import SqlAccountMergeStore
        from crm_core.account_merge import apply_merge, plan_merge

        with closing(connect(self.database)) as db, transaction(db):
            db.execute("""
                INSERT INTO credit_jobs(account_id, actor_id, action, state, on_day, budget_day, request_key, created_at)
                VALUES (?, ?, 'people_search', 'queued', '2026-11-26', '2026-11-26', 'unused-lookup', '2026-11-26')
            """, (self.loser, self.rep))
            store = SqlAccountMergeStore(db)
            plan = plan_merge(store, [self.winner, self.loser], observed=self.observed)
            self.assertFalse(plan["conflicts"])
            self.assertEqual(apply_merge(store, plan, actor_id=self.admin, observed=self.observed), self.winner)
            request = db.execute("""
                SELECT account_id, state, attempts FROM credit_jobs WHERE request_key = 'unused-lookup'
            """).fetchone()
            self.assertEqual(tuple(request), (self.winner, "queued", 0))

    def test_manager_cannot_apply_the_bulk_migration(self):
        from merge_accounts import _apply_database, _preview_database

        preview = _preview_database(self.database)
        with self.assertRaisesRegex(PermissionError, "active Admin"):
            _apply_database(self.database, expected=preview["fingerprint"], email="manager@example.test",
                            password=DEMO_PASSWORD)
        self.assertEqual(_preview_database(self.database), preview)

    def test_command_preview_and_repeat_apply(self):
        from merge_accounts import main

        def invoke(*flags):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", str(self.database), *flags]), 0)
            return json.loads(output.getvalue())

        preview = invoke("--dry-run")
        self.assertEqual(preview["groups"][0]["survivor"]["id"], self.winner)
        self.assertEqual(preview["groups"][0]["counts"]["people"], 1)
        self.assertEqual(preview["groups"][0]["affected_records"]["people"], [self.person])
        self.assertEqual(invoke("--dry-run"), preview)
        with closing(connect(self.database, read_only=True)) as db:
            self.assertFalse(db.execute("SELECT 1 FROM account_merges").fetchone())
        options = ("--apply", "--expect", preview["fingerprint"], "--admin-email", "admin@example.test")
        with patch("merge_accounts.getpass", return_value=DEMO_PASSWORD):
            self.assertEqual(invoke(*options)["merged_groups"], 1)
            self.assertEqual(invoke(*options)["merged_groups"], 0)
        self.assertEqual(invoke("--dry-run")["groups"], [])


if __name__ == "__main__":
    unittest.main()
