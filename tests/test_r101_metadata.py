"""R1-01 rule metadata, starter rules, and migration acceptance checks."""

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from crm_store import connect
from crm_store import initialize as initialize_crm
from workflow_store import initialize as initialize_workflow
from workflow_store import suppression_rules


STARTERS = {
    "cvent.com", "cventevents.com", "cvent-production.com", "*.cventevents.com",
}


class SuppressionMetadataTest(unittest.TestCase):
    def setUp(self):
        self.database = self.database_target()
        initialize_crm(self.database)

    def database_target(self):
        directory = Path(__file__).resolve().parents[1] / "instance" / f"suppression-metadata-{uuid4().hex}"
        directory.mkdir(parents=True)
        self.addCleanup(self.clean_up, directory)
        return directory / "test.sqlite3"

    def clean_up(self, directory):
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()

    def check_invalid_reason(self, connection):
        with self.assertRaises(sqlite3.IntegrityError), connection:
            connection.execute("""
                INSERT INTO suppression(kind, value, reason, created_at)
                VALUES ('domain', 'invalid.example', 'free text', '2026-11-26')
            """)

    def test_r101_1_fresh_schema_enforces_the_reason_codes_and_keeps_rule_metadata(self):
        with closing(connect(self.database)) as connection:
            initialize_workflow(connection)
            self.check_invalid_reason(connection)
            with connection:
                for code in ("competitor", "customer", "open_deal", "opt_out", "bounced", "manual"):
                    connection.execute("""
                        INSERT INTO suppression(kind, value, reason, source, created_at)
                        VALUES ('email', ?, ?, 'csv', '2026-11-26')
                    """, (f"{code}@example.test", code))
            rules = connection.execute("""
                SELECT id, kind, value, reason, source, created_by, created_at FROM suppression
                WHERE kind = 'email' ORDER BY id
            """).fetchall()
            self.assertEqual([tuple(rule)[1:] for rule in rules], [
                ("email", f"{code}@example.test", code, "csv", None, "2026-11-26")
                for code in ("competitor", "customer", "open_deal", "opt_out", "bounced", "manual")
            ])

    def test_r101_1_migrates_legacy_values_without_losing_identity_or_audit(self):
        with closing(connect(self.database)) as connection:
            initialize_workflow(connection)
            with connection:
                connection.execute("DROP TABLE suppression")
                connection.execute("""
                    CREATE TABLE suppression (
                        id INTEGER PRIMARY KEY,
                        kind TEXT NOT NULL CHECK (kind IN ('domain', 'email', 'account')),
                        value TEXT NOT NULL COLLATE NOCASE,
                        reason TEXT NOT NULL,
                        created_by INTEGER REFERENCES users(id),
                        created_at TEXT NOT NULL,
                        removed_at TEXT,
                        UNIQUE (kind, value)
                    )
                """)
                connection.execute("""
                    INSERT INTO users(id, name, email, role, timezone, password_hash)
                    VALUES (8, 'Legacy admin', 'admin@example.test', 'admin', 'Asia/Kolkata', 'offline')
                """)
                connection.executemany("""
                    INSERT INTO suppression(id, kind, value, reason, created_by, created_at, removed_at)
                    VALUES (?, ?, ?, ?, ?, '2026-11-20', ?)
                """, [
                    (12, "domain", "cvent.com", "Competitor", None, "2026-11-23"),
                    (39, "email", "case.worker@example.test", "Open deal", 8, None),
                    (40, "domain", "legacy.example", "Prior agreement with the team", 8, None),
                ])
                details = json.dumps({"rule_id": 12, "reason": "Competitor"})
                connection.execute("""
                    INSERT INTO audit_events(actor_id, action, details, created_at)
                    VALUES (8, 'suppression_remove', ?, '2026-11-23')
                """, (details,))
            for _ in range(2):
                initialize_workflow(connection)
                self.assertEqual([tuple(row) for row in connection.execute("""
                    SELECT id, kind, value, reason, reason_note, source, created_by, created_at, removed_at
                    FROM suppression ORDER BY id
                """)], [
                    (12, "domain", "cvent.com", "competitor", "Competitor", "seed", None, "2026-11-20", "2026-11-23"),
                    (39, "email", "case.worker@example.test", "open_deal", "Open deal", "legacy", 8, "2026-11-20", None),
                    (40, "domain", "legacy.example", "manual", "Prior agreement with the team", "legacy", 8, "2026-11-20", None),
                ])
                self.assertEqual([row["id"] for row in suppression_rules(connection)], [39, 40])
                self.assertEqual(tuple(connection.execute("SELECT actor_id, details FROM audit_events").fetchone()),
                                 (8, details))
            self.check_invalid_reason(connection)

    def test_r101_2_keeps_four_canonical_starters_and_respects_existing_removal(self):
        with closing(connect(self.database)) as connection:
            initialize_workflow(connection)
            seeds = list(connection.execute("SELECT id, kind, value, reason, source FROM suppression ORDER BY id"))
            self.assertEqual({row["value"] for row in seeds}, STARTERS)
            self.assertEqual(len(seeds), len(STARTERS))
            self.assertEqual({(row["kind"], row["reason"], row["source"]) for row in seeds},
                             {("domain", "competitor", "seed")})
            with connection:
                connection.execute("""
                    UPDATE suppression SET removed_at = '2026-11-26' WHERE value = 'cventevents.com'
                """)
            initialize_workflow(connection)
            self.assertEqual([tuple(row) for row in connection.execute(
                "SELECT id, kind, value, reason, source FROM suppression ORDER BY id"
            )], [tuple(row) for row in seeds])
            self.assertEqual({row["value"] for row in suppression_rules(connection)}, STARTERS - {"cventevents.com"})
