"""R1-08.4. Rehearse audited Cvent credit reconciliation without provider calls."""

import csv
import io
import json
import sqlite3
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import test_postgres_pilot
from app import create_app
from crm_store import connect
from legacy_contact_store import APOLLO_LEADS_SCHEMA
from workflow_store import DEMO_PASSWORD, timestamp, transaction


EXPORT_FIELDS = ("reference", "action", "credits", "at", "legacy_user_id", "cvent_company_id", "apollo_person_id")
CHARGE = ("charge-1", "email_unlock", "1.125", "2026-11-08T22:30:00-04:00", 11, 501, "sample-person")
SECOND = ("charge-2", "org_match", "0.5", "2026-11-10T04:59:00Z", 12, 999, "")


class CreditHistoryTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1] / "instance" / f"credit-history-{uuid4().hex}"
        self.folder.mkdir(parents=True)
        self.addCleanup(self.clean_files)
        self.database = self.database_target()
        create_app(self.database, demo=True)
        self.export = self.folder / "charges.csv"
        self.statement = self.folder / "statement.json"
        self.user_map = self.folder / "users.json"
        self.user_map.write_text(json.dumps({"11": "jordan@example.test"}), encoding="utf-8")
        self.write_statement("2.375")
        with closing(connect(self.database)) as db, transaction(db):
            self.jordan = db.execute("SELECT id FROM users WHERE email = 'jordan@example.test'").fetchone()[0]
            self.admin = db.execute("SELECT id FROM users WHERE email = 'admin@example.test'").fetchone()[0]
            self.account = self.add_account(db, "survivor")
            self.old_account = self.add_account(db, "retired")
            db.execute("INSERT INTO account_merges VALUES (?, ?, ?)", (self.old_account, self.account, timestamp()))
            db.execute("""
                INSERT INTO imported_source_evidence(source_slug, external_id, account_id, source_url, observed_at, payload_json)
                VALUES ('cvent', 'company:501', ?, '', ?, '{}')
            """, (self.old_account, timestamp()))
            self.person = db.execute("INSERT INTO people(account_id, name) VALUES (?, 'Fixture Person')",
                                     (self.account,)).lastrowid
            db.execute(APOLLO_LEADS_SCHEMA)
            db.execute("""
                INSERT INTO cvent_apollo_leads(apollo_id, person_id, legacy_company_id, company_domain, imported_at)
                VALUES ('sample-person', ?, 501, 'survivor.example', ?)
            """, (self.person, timestamp()))

    def database_target(self):
        return self.folder / "credits.sqlite3"

    def clean_files(self):
        for path in self.folder.iterdir():
            path.unlink()
        self.folder.rmdir()

    @staticmethod
    def add_account(db, key):
        return db.execute("""
            INSERT INTO accounts(identity_key, name, domain, root_domain) VALUES (?, ?, ?, ?)
        """, (key, key, f"{key}.example", f"{key}.example")).lastrowid

    def write_export(self, *rows):
        with self.export.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(EXPORT_FIELDS)
            writer.writerows(rows)

    def write_statement(self, total, reference="cvent-verified-2026-11"):
        self.statement.write_text(json.dumps({
            "reference": reference, "as_of": "2026-11-30T00:00:00Z", "total_credits": total,
        }), encoding="utf-8")

    def command(self, *options, status=0, email="admin@example.test", password=DEMO_PASSWORD):
        from import_cvent_credits import main

        arguments = ["--db", str(self.database), "--source", str(self.export),
                     "--statement", str(self.statement), "--user-map", str(self.user_map)]
        if "--apply" in options:
            arguments.extend(("--admin-email", email))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch("import_cvent_credits.getpass", return_value=password):
            if status:
                with self.assertRaises(SystemExit) as failure:
                    main([*arguments, *options])
                self.assertEqual(failure.exception.code, status)
            else:
                self.assertEqual(main([*arguments, *options]), 0)
        return err.getvalue() if status else json.loads(out.getvalue())

    def snapshot(self):
        with closing(connect(self.database, read_only=True)) as db:
            return {
                table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY {key}")]
                for table, key in (("credit_ledger", "id"), ("audit_events", "id"), ("accounts", "id"),
                                   ("people", "id"), ("account_work", "account_id"))
            }

    def apply(self, preview, **options):
        return self.command("--apply", "--expect", preview["fingerprint"], **options)

    def test_preview_apply_and_repeat_preserve_mapped_history_and_missing_attribution(self):
        self.write_export(SECOND, CHARGE, CHARGE)
        before = self.snapshot()
        preview = self.command()
        self.assertEqual(preview, self.command("--dry-run"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(preview["new_charges"], 2)
        self.assertEqual(preview["confirmed_credits"], "1.625")
        self.assertEqual(preview["opening_adjustment"], "0.75")
        self.assertEqual(self.apply(preview)["written"], 3)
        after = self.snapshot()
        with closing(connect(self.database, read_only=True)) as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM credit_ledger ORDER BY id")]
            self.assertEqual([row["source"] for row in rows], ["historical", "historical", "adjustment"])
            self.assertEqual([float(row["credits"]) for row in rows], [1.125, 0.5, 0.75])
            self.assertEqual(rows[0]["reference"], "charge-1")
            self.assertEqual(rows[0]["at"], "2026-11-09T02:30:00.000000+00:00")
            self.assertEqual(rows[0]["on_day"], "2026-11-08")
            self.assertEqual([rows[0][key] for key in ("user_id", "account_id", "person_id")],
                             [self.jordan, self.account, self.person])
            self.assertEqual([rows[1][key] for key in ("user_id", "account_id", "person_id")], [None] * 3)
            self.assertEqual(rows[2]["action"], "opening_balance_adjustment")
            audit = db.execute("SELECT actor_id, details FROM audit_events WHERE action = 'cvent_credit_history_import'").fetchall()
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["actor_id"], self.admin)
            self.assertEqual(json.loads(audit[0]["details"])["statement_reference"], "cvent-verified-2026-11")
        self.assertEqual(self.apply(preview)["written"], 0)
        self.assertEqual(self.command()["already_imported"], 2)
        self.assertEqual(self.snapshot(), after)
        self.assertEqual({key: after[key] for key in ("accounts", "people", "account_work")},
                         {key: before[key] for key in ("accounts", "people", "account_work")})

    def test_negative_adjustment_and_conflicting_replay(self):
        self.write_export(CHARGE)
        self.write_statement("1")
        preview = self.command()
        self.assertEqual(preview["opening_adjustment"], "-0.125")
        self.assertEqual(self.apply(preview)["written"], 2)
        after = self.snapshot()
        self.write_statement("1", reference="different-approval")
        self.assertIn("already recorded", self.command(status=2))
        self.write_statement("1")
        self.write_export((*CHARGE[:2], "1.126", *CHARGE[3:]))
        self.assertIn("already recorded", self.command(status=2))
        self.assertEqual(self.snapshot(), after)

    def test_conflicting_duplicate_unverified_amount_and_timestamp_are_rejected(self):
        before = self.snapshot()
        self.write_export(CHARGE, (*CHARGE[:2], "1.126", *CHARGE[3:]))
        self.assertIn("conflicting", self.command(status=2))
        for field, invalid in ((2, "0"), (2, "NaN"), (2, "0.0000001"), (3, "2026-11-09 02:30:00"),
                               (3, "2026-12-01T02:30:00Z"), (1, "opening_balance_adjustment")):
            with self.subTest(value=invalid):
                damaged = list(CHARGE)
                damaged[field] = invalid
                self.write_export(damaged)
                self.command(status=2)
        self.assertEqual(self.snapshot(), before)

    def test_stale_mapping_conflict_and_admin_authentication(self):
        self.write_export(CHARGE)
        preview = self.command()
        before = self.snapshot()
        self.assertIn("Admin", self.apply(preview, status=2, email="manager@example.test"))
        self.assertIn("Admin", self.apply(preview, status=2, password="wrong fixture password"))
        self.assertEqual(self.snapshot(), before)
        self.user_map.write_text(json.dumps({"11": "riya@example.test"}), encoding="utf-8")
        self.assertIn("preview", self.apply(preview, status=2))
        self.assertEqual(self.snapshot(), before)
        with closing(connect(self.database)) as db, transaction(db):
            other = self.add_account(db, "other")
            db.execute("UPDATE imported_source_evidence SET account_id = ? WHERE external_id = 'company:501'", (other,))
        self.assertIn("conflicting", self.command(status=2))

    def test_late_failure_rolls_back_postings_and_audit(self):
        from credit_history_store import SqlCreditHistoryStore

        self.write_export(CHARGE, SECOND)
        preview = self.command()
        before = self.snapshot()
        with patch.object(SqlCreditHistoryStore, "record_summary", side_effect=sqlite3.OperationalError):
            self.assertIn("could not be processed", self.apply(preview, status=2))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.apply(preview)["written"], 3)

    def test_zero_adjustment_still_audits_and_cannot_be_reopened(self):
        self.write_export(CHARGE)
        self.write_statement("1.125")
        preview = self.command()
        self.assertEqual(preview["opening_adjustment"], "0")
        self.assertEqual(self.apply(preview)["written"], 1)
        self.assertEqual(self.apply(preview)["written"], 0)
        self.write_statement("1.126")
        self.assertIn("already recorded", self.command(status=2))

    def test_current_and_offline_postings_do_not_change_the_opening_statement(self):
        self.write_export(CHARGE)
        preview = self.command()
        with closing(connect(self.database)) as db, transaction(db):
            for source, credits in (("apollo", 2), ("offline", 3)):
                job_id = db.execute("""
                    INSERT INTO credit_jobs(account_id, person_id, actor_id, action, state,
                                            on_day, budget_day, request_key, created_at)
                    VALUES (?, ?, ?, 'email_unlock', 'done', '2026-11-08', '2026-11-08', ?, ?)
                """, (self.account, self.person, self.jordan, f"demo-{source}", timestamp())).lastrowid
                db.execute("""
                    INSERT INTO credit_ledger(job_id, user_id, account_id, person_id,
                                              action, credits, on_day, at, source, reference)
                    VALUES (?, ?, ?, ?, 'email_unlock', ?, '2026-11-08', ?, ?, 'charge-1')
                """, (job_id, self.jordan, self.account, self.person, credits, timestamp(), source))
        self.assertEqual(self.command(), preview)
        self.assertEqual(self.apply(preview)["written"], 2)
        with closing(connect(self.database, read_only=True)) as db:
            postings = [(row["source"], float(row["credits"])) for row in db.execute("""
                SELECT source, credits FROM credit_ledger ORDER BY id
            """)]
        self.assertEqual(postings, [("apollo", 2), ("offline", 3), ("historical", 1.125), ("adjustment", 1.25)])


@unittest.skipUnless(test_postgres_pilot.TEST_URL, "Set CRM_TEST_POSTGRES_URL for disposable PostgreSQL.")
class PostgresCreditHistoryTest(test_postgres_pilot.PostgresDatabase, CreditHistoryTest):
    pass
