"""Exercise approved Cvent ownership import through its operator command."""

import csv
import io
import json
import sqlite3
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from openpyxl import Workbook

import test_postgres_pilot
from app import create_app
from assignment_store import SqlAssignmentStore
from crm_store import connect
from workflow_store import DEMO_PASSWORD, save_suppression_rule, timestamp, transaction


EXPORT_COLUMNS = ("company_id", "user_id", "username", "full_name", "assigned_at")


class LegacyAllocationTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1] / "instance" / f"legacy-allocations-{uuid4().hex}"
        self.folder.mkdir(parents=True)
        self.addCleanup(self.clean_files)
        self.database = self.database_target()
        create_app(self.database, demo=True)
        self.user_map = self.folder / "approved.json"
        self.user_map.write_text(json.dumps({
            "11": "riya@example.test", "12": "jordan@example.test", "13": "manager@example.test",
            "14": "inactive@example.test",
        }), encoding="utf-8")
        with closing(connect(self.database)) as db, transaction(db):
            self.admin = db.execute("SELECT id FROM users WHERE email = 'admin@example.test'").fetchone()[0]
            self.riya = db.execute("SELECT id FROM users WHERE email = 'riya@example.test'").fetchone()[0]
            self.jordan = db.execute("SELECT id FROM users WHERE email = 'jordan@example.test'").fetchone()[0]
            db.execute("""
                INSERT INTO users(name, email, role, timezone, password_hash, active)
                VALUES ('Former SDR', 'inactive@example.test', 'sdr', 'Asia/Kolkata', '', 0)
            """)
            self.first = self.account(db, "first")
            old = self.account(db, "retired", company_id=501)
            db.execute("INSERT INTO account_merges VALUES (?, ?, ?)", (old, self.first, timestamp()))
            self.second = self.account(db, "second", company_id=502)
            self.conflict = self.account(db, "conflict", company_id=601)
            self.link(db, self.conflict, 602)
            self.owned = self.account(db, "owned", company_id=701)
            blocked = self.account(db, "blocked", company_id=801)
            self.account(db, "rejected-owner", company_id=901)
            self.account(db, "unmapped-owner", company_id=902)
            self.account(db, "inactive-owner", company_id=903)
            db.execute("INSERT INTO account_work(account_id, stage, revision) VALUES (?, 'qualified', 1)", (self.first,))
            db.execute("""
                INSERT INTO account_work(account_id, owner_id, stage, revision) VALUES (?, ?, 'working', 3)
            """, (self.owned, self.jordan))
            save_suppression_rule(db, "account", str(blocked), "manual", self.admin, None)

    def database_target(self):
        return self.folder / "allocations.sqlite3"

    def clean_files(self):
        for path in self.folder.iterdir():
            path.unlink()
        self.folder.rmdir()

    @staticmethod
    def link(db, account_id, company_id):
        db.execute("""
            INSERT INTO imported_source_evidence (source_slug, external_id, account_id, source_url, observed_at, payload_json)
            VALUES ('cvent', ?, ?, '', ?, '{}')
        """, (f"company:{company_id}", account_id, timestamp()))

    def account(self, db, name, *, company_id=None):
        account_id = db.execute("""
            INSERT INTO accounts(identity_key, name, domain, root_domain, organization_type)
            VALUES (?, ?, ?, ?, 'Unknown')
        """, (name, name, f"{name}.example", f"{name}.example")).lastrowid
        if company_id:
            self.link(db, account_id, company_id)
        return account_id

    def document(self, pairs, *, workbook=False):
        rows = [(company, user, f"legacy-{user}", "Legacy SDR", "2026-11-11 09:00:00") for company, user in pairs]
        if workbook:
            source = self.folder / "saved.xlsx"
            book = Workbook()
            book.active.append(EXPORT_COLUMNS)
            for row in rows:
                book.active.append(row)
            book.save(source)
            book.close()
        else:
            source = self.folder / "saved.csv"
            with source.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(EXPORT_COLUMNS)
                writer.writerows(rows)
        return source

    def snapshot(self):
        with closing(connect(self.database, read_only=True)) as db:
            return {
                table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY {key}")]
                for table, key in (("accounts", "id"), ("account_work", "account_id"), ("audit_events", "id"),
                                   ("imported_source_evidence", "id"), ("account_merges", "from_id"))
            }

    def command(self, source, *options, status=0, email="admin@example.test", use_map=True, password=DEMO_PASSWORD):
        from import_cvent_allocations import main

        arguments = ["--db", str(self.database), "--source", str(source)]
        if use_map:
            arguments.extend(("--user-map", str(self.user_map)))
        if "--apply" in options:
            arguments.extend(("--admin-email", email))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch("import_cvent_allocations.getpass", return_value=password):
            if status:
                with self.assertRaises(SystemExit) as failure:
                    main([*arguments, *options])
                self.assertEqual(failure.exception.code, status)
            else:
                self.assertEqual(main([*arguments, *options]), 0)
        return err.getvalue() if status else json.loads(out.getvalue())

    def test_preview_holds_conflicts_and_apply_reuses_survivor_without_moving_current_owners(self):
        source = self.document([(501, 11), (501, 11), (502, 11), (601, 11), (602, 12), (701, 11),
                                (801, 11), (901, 13), (902, 15), (903, 14), (999, 11)], workbook=True)
        before = self.snapshot()
        dry = self.command(source, use_map=False)
        self.assertEqual(dry["counts"].get("ready", 0), 0)
        preview = self.command(source)
        self.assertEqual(preview, self.command(source))
        self.assertEqual(before, self.snapshot())
        self.assertEqual({row["company_id"]: row["status"] for row in preview["rows"]}, {
            501: "ready", 502: "ready", 601: "conflict", 602: "conflict", 701: "already_owned",
            801: "suppressed", 901: "inactive_sdr", 902: "unmapped_user", 903: "inactive_sdr", 999: "unlinked_company",
        })
        self.assertEqual(len(preview["rows"]), 10)
        self.assertEqual(preview["rows"][0]["account_id"], self.first)
        applied = self.command(source, "--apply", "--expect", preview["fingerprint"])
        self.assertEqual(applied["applied"], 2)
        after = self.snapshot()
        for key in ("accounts", "imported_source_evidence", "account_merges"):
            self.assertEqual(after[key], before[key])
        with closing(connect(self.database, read_only=True)) as db:
            self.assertEqual([tuple(row) for row in db.execute("""
                SELECT account_id, owner_id, stage, revision FROM account_work ORDER BY account_id
            """)], [(self.first, self.riya, "qualified", 2), (self.second, self.riya, "assigned", 1),
                   (self.owned, self.jordan, "working", 3)])
            audit = db.execute("""
                SELECT actor_id, details FROM audit_events WHERE action = 'legacy_cvent_allocations'
            """).fetchall()
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["actor_id"], self.admin)
            recorded = json.loads(audit[0]["details"])["allocations"]
            self.assertEqual([row["company_id"] for row in recorded], [501, 502])
            self.assertEqual(recorded[0]["legacy_assigned_at"], "2026-11-11 09:00:00")
        replay = self.command(source)
        self.assertEqual(replay["counts"].get("already_current", 0), 2)
        self.assertEqual(self.command(source, "--apply", "--expect", replay["fingerprint"])["applied"], 0)
        self.assertEqual(self.snapshot(), after)

    def test_active_admin_and_current_preview_required(self):
        source = self.document([(501, 11)])
        preview = self.command(source)
        before = self.snapshot()
        apply = ("--apply", "--expect", preview["fingerprint"])
        self.assertIn("Admin", self.command(source, *apply, status=2, email="manager@example.test"))
        self.assertIn("Admin", self.command(source, *apply, status=2, password="wrong fixture password"))
        self.assertIn("--user-map", self.command(source, *apply, status=2, use_map=False))
        self.assertEqual(self.snapshot(), before)
        with closing(connect(self.database)) as db, transaction(db):
            db.execute("UPDATE account_work SET revision = 2 WHERE account_id = ?", (self.first,))
        changed = self.snapshot()
        self.assertIn("preview", self.command(source, *apply, status=2))
        self.assertEqual(self.snapshot(), changed)

    def test_failure_after_first_owner_rolls_back_owners_and_audit(self):
        source = self.document([(501, 11), (502, 12)])
        preview = self.command(source)
        before = self.snapshot()
        save = SqlAssignmentStore.save
        written = []

        def fail_second(store, change):
            if written:
                raise sqlite3.OperationalError("fixture second assignment failed")
            save(store, change)
            written.append(change.account_id)

        with patch.object(SqlAssignmentStore, "save", fail_second):
            self.assertIn("could not be processed", self.command(source, "--apply", "--expect", preview["fingerprint"],
                                                                  status=2))
        self.assertEqual(written, [self.first])
        self.assertEqual(self.snapshot(), before)


@unittest.skipUnless(test_postgres_pilot.TEST_URL, "Set CRM_TEST_POSTGRES_URL for disposable PostgreSQL.")
class PostgresLegacyAllocationTest(test_postgres_pilot.PostgresDatabase, LegacyAllocationTest):
    pass
