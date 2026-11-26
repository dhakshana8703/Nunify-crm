"""R1-01.5 stored account restrictions across rule and account changes."""

import io
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app import create_app
from crm_core.transfer import transfer_accounts
from crm_store import connect, import_snapshot, initialize
from ingest_store import SourceStore
from transfer_store import SqlSnapshotTransfer
from workflow_store import DEMO_PASSWORD, transaction


class SuppressionProjectionTest(unittest.TestCase):
    observed = datetime(2026, 11, 26, 10, tzinfo=timezone.utc)

    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / "instance" / f"r101-projection-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(self.clean_up)
        self.database = self.database_target()

    def clean_up(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def database_target(self):
        return self.directory / "test.sqlite3"

    def sign_in(self, actor):
        client = self.app.test_client()
        self.assertEqual(client.get("/login").status_code, 200)
        self.assertEqual(self.post(client, "/login", email=f"{actor}@example.test",
                                   password=DEMO_PASSWORD).status_code, 303)
        self.assertEqual(client.get("/my-accounts" if actor == "riya" else "/assignment").status_code, 200)
        return client

    def post(self, client, path, **data):
        with client.session_transaction() as session:
            token = session["csrf"]
        return client.post(path, data={"csrf_token": token, **data})

    def start(self):
        self.app = create_app(self.database, demo=True)
        self.app.config.update(TESTING=True, CRM_CLOCK=lambda: self.observed)

    def add_rule(self, admin, kind, value, reason):
        self.assertEqual(self.post(admin, "/suppression", kind=kind, value=value, reason=reason).status_code, 303)
        return self.rule_id(kind, value)

    def rule_id(self, kind, value):
        with closing(connect(self.database)) as connection:
            row = connection.execute("SELECT id FROM suppression WHERE kind = ? AND value = ? AND removed_at IS NULL",
                                     (kind, str(value))).fetchone()
        self.assertIsNotNone(row)
        return row["id"]

    def assert_projection(self, expected):
        with closing(connect(self.database)) as connection:
            self.assertEqual(
                {row["identity_key"]: (row["suppressed"], row["suppress_reason"])
                 for row in connection.execute("SELECT identity_key, suppressed, suppress_reason FROM accounts")},
                expected,
            )
            self.assertEqual(
                {row["identity_key"] for row in connection.execute(
                    "SELECT identity_key FROM accounts WHERE suppressed = TRUE",
                )},
                {key for key, (suppressed, _) in expected.items() if suppressed},
            )

    def test_backfill_flags_match_seed_domains_without_holding_lookalikes(self):
        initialize(self.database)
        with closing(connect(self.database)) as connection, connection:
            connection.executemany("""
                INSERT INTO accounts(identity_key, name, domain, organization_type) VALUES (?, ?, ?, 'Association')
            """, [
                ("platform", "Cvent Platform", "cventevents.com"),
                ("division", "Cvent Division", "TWO.ONE.CVENTEVENTS.COM"),
                ("organizer", "Eligible Organizer", "notcventevents.com"),
            ])
        for _ in range(2):
            self.start()
            self.assert_projection({
                "platform": (True, "competitor"), "division": (True, "competitor"), "organizer": (False, None),
            })
        manager = self.sign_in("manager")
        original = manager.get("/assignment").data
        self.assertIn(b"Eligible Organizer", original)
        self.assertNotIn(b"Cvent Platform", original)
        inspected = manager.get("/assignment?show_suppressed=1").data
        self.assertIn(b"Cvent Platform", inspected)
        self.assertIn(b"Competitor", inspected)

    def test_account_projection_switches_to_next_rule_and_does_not_hold_account_for_email(self):
        self.start()
        with closing(connect(self.database)) as connection, transaction(connection):
            account_id = SourceStore(connection).create_account("Eligible Organizer", "organizer.example", "Association")
            connection.execute("""
                INSERT INTO account_work(account_id, owner_id, stage)
                VALUES (?, (SELECT id FROM users WHERE email = 'riya@example.test'), 'assigned')
            """, (account_id,))
            connection.execute("""
                INSERT INTO people(account_id, name, email, do_not_contact, origin)
                VALUES (?, 'Opted Out Contact', 'held@organizer.example', 1, 'apollo_offline')
            """, (account_id,))
            identity = connection.execute("SELECT identity_key FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]
        admin = self.sign_in("admin")
        rep = self.sign_in("riya")
        self.add_rule(admin, "email", "held@organizer.example", "opt_out")
        self.assert_projection({identity: (False, None)})
        self.assertIn(b"Eligible Organizer", rep.get("/my-accounts").data)

        upload = io.BytesIO(b"kind,value,reason\ndomain,organizer.example,customer\n")
        self.assertEqual(self.post(admin, "/suppression", rules=(upload, "rules.csv")).status_code, 303)
        winner = self.rule_id("domain", "organizer.example")
        alternate = self.add_rule(admin, "account", account_id, "open_deal")
        self.assert_projection({identity: (True, "customer")})
        self.assertNotIn(b"Eligible Organizer", rep.get("/my-accounts").data)
        self.assertIn(b"Customer", rep.get("/my-accounts?show_suppressed=1").data)

        self.assertEqual(self.post(admin, f"/suppression/{winner}/remove").status_code, 303)
        self.assert_projection({identity: (True, "open_deal")})
        self.assertEqual(self.add_rule(admin, "domain", "organizer.example", "customer"), winner)
        self.assert_projection({identity: (True, "customer")})
        for rule_id in (winner, alternate):
            self.assertEqual(self.post(admin, f"/suppression/{rule_id}/remove").status_code, 303)
        self.assert_projection({identity: (False, None)})
        self.assertIn(b"Eligible Organizer", rep.get("/my-accounts").data)
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT stage FROM account_work").fetchone()[0], "assigned")

    def test_new_accounts_use_current_destination_rules_for_seed_import_and_transfer(self):
        self.start()
        admin = self.sign_in("admin")
        self.add_rule(admin, "domain", "destination.example", "customer")
        self.add_rule(admin, "domain", "seeded.example", "customer")
        import_snapshot(self.database, {
            "seed": {"name": "Seeded", "domain": "seeded.example", "organization_type": "Association"},
        }, [])
        with closing(connect(self.database)) as connection, transaction(connection):
            SourceStore(connection).create_account("Ingested", "ingested.example", "Association")
            identity = connection.execute("SELECT identity_key FROM accounts WHERE domain = 'ingested.example'").fetchone()[0]
            counts = {}
            transfer_accounts(SqlSnapshotTransfer(connection), [
                {"id": 101, "identity_key": "held", "name": "Destination Match", "domain": "destination.example",
                 "organization_type": "Association", "name_source": "unreviewed", "name_source_ref": None,
                 "stage": "discovered", "suppressed": False, "suppress_reason": None},
                {"id": 102, "identity_key": "clear", "name": "Source Match", "domain": "source.example",
                 "organization_type": "Association", "name_source": "unreviewed", "name_source_ref": None,
                 "stage": "qualified", "suppressed": True, "suppress_reason": "manual"},
            ], {}, counts, apply=True)
            self.assertEqual(counts["accounts_added"], 2)
        self.assert_projection({
            "seed": (True, "customer"), "held": (True, "customer"), "clear": (False, None), identity: (False, None),
        })
