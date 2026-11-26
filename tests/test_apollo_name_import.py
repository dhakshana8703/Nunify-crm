"""R1-03 offline Apollo organization evidence import."""

import io
import json
import sqlite3
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from account_store import SqlAccountNames
from app import create_app
from crm_core.account_names import account_naming
from crm_store import connect, import_snapshot
from workflow_store import DEMO_PASSWORD


class ApolloNameImportTest(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / "instance" / f"apollo-org-test-{uuid4().hex}"
        directory.mkdir(parents=True)
        self.addCleanup(self._remove_files, directory)
        self.database = self.database_target(directory)
        self.cache = directory / "saved-cache.sqlite3"
        import_snapshot(self.database, {
            "institute": {"name": "Theiia", "domain": "theiia.org", "organization_type": "Association"},
            "synnex": {"name": "Tdsynnex", "domain": "tdsynnex.com", "organization_type": "Company"},
        }, [])
        create_app(self.database, demo=True)
        self.crm = connect(self.database)
        self.addCleanup(self.crm.close)
        with closing(sqlite3.connect(self.cache)) as cache, cache:
            cache.execute("""
                CREATE TABLE apollo_org_cache (
                    domain TEXT PRIMARY KEY, org_id TEXT, name TEXT, raw_json TEXT NOT NULL, cached_at TEXT
                )
            """)

    def database_target(self, directory):
        return directory / "crm.sqlite3"

    @staticmethod
    def _remove_files(directory):
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()

    def cache_row(self, domain, org_id, name, *, primary=None, website=None, **extra):
        raw = {"id": org_id, "name": name, "primary_domain": primary, "website_url": website, **extra}
        with closing(sqlite3.connect(self.cache)) as cache, cache:
            cache.execute("INSERT INTO apollo_org_cache VALUES (?, ?, ?, ?, '2026-11-25 12:00:00')",
                          (domain, org_id, name, json.dumps(raw)))

    def run_import(self, *, apply=False):
        from apollo_name_import import import_saved_apollo_names

        return import_saved_apollo_names(self.crm, self.cache, apply=apply)

    def evidence(self):
        return [tuple(row) for row in self.crm.execute("""
            SELECT external_id, account_id, source_url, observed_at, payload_json FROM imported_source_evidence
            WHERE source_slug = 'apollo-organization' ORDER BY external_id
        """)]

    def test_r103_import_uses_verified_saved_apollo_organization_and_replays(self):
        self.cache_row("national.theiia.org", "institute-1", "The Institute of Internal Auditors",
                       primary="theiia.org", website="https://www.theiia.org/about", private_person="ignored")
        self.cache_row("tdsynnex.com", "synnex-1", "TD SYNNEX", website="https://www.tdsynnex.com")
        before = self.cache.read_bytes()
        self.assertEqual(self.run_import(), {"imported": 2, "unchanged": 0, "review": 0})
        self.assertFalse(self.evidence())
        self.assertIsNone(self.crm.execute("SELECT 1 FROM ingest_sources WHERE slug='apollo-organization'").fetchone())
        self.assertEqual(self.run_import(apply=True), {"imported": 2, "unchanged": 0, "review": 0})
        saved = self.evidence()
        self.assertEqual(self.run_import(apply=True), {"imported": 0, "unchanged": 2, "review": 0})
        self.assertEqual(self.evidence(), saved)
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertNotIn("private_person", saved[0][4])
        self.assertEqual(self.crm.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 2)
        self.assertEqual(self.crm.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 0)
        self.assertEqual(tuple(self.crm.execute("""
            SELECT adapter, key_hash FROM ingest_sources WHERE slug='apollo-organization'
        """).fetchone()), ("upload", None))

        store = SqlAccountNames(self.crm)
        account = next(row for row in store.accounts() if row["domain"] == "theiia.org")
        record = account_naming(account, store.evidence(account["id"])[account["id"]])
        self.assertEqual((record["preferred"]["source"], record["preferred"]["name"]),
                         ("apollo", "The Institute of Internal Auditors"))
        self.assertEqual(account["name"], "Theiia")

    def test_r103_holds_mismatched_or_ambiguous_apollo_identity(self):
        self.cache_row("theiia.org", "institute-1", "Wrong Organization", primary="theiia.org",
                       website="https://cvent.com")
        self.cache_row("www.theiia.org", "institute-2", "Incorrect Institute", primary="other.org",
                       website="https://theiia.org")
        self.cache_row("unknown.org", "unknown-1", "Other Organization", primary="unknown.org")
        self.cache_row("sub.tdsynnex.com", "synnex-1", "TD SYNNEX", primary="tdsynnex.com")
        self.cache_row("www.tdsynnex.com", "", "TD SYNNEX", primary="tdsynnex.com")
        self.cache_row("east.theiia.org", "institute-5", "Unverified Institute")
        with self.crm:
            self.crm.execute("""
                INSERT INTO accounts(identity_key, name, domain, organization_type)
                VALUES ('duplicate', 'Legacy Synnex duplicate', 'legacy.tdsynnex.com', 'Company')
            """)
        self.assertEqual(self.run_import(apply=True), {"imported": 0, "unchanged": 0, "review": 6})
        self.assertFalse(self.evidence())

    def test_r103_distinct_apollo_names_for_one_root_remain_visible_to_review(self):
        self.cache_row("theiia.org", "institute-1", "The Institute of Internal Auditors", primary="theiia.org")
        self.cache_row("national.theiia.org", "institute-2", "Different Institute Name", primary="theiia.org")
        self.assertEqual(self.run_import(apply=True), {"imported": 2, "unchanged": 0, "review": 0})
        store = SqlAccountNames(self.crm)
        account = next(row for row in store.accounts() if row["domain"] == "theiia.org")
        record = account_naming(account, store.evidence(account["id"])[account["id"]])
        self.assertEqual(record["state"], "conflicting")
        self.assertIsNone(record["preferred"])
        self.assertEqual({row[0] for row in self.evidence()}, {"institute-1", "institute-2"})

    def test_r103_saved_provider_id_cannot_move_to_another_account(self):
        self.cache_row("theiia.org", "organization-1", "The Institute of Internal Auditors", primary="theiia.org")
        self.assertEqual(self.run_import(apply=True)["imported"], 1)
        saved = self.evidence()
        self.cache_row("tdsynnex.com", "organization-1", "TD SYNNEX", primary="tdsynnex.com")
        self.assertEqual(self.run_import(apply=True), {"imported": 0, "unchanged": 0, "review": 2})
        self.assertEqual(self.evidence(), saved)

    def test_r103_verified_apollo_name_accepts_a_long_mixed_case_brand(self):
        name = "SingleOfficialBrandName"
        self.cache_row("theiia.org", "test-brand", name, primary="theiia.org")
        self.assertEqual(self.run_import(apply=True), {"imported": 1, "unchanged": 0, "review": 0})
        store = SqlAccountNames(self.crm)
        account = next(row for row in store.accounts() if row["domain"] == "theiia.org")
        record = account_naming(account, store.evidence(account["id"])[account["id"]])
        self.assertEqual((record["preferred"]["source"], record["preferred"]["name"]), ("apollo", name))

    def test_r103_command_previews_and_requires_an_active_admin_to_write(self):
        from apollo_name_import import main

        self.cache_row("theiia.org", "institute-1", "The Institute of Internal Auditors", primary="theiia.org")
        arguments = ("--db", str(self.database), "--cache", str(self.cache))
        out = io.StringIO()
        with redirect_stdout(out), patch("apollo_name_import.getpass") as shortcut:
            self.assertEqual(main(arguments), 0)
            shortcut.assert_not_called()
        self.assertEqual(json.loads(out.getvalue()), {"mode": "dry_run", "imported": 1, "review": 0, "unchanged": 0})
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as denied, patch(
            "apollo_name_import.getpass", return_value=DEMO_PASSWORD,
        ):
            main((*arguments, "--apply", "--admin-email", "manager@example.test"))
        self.assertEqual(denied.exception.code, 2)
        self.assertFalse(self.evidence())
        with redirect_stdout(io.StringIO()), patch("apollo_name_import.getpass", return_value=DEMO_PASSWORD):
            self.assertEqual(main((*arguments, "--apply", "--admin-email", "admin@example.test")), 0)
        self.assertEqual(len(self.evidence()), 1)

    def test_r103_blocks_reserved_slug_collision_and_rolls_back_failed_write(self):
        self.cache_row("theiia.org", "institute-1", "The Institute of Internal Auditors", primary="theiia.org")
        self.cache_row("tdsynnex.com", "synnex-1", "TD SYNNEX", primary="tdsynnex.com")
        with self.crm:
            self.crm.execute("INSERT INTO ingest_sources (slug, name, adapter, key_hash) VALUES (?, ?, ?, ?)",
                             ("apollo-organization", "External test", "external", "test-digest"))
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.run_import(apply=True)
        self.assertFalse(self.evidence())
        with self.crm:
            self.crm.execute("DELETE FROM ingest_sources WHERE slug = 'apollo-organization'")

        from apollo_name_import import _save_evidence

        def interrupted(connection, evidence):
            if evidence["external_id"] == "institute-1":
                raise RuntimeError("Synthetic write failure")
            return _save_evidence(connection, evidence)

        with patch("apollo_name_import._save_evidence", side_effect=interrupted), self.assertRaisesRegex(
            RuntimeError, "Synthetic write failure",
        ):
            self.run_import(apply=True)
        self.assertFalse(self.evidence())
        self.assertIsNone(self.crm.execute("SELECT 1 FROM ingest_sources WHERE slug='apollo-organization'").fetchone())
