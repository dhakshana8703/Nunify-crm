"""R1-02.2 acceptance coverage for additive database upgrades."""

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from crm_store import connect, table_exists
from crm_store import initialize as initialize_crm
from workflow_store import transaction


class RootDomainSchemaTest(unittest.TestCase):
    def setUp(self):
        self.database = self.database_target()

    def database_target(self):
        directory = Path(__file__).resolve().parents[1] / "instance" / f"root-domain-test-{uuid4().hex}"
        directory.mkdir(parents=True)
        self.addCleanup(self.clean_up, directory)
        return directory / "test.sqlite3"

    def clean_up(self, directory):
        for file in directory.iterdir():
            file.unlink()
        directory.rmdir()

    def old_schema(self, rows):
        with closing(connect(self.database)) as connection, connection:
            connection.execute("""
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY, identity_key TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, domain TEXT NOT NULL, organization_type TEXT NOT NULL,
                    owner TEXT
                )
            """)
            connection.executemany("""
                INSERT INTO accounts (identity_key, name, domain, organization_type)
                VALUES (?, ?, ?, 'Association')
            """, rows)

    def account_roots(self, connection):
        return [tuple(row) for row in connection.execute(
            "SELECT id, domain, root_domain FROM accounts ORDER BY id"
        )]

    def assert_root_uniqueness(self, connection, root):
        with self.assertRaises(sqlite3.IntegrityError), connection:
            connection.execute("""
                INSERT INTO accounts (identity_key, name, domain, organization_type, root_domain)
                VALUES ('duplicate', 'Duplicate', 'another.example', 'Association', ?)
            """, (root.upper(),))

    def root_index_exists(self, connection):
        if getattr(connection, "dialect", "sqlite") == "postgresql":
            query = "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = ?"
        else:
            query = "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?"
        return connection.execute(query, ("accounts_root_domain_unique",)).fetchone() is not None

    def test_r102_2_backfills_only_usable_roots_and_preserves_account_identity_on_restart(self):
        self.old_schema([
            ("lakeshore", "Lakeshore", "WWW.events.lakeshore.co.uk."),
            ("unknown", "To review", "co.uk"),
        ])
        for _ in range(2):
            initialize_crm(self.database)
            with closing(connect(self.database)) as connection:
                self.assertEqual(self.account_roots(connection), [
                    (1, "WWW.events.lakeshore.co.uk.", "lakeshore.co.uk"),
                    (2, "co.uk", None),
                ])
                self.assertTrue(table_exists(connection, "account_merges"))
                self.assert_root_uniqueness(connection, "lakeshore.co.uk")

    def test_r102_2_holds_colliding_legacy_rows_and_backfills_the_survivor_after_retirement(self):
        self.old_schema([
            ("alpfa", "ALPFA", "alpfa.org"),
            ("alpfa-national", "ALPFA", "national.alpfa.org"),
            ("other", "Other organization", "team.other.org.au"),
        ])
        initialize_crm(self.database)
        with closing(connect(self.database)) as connection:
            self.assertEqual(self.account_roots(connection), [
                (1, "alpfa.org", None),
                (2, "national.alpfa.org", None),
                (3, "team.other.org.au", "other.org.au"),
            ])
            self.assertFalse(self.root_index_exists(connection))
            with transaction(connection):
                connection.execute("""
                    INSERT INTO account_merges(from_id, to_id, merged_at) VALUES (2, 1, '2026-11-26')
                """)
                from account_identity_schema import refresh_account_roots

                refresh_account_roots(connection)
            self.assertEqual(self.account_roots(connection), [
                (1, "alpfa.org", "alpfa.org"),
                (2, "national.alpfa.org", None),
                (3, "team.other.org.au", "other.org.au"),
            ])
            self.assertTrue(self.root_index_exists(connection))
            self.assert_root_uniqueness(connection, "alpfa.org")

    def test_r102_2_tombstone_requires_existing_accounts_and_an_older_survivor(self):
        self.old_schema([("first", "First", "first.example"), ("second", "Second", "second.example")])
        initialize_crm(self.database)
        with closing(connect(self.database)) as connection:
            for losing, surviving in ((1, 1), (1, 2), (7, 2), (2, 0)):
                with self.subTest(losing=losing, surviving=surviving):
                    with self.assertRaises(sqlite3.IntegrityError), connection:
                        connection.execute("""
                            INSERT INTO account_merges(from_id, to_id, merged_at) VALUES (?, ?, '2026-11-26')
                        """, (losing, surviving))
