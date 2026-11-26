"""R1-03.2 account name changes preserve same-root source identity."""

import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from account_store import SqlAccountNames
from app import create_app
from crm_core.ingest import evaluate_intake, evaluate_saved
from crm_store import connect
from ingest_store import SourceStore
from test_postgres_pilot import TEST_URL, PostgresDatabase
from test_source_core import POLICY, event, listing
from workflow_store import timestamp, transaction, utcnow


OLD_NAME = "Lakeside Trade Association"
NEW_NAME = "Lakeside Industry Council"
ROOT = "lakeside.example"


class AccountNameIdentityTest(unittest.TestCase):
    def setUp(self):
        self.database = self.database_target()
        create_app(self.database, demo=True)

    def database_target(self):
        folder = Path(__file__).resolve().parents[1] / "instance" / f"name-identities-{uuid4().hex}"
        folder.mkdir(parents=True)
        self.addCleanup(self.clean_up, folder)
        return folder / "identity.sqlite3"

    @staticmethod
    def clean_up(folder):
        for path in folder.iterdir():
            path.unlink()
        folder.rmdir()

    @staticmethod
    def rename(connection, account_id, old, new):
        admin = connection.execute("SELECT id FROM users WHERE email = 'admin@example.test'").fetchone()[0]
        SqlAccountNames(connection).save_name(
            account_id, admin, (old, "unreviewed", None), (new, "apollo", "apollo:verified-name"),
            f"https://{ROOT}", "Applied the reviewed account name backfill.", utcnow(),
        )

    def test_backfill_preserves_replay_and_new_same_root_source_links(self):
        with closing(connect(self.database)) as connection, transaction(connection):
            store = SourceStore(connection)
            original = event(f"events.{ROOT}", OLD_NAME, "original")
            self.assertEqual(evaluate_saved(store, "cvent", original, {}, promote=True)[0], "imported")
            account_id = connection.execute("SELECT id FROM accounts WHERE root_domain = ?", (ROOT,)).fetchone()[0]
            self.rename(connection, account_id, OLD_NAME, NEW_NAME)

            self.assertEqual(evaluate_saved(store, "cvent", original, {}, promote=True)[0], "unchanged")
            self.assertEqual(evaluate_saved(
                store, "cvent", event(f"events.{ROOT}", OLD_NAME, "new-event"), {}, promote=True,
            )[0], "imported")
            self.assertEqual(evaluate_saved(
                store, "directory", listing(f"national.{ROOT}", OLD_NAME, "new-listing"), POLICY, promote=True,
            )[0], "imported")
            self.assertEqual(evaluate_intake(
                store, "upload", {"name": OLD_NAME, "domain": ROOT, "source_id": "new-upload"},
            )[0], "imported")
            unrelated_name = "Lakeside Trade Association Chapter"
            self.assertEqual(evaluate_saved(
                store, "directory", listing(f"chapter.{ROOT}", unrelated_name, "chapter"), POLICY, promote=True,
            )[0], "review")
            self.assertEqual(evaluate_intake(
                store, "upload", {"name": unrelated_name, "domain": ROOT, "source_id": "unrelated"},
            )[0], "review")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT name FROM accounts").fetchone()[0], NEW_NAME)
            self.assertEqual({row[0] for row in connection.execute("SELECT account_id FROM source_records")}, {account_id})
            self.assertEqual({row[0] for row in connection.execute("SELECT account_id FROM imported_source_evidence")}, {account_id})

    def test_retired_name_alias_stays_in_its_root_and_requires_one_canonical_account(self):
        with closing(connect(self.database)) as connection, transaction(connection):
            store = SourceStore(connection)
            winner = store.create_account(NEW_NAME, ROOT, "Association")
            retired = connection.execute("""
                INSERT INTO accounts(identity_key, name, domain, organization_type)
                VALUES ('retired-identity', ?, ?, 'Association')
            """, (OLD_NAME, f"retired.{ROOT}")).lastrowid
            self.rename(connection, retired, OLD_NAME, "Former Display Name")
            payload = {"name": OLD_NAME, "domain": ROOT, "source_id": "previous-name"}
            self.assertEqual(evaluate_intake(store, "upload", payload)[0], "review")
            connection.execute("INSERT INTO account_merges VALUES (?, ?, ?)", (retired, winner, timestamp()))
            self.assertEqual(evaluate_intake(store, "upload", payload)[0], "imported")
            self.assertEqual(connection.execute(
                "SELECT account_id FROM imported_source_evidence WHERE external_id = 'previous-name'",
            ).fetchone()[0], winner)
            other = store.create_account("Unrelated Council", "other.example", "Association")
            self.assertNotEqual(other, winner)
            self.assertEqual(evaluate_intake(
                store, "upload", dict(payload, domain="other.example", source_id="other-root"),
            )[0], "review")


@unittest.skipUnless(TEST_URL, "Set CRM_TEST_POSTGRES_URL for disposable PostgreSQL.")
class PostgresAccountNameIdentityTest(PostgresDatabase, AccountNameIdentityTest):
    pass
