"""Exercise the reviewed R1-03 backfill on isolated CRM databases."""

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
from workflow_store import DEMO_PASSWORD, timestamp, transaction


class AccountNameBackfillTest(unittest.TestCase):
    def setUp(self):
        self.database = self.database_target()
        create_app(self.database, demo=True)
        with closing(connect(self.database)) as connection, transaction(connection):
            self.theiia = self.add_account(connection, "iia", "Theiia", "theiia.org")
            self.td = self.add_account(connection, "td", "Tdsynnex", "tdsynnex.com")
            self.manual = self.add_account(connection, "manual", "Protected Name", "manual.example", "manual")
            self.conflict = self.add_account(connection, "conflict", "Existing Council", "council.example")
            self.missing = self.add_account(connection, "missing", "Needs Review", "")
            self.retired = self.add_account(connection, "retired", "Retired Record", "retired.example")
            connection.execute("INSERT INTO account_merges VALUES (?, ?, ?)", (self.retired, self.theiia, timestamp()))

            connection.execute("""
                INSERT INTO imported_source_evidence
                    (source_slug, external_id, account_id, source_url, observed_at, payload_json)
                VALUES ('upload', 'iia-website', ?, 'https://theiia.org/about', ?, ?)
            """, (self.theiia, timestamp(), json.dumps({
                "og:site_name": "The Institute of Internal Auditors", "website_title": "Internal Audit Resources",
            })))
            for account, source_id, organizer in (
                (self.td, "td-event", "TD SYNNEX"),
                (self.manual, "manual-event", "Source Name"),
                (self.conflict, "conflict-one", "First Council"),
                (self.conflict, "conflict-two", "Second Council"),
            ):
                event_url = f"https://web.cvent.com/event/{source_id}"
                details = json.dumps({"organizer_company": organizer, "event_name": "Annual Meeting"})
                connection.execute("""
                    INSERT INTO source_records (source, source_id, source_file, source_url, account_id, details_json, raw_json)
                    VALUES ('cvent', ?, 'saved-events.json', ?, ?, ?, '{}')
                """, (source_id, event_url, account, details))

            self.admin = connection.execute("SELECT id FROM users WHERE email = 'admin@example.test'").fetchone()[0]
            owner = connection.execute("SELECT id FROM users WHERE email = 'riya@example.test'").fetchone()[0]
            connection.execute("""
                INSERT INTO account_work(account_id, owner_id, stage, revision) VALUES (?, ?, 'working', 7)
            """, (self.td, owner))
            person = connection.execute("""
                INSERT INTO people(account_id, name, unlocked) VALUES (?, 'Saved Person', 1)
            """, (self.td,)).lastrowid
            connection.execute("""
                INSERT INTO tasks(account_id, person_id, owner_id, title, due_on)
                VALUES (?, ?, ?, 'Prepare follow-up', '2026-11-27')
            """, (self.td, person, owner))

    def database_target(self):
        folder = Path(__file__).resolve().parents[1] / "instance" / f"name-backfill-{uuid4().hex}"
        folder.mkdir(parents=True)
        self.addCleanup(self.clean_up, folder)
        return folder / "backfill.sqlite3"

    def clean_up(self, folder):
        for path in folder.iterdir():
            path.unlink()
        folder.rmdir()

    @staticmethod
    def add_account(connection, identity, name, domain, source="unreviewed"):
        return connection.execute("""
            INSERT INTO accounts(identity_key, name, domain, root_domain, organization_type, name_source)
            VALUES (?, ?, ?, ?, 'Unknown', ?)
        """, (identity, name, domain, domain or None, source)).lastrowid

    def snapshot(self):
        with closing(connect(self.database, read_only=True)) as connection:
            snapshot = {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {key}")]
                for table, key in (
                    ("accounts", "id"), ("account_work", "account_id"), ("people", "id"), ("tasks", "id"),
                    ("source_records", "id"), ("imported_source_evidence", "id"), ("account_merges", "from_id"),
                    ("audit_events", "id"), ("suppression", "id"), ("credit_ledger", "id"),
                )
            }
            snapshot["account_identities"] = [tuple(row) for row in connection.execute("""
                SELECT id, identity_key, domain, root_domain, organization_type FROM accounts ORDER BY id
            """)]
            return snapshot

    def command(self, *options, status=0, password=DEMO_PASSWORD):
        from backfill_account_names import main

        stdout, stderr = io.StringIO(), io.StringIO()
        arguments = ["--db", str(self.database), *options]
        with redirect_stdout(stdout), redirect_stderr(stderr), patch("backfill_account_names.getpass", return_value=password):
            if status:
                with self.assertRaises(SystemExit) as exit_error:
                    main(arguments)
                self.assertEqual(exit_error.exception.code, status)
            else:
                self.assertEqual(main(arguments), 0)
        return stderr.getvalue() if status else json.loads(stdout.getvalue())

    def apply(self, plan, *, email="admin@example.test", status=0, password=DEMO_PASSWORD):
        return self.command("--apply", "--expect", plan["fingerprint"], "--admin-email", email,
                            status=status, password=password)

    def test_preview_and_apply_correct_names_then_replay_without_writes(self):
        before = self.snapshot()
        preview = self.command()
        self.assertEqual(preview, self.command("--dry-run"))
        self.assertEqual(before, self.snapshot())
        self.assertEqual({row["id"]: row["action"] for row in preview["accounts"]}, {
            self.theiia: "change", self.td: "change", self.manual: "manual",
            self.conflict: "conflicting", self.missing: "missing",
        })

        applied = self.apply(preview)
        self.assertEqual(applied["counts"]["change"], 2)
        after = self.snapshot()
        for table in set(before) - {"accounts", "audit_events"}:
            self.assertEqual(before[table], after[table], table)
        with closing(connect(self.database, read_only=True)) as connection:
            names = {
                row["id"]: tuple(row)[1:] for row in connection.execute("""
                    SELECT id, name, name_source, name_source_ref FROM accounts ORDER BY id
                """)
            }
            self.assertEqual(names, {
                self.theiia: ("The Institute of Internal Auditors", "website", "upload:iia-website"),
                self.td: ("TD SYNNEX", "organizer", "cvent:td-event"),
                self.manual: ("Protected Name", "manual", None),
                self.conflict: ("Existing Council", "unreviewed", None),
                self.missing: ("Needs Review", "unreviewed", None),
                self.retired: ("Retired Record", "unreviewed", None),
            })
            changes = connection.execute("""
                SELECT actor_id, account_id, details FROM audit_events
                WHERE action = 'account_name_changed' ORDER BY account_id
            """).fetchall()
            self.assertEqual([tuple(row)[:2] for row in changes], [(self.admin, self.theiia), (self.admin, self.td)])
            for row in changes:
                audit = json.loads(row["details"])
                self.assertIn("backfill", audit["reason"])
                self.assertEqual(audit["after"]["reference"], names[row["account_id"]][2])

        replay = self.command()
        self.assertEqual(replay["counts"].get("change", 0), 0)
        self.apply(replay)
        self.assertEqual(self.snapshot(), after)

    def test_invalid_admin_and_stale_preview_make_no_writes(self):
        preview = self.command()
        before = self.snapshot()
        self.assertIn("Admin", self.apply(preview, email="manager@example.test", status=2))
        self.assertIn("Admin", self.apply(preview, password="invalid fixture password", status=2))
        self.assertIn("--expect", self.command("--apply", "--admin-email", "admin@example.test", status=2))
        self.assertEqual(self.snapshot(), before)
        with closing(connect(self.database)) as connection, transaction(connection):
            connection.execute("UPDATE users SET active = 0 WHERE id = ?", (self.admin,))
        self.assertIn("Admin", self.apply(preview, status=2))
        self.assertEqual(self.snapshot(), before)
        with closing(connect(self.database)) as connection, transaction(connection):
            connection.execute("UPDATE users SET active = 1 WHERE id = ?", (self.admin,))
            connection.execute("UPDATE accounts SET name = 'Verified Manual Edit', name_source = 'manual' WHERE id = ?",
                               (self.theiia,))
        changed = self.snapshot()
        self.assertIn("dry run", self.apply(preview, status=2))
        self.assertEqual(self.snapshot(), changed)

    def test_second_name_write_failure_rolls_back_first_write_and_audit(self):
        from account_store import SqlAccountNames

        preview = self.command()
        before = self.snapshot()
        save_name = SqlAccountNames.save_name
        written = []

        def failing_save(store, *args, **kwargs):
            if written:
                raise sqlite3.OperationalError("fixture second write failed")
            save_name(store, *args, **kwargs)
            written.append(True)

        with patch.object(SqlAccountNames, "save_name", failing_save):
            self.assertIn("could not be processed", self.apply(preview, status=2))
        self.assertEqual(len(written), 1)
        self.assertEqual(self.snapshot(), before)


@unittest.skipUnless(test_postgres_pilot.TEST_URL, "Set CRM_TEST_POSTGRES_URL for disposable PostgreSQL.")
class PostgresAccountNameBackfillTest(test_postgres_pilot.PostgresDatabase, AccountNameBackfillTest):
    pass
