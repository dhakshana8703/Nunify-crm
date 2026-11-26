"""Exercise the one-time source transfer with a populated destination."""

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from werkzeug.security import generate_password_hash

from app import create_app
from crm_core.transfer import transfer_snapshot
from crm_store import connect, import_snapshot, table_exists
from cvent_apollo_import import SCHEMA as APOLLO_SCHEMA
from transfer_saved_sources import transfer_saved_sources
from transfer_store import SqlSnapshotTransfer, read_snapshot, transfer_transaction
from workflow_store import save_suppression_rule


OBSERVED = "2026-11-24T00:00:00+00:00"
COUNTERS = (
    "accounts_added", "source_records_added", "people_added",
    "saved_unlocks_added", "qualifications_added", "review_records_added",
)


def account(name, domain):
    return {"name": name, "domain": domain, "organization_type": "Association"}


def evidence(source, source_id, identity, details):
    return {
        "source": source, "source_id": source_id, "source_url": f"https://source.example/{source_id}",
        "source_file": "example.json", "identity_key": identity,
        "reason": None if identity else "Shared domain needs review",
        "details": details, "raw": details,
    }


def account_id(db, identity_key):
    return db.execute("SELECT id FROM accounts WHERE identity_key = ?", (identity_key,)).fetchone()[0]


class SavedSourceTransferTest(unittest.TestCase):
    def setUp(self):
        instance = Path(__file__).resolve().parents[1] / "instance"
        self.scratch = instance / f"saved-source-transfer-{uuid4().hex}"
        self.scratch.mkdir(parents=True)
        self.addCleanup(self.remove_test_files)
        self.source = self.scratch / "local.sqlite3"
        self.destination = self.scratch / "hosted.sqlite3"
        create_app(self.source)
        create_app(self.destination, demo=True)
        self.shared_event = evidence("cvent", "event-shared", "shared", {"event_name": "Shared meeting"})
        import_snapshot(self.destination, {
            "pilot-association": account("Pilot Association", "pilot.example"),
            "shared": account("Host-edited organization", "shared.example"),
        }, [self.shared_event])
        with closing(connect(self.destination)) as db, db:
            self.hosted_id = account_id(db, "shared")
            self.hosted_rep = db.execute("SELECT id FROM users WHERE email = 'riya@example.test'").fetchone()[0]
            self.save_activity_and_task(db, self.hosted_id, self.hosted_rep, "Hosted follow-up")
            db.execute("UPDATE accounts SET name_source='manual' WHERE id=?", (self.hosted_id,))
            db.execute("INSERT INTO qualifications(account_id,score,signals,observed_at,override) VALUES (?, 80, '[]', ?, 1)",
                       (self.hosted_id, OBSERVED))

    def remove_test_files(self):
        for file in self.scratch.iterdir():
            file.unlink()
        self.scratch.rmdir()

    def save_activity_and_task(self, db, organization, rep, task):
        db.execute("INSERT INTO account_work(account_id,owner_id,stage,revision,assigned_at) VALUES (?,?,'assigned',4,?)",
                   (organization, rep, OBSERVED))
        activity = db.execute("""
            INSERT INTO activities(account_id,actor_id,channel,outcome,direction,occurred_at)
            VALUES (?,?,'call','no_answer','outbound',?)
        """, (organization, rep, OBSERVED)).lastrowid
        db.execute("INSERT INTO tasks(account_id,owner_id,activity_id,title,due_on) VALUES (?,?,?,?,'2026-11-25')",
                   (organization, rep, activity, task))

    def fixture(self):
        import_snapshot(self.source, {
            "shared": account("Source name", "shared.example"),
            "new": account("New source organization", "new.example"),
            "local-only": account("Local practice organization", "local-only.example"),
        }, [
            self.shared_event,
            evidence("directory", "listing-shared", "shared", {"name": "Source name"}),
            evidence("cvent", "event-new", "new", {"event_name": "New meeting"}),
            evidence("directory", "needs-review", None, {"name": "Unverified chapter"}),
        ])
        with closing(connect(self.source)) as db, db:
            db.execute(APOLLO_SCHEMA)
            shared = account_id(db, "shared")
            new = account_id(db, "new")
            local_rep = db.execute("""
                INSERT INTO users(name,email,role,timezone,password_hash)
                VALUES ('Local rep','local-only@example.test','sdr','Asia/Kolkata',?)
            """, (generate_password_hash(uuid4().hex),)).lastrowid
            self.save_activity_and_task(db, shared, local_rep, "Local-only follow-up")
            db.execute("UPDATE accounts SET name_source='organizer',name_source_ref='event-new' WHERE id=?", (new,))
            db.execute("INSERT INTO account_work(account_id,stage,revision) VALUES (?,'qualified',3)", (new,))
            db.executemany("INSERT INTO qualifications(account_id,score,signals,observed_at) VALUES (?, ?, '[]', ?)",
                           [(shared, 30, OBSERVED), (new, 60, OBSERVED)])
            contact = db.execute("""
                INSERT INTO people(account_id,name,title,email,origin)
                VALUES (?,'Event Planner','Planner','planner@new.example','cvent')
            """, (new,)).lastrowid
            event = db.execute("SELECT id FROM source_records WHERE source_id = 'event-new'").fetchone()[0]
            db.execute("INSERT INTO cvent_contact_evidence(person_id,source_record_id,observed_at) VALUES (?,?,?)",
                       (contact, event, OBSERVED))
            lead = db.execute("""
                INSERT INTO people(account_id,name,email,origin,unlocked,email_verified)
                VALUES (?,'Saved Apollo Lead','lead@shared.example','apollo',1,1)
            """, (shared,)).lastrowid
            db.execute("""
                INSERT INTO cvent_apollo_leads(apollo_id,person_id,legacy_company_id,company_domain,unlocked_at,imported_at)
                VALUES ('example-apollo-lead',?,91,'shared.example',?,?)
            """, (lead, OBSERVED, OBSERVED))
            db.execute("""
                INSERT INTO people(account_id,name,email,origin)
                VALUES (?,'Practice contact','practice@shared.example','demo')
            """, (shared,))

    def snapshot(self, database=None):
        tables = ("accounts", "source_records", "qualifications", "account_work", "users", "people",
                  "cvent_contact_evidence", "cvent_apollo_leads", "activities", "tasks", "credit_ledger")
        with closing(connect(database or self.destination, read_only=True)) as db:
            return {
                table: sorted(tuple(row) for row in db.execute(f"SELECT * FROM {table}")) if table_exists(db, table) else None
                for table in tables
            }

    def test_preview_for_apply_stops_when_hosted_suppression_schema_is_missing(self):
        self.fixture()
        with closing(connect(self.destination)) as db, db:
            db.execute("ALTER TABLE accounts DROP COLUMN suppress_reason")
        before = self.snapshot()

        preview = transfer_saved_sources(self.source, self.destination, companies_only=True)
        self.assertEqual(preview["accounts_added"], 1)
        with self.assertRaisesRegex(ValueError, "Start the updated CRM"):
            transfer_saved_sources(self.source, self.destination, companies_only=True, require_apply_ready=True)
        with self.assertRaisesRegex(ValueError, "Start the updated CRM"):
            transfer_saved_sources(self.source, self.destination, companies_only=True, apply=True)
        self.assertEqual(self.snapshot(), before)

        create_app(self.destination)
        ready = transfer_saved_sources(self.source, self.destination, companies_only=True, require_apply_ready=True)
        self.assertEqual(ready["accounts_added"], 1)

        create_app(self.destination)
        ready = transfer_saved_sources(self.source, self.destination, companies_only=True, require_apply_ready=True)
        self.assertEqual(ready["accounts_added"], 1)

    def test_preview_apply_and_replay_preserve_hosted_work_and_source_links(self):
        self.fixture()
        before = self.snapshot()
        source_before = self.snapshot(self.source)
        preview = transfer_saved_sources(self.source, self.destination)
        self.assertEqual(self.snapshot(), before)
        for name, expected in (("accounts_added", 1), ("source_records_added", 3),
                               ("people_added", 2), ("saved_unlocks_added", 1),
                               ("qualifications_added", 1), ("review_records_added", 1)):
            self.assertEqual(preview[name], expected, name)
        applied = transfer_saved_sources(self.source, self.destination, apply=True)
        self.assertEqual({name: applied[name] for name in COUNTERS}, {name: preview[name] for name in COUNTERS})

        after = self.snapshot()
        for table in ("users", "activities", "tasks", "credit_ledger"):
            self.assertEqual(after[table], before[table], table)
        with closing(connect(self.destination, read_only=True)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM people").fetchone()[0], 2)
            self.assertEqual(account_id(db, "shared"), self.hosted_id)
            hosted = db.execute("""
                SELECT a.name,a.name_source,w.owner_id,w.stage,w.revision,q.score
                FROM accounts a JOIN account_work w ON w.account_id=a.id
                JOIN qualifications q ON q.account_id=a.id WHERE a.id=?
            """, (self.hosted_id,)).fetchone()
            self.assertEqual(tuple(hosted), ("Host-edited organization", "manual", self.hosted_rep, "assigned", 4, 80))
            new = account_id(db, "new")
            naming = db.execute("SELECT name_source,name_source_ref FROM accounts WHERE id=?", (new,)).fetchone()
            self.assertEqual(tuple(naming), ("organizer", "event-new"))
            work = db.execute("SELECT owner_id,stage FROM account_work WHERE account_id=?", (new,)).fetchone()
            self.assertEqual(tuple(work), (None, "qualified"))
            review = db.execute("SELECT account_id,review_reason FROM source_records WHERE source_id='needs-review'").fetchone()
            self.assertEqual(tuple(review), (None, "Shared domain needs review"))
            contact = db.execute("""
                SELECT p.account_id,s.account_id,s.source_id FROM cvent_contact_evidence e
                JOIN people p ON p.id=e.person_id JOIN source_records s ON s.id=e.source_record_id
            """).fetchone()
            self.assertEqual(tuple(contact), (new, new, "event-new"))
            lead = db.execute("""
                SELECT p.account_id,p.unlocked,p.email_verified,c.apollo_id,c.unlocked_at
                FROM cvent_apollo_leads c JOIN people p ON p.id=c.person_id
            """).fetchone()
            self.assertEqual(tuple(lead), (self.hosted_id, 1, 1, "example-apollo-lead", OBSERVED))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

        replay = transfer_saved_sources(self.source, self.destination, apply=True)
        self.assertEqual({name: replay[name] for name in COUNTERS}, dict.fromkeys(COUNTERS, 0))
        self.assertEqual(self.snapshot(), after)
        self.assertEqual(self.snapshot(self.source), source_before)

    def test_domain_conflict_rolls_back_without_duplicate_account(self):
        self.fixture()
        import_snapshot(self.destination, {"different": account("Ambiguous host match", "NEW.EXAMPLE")}, [])
        before = self.snapshot()
        with self.assertRaises(ValueError):
            transfer_saved_sources(self.source, self.destination, apply=True)
        self.assertEqual(self.snapshot(), before)

    def test_r102_transfer_holds_retired_domain_in_source_and_retired_id_in_destination(self):
        self.fixture()
        for database, identity, kind in ((self.source, "new", "domain"), (self.destination, "shared", "account")):
            with closing(connect(database)) as db, db:
                former_domain = f"retired-{identity}.example"
                former_id = db.execute("""
                    INSERT INTO accounts(identity_key,name,domain,organization_type)
                    VALUES ('old-identity','Former organization',?,'Association')
                """, (former_domain,)).lastrowid
                db.execute("INSERT INTO account_merges(from_id,to_id,merged_at) VALUES (?,?,?)",
                           (former_id, account_id(db, identity), OBSERVED))
                value = former_domain if kind == "domain" else str(former_id)
                save_suppression_rule(db, kind, value, "manual", None, None)

        for apply in (False, True):
            report = transfer_saved_sources(self.source, self.destination, apply=apply)
            self.assertEqual((report["contacts_held"], report["people_added"], report["saved_unlocks_added"]), (2, 0, 0))
            self.assertEqual(report["source_records_added"], 3)

    def test_source_id_conflict_rolls_back_without_moving_evidence(self):
        self.fixture()
        conflicting = evidence("cvent", "event-new", "pilot-association", {"event_name": "Other organization"})
        import_snapshot(self.destination, {}, [conflicting])
        before = self.snapshot()
        with self.assertRaises(ValueError):
            transfer_saved_sources(self.source, self.destination, apply=True)
        self.assertEqual(self.snapshot(), before)

    def test_write_failure_rolls_back_earlier_inserts(self):
        self.fixture()
        with closing(connect(self.destination)) as db:
            db.execute(APOLLO_SCHEMA)
            db.executescript("""
                CREATE TRIGGER fail_saved_apollo_write BEFORE INSERT ON cvent_apollo_leads
                BEGIN
                    SELECT RAISE(ABORT, 'Synthetic contact write failure');
                END;
            """)
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            transfer_saved_sources(self.source, self.destination, apply=True)
        self.assertEqual(self.snapshot(), before)

    def company_fixture(self):
        self.fixture()
        import_snapshot(self.source, {"roster-only": account("Roster Organization", "roster.example")}, [])
        rows = [
            (1, "company:1", "imported", "Linked existing company"),
            (2, "company:2", "imported", "Created roster-only company"),
            (3, "company:3", "review", "Needs identity evidence"),
            (4, "company:4", "suppressed", "Platform restriction"),
        ]
        with closing(connect(self.source)) as db, db:
            db.execute("""
                INSERT INTO ingest_sources(slug,name,adapter,key_hash)
                VALUES ('cvent-company-roster','Cvent Company Roster','external','local-fixture-key')
            """)
            run_id = db.execute("""
                INSERT INTO source_runs(source_slug,input_name,trigger,state,queued_at,finished_at,request_key,request_hash)
                VALUES ('cvent-company-roster','companies.csv','upload','partial',?,?, 'roster-key','roster-hash')
            """, (OBSERVED, OBSERVED)).lastrowid
            for row, external_id, result, detail in rows:
                payload = json.dumps({"source_id": external_id, "evidence_type": "Cvent company export"})
                db.execute("""
                    INSERT INTO source_run_rows(run_id,row_number,payload_json,result,detail)
                    VALUES (?,?,?,?,?)
                """, (run_id, row, payload, result, detail))
                if result == "imported":
                    identity = "shared" if row == 1 else "roster-only"
                    db.execute("""
                        INSERT INTO imported_source_evidence
                        (source_slug,external_id,account_id,source_url,observed_at,payload_json)
                        VALUES ('cvent', ?, ?, ?, ?, ?)
                    """, (external_id, account_id(db, identity), f"https://{identity}.example", OBSERVED, payload))

    def company_transfer(self, *, apply=False):
        snapshot = read_snapshot(self.source, companies_only=True)
        with closing(connect(self.destination, read_only=not apply)) as db:
            with transfer_transaction(db, apply=apply, postgres=False):
                store = SqlSnapshotTransfer(db)
                admin = store.destination_admin("admin@example.test")
                return transfer_snapshot(store, *snapshot, apply=apply, actor_id=admin["id"] if apply else None)

    def company_state(self):
        with closing(connect(self.destination, read_only=True)) as db:
            return {table: tuple(sorted(tuple(row) for row in db.execute(f"SELECT * FROM {table}")))
                    for table in ("accounts", "ingest_sources", "source_runs", "source_run_rows",
                                  "imported_source_evidence", "audit_events")}

    def test_company_transfer_preserves_roster_evidence_review_and_hosted_work(self):
        self.company_fixture()
        hosted_before = self.snapshot()
        with closing(connect(self.destination)) as db, db:
            db.execute("""
                INSERT INTO source_runs(source_slug,input_name,trigger,state,queued_at)
                VALUES ('cvent','existing.csv','upload','complete',?)
            """, (OBSERVED,))
        company_before = self.company_state()
        preview = self.company_transfer()
        self.assertEqual(self.snapshot(), hosted_before)
        self.assertEqual(self.company_state(), company_before)
        self.assertEqual((preview["accounts_added"], preview["company_evidence_added"], preview["roster_rows_added"],
                          preview["roster_runs_added"], preview["people_added"], preview["saved_unlocks_added"]),
                         (2, 2, 4, 1, 0, 0))
        self.assertEqual(self.company_transfer(apply=True), preview)
        with closing(connect(self.destination, read_only=True)) as db:
            self.assertEqual(tuple(db.execute("""
                SELECT name, name_source, owner_id, stage FROM accounts JOIN account_work ON accounts.id = account_work.account_id
                WHERE identity_key = 'shared'
            """).fetchone()), ("Host-edited organization", "manual", self.hosted_rep, "assigned"))
            links = {row["external_id"]: row["identity_key"] for row in db.execute("""
                SELECT external_id, identity_key FROM imported_source_evidence JOIN accounts
                ON imported_source_evidence.account_id = accounts.id
            """)}
            self.assertEqual(links, {"company:1": "shared", "company:2": "roster-only"})
            run = db.execute("SELECT id,state FROM source_runs WHERE source_slug='cvent-company-roster'").fetchone()
            self.assertEqual((run["id"], run["state"]), (2, "partial"))
            self.assertEqual([tuple(row) for row in db.execute("""
                SELECT result,detail FROM source_run_rows WHERE run_id=? ORDER BY row_number
            """, (run["id"],))], [
                ("imported", "Linked existing company"), ("imported", "Created roster-only company"),
                ("review", "Needs identity evidence"), ("suppressed", "Platform restriction"),
            ])
            self.assertIsNone(db.execute("""
                SELECT key_hash FROM ingest_sources WHERE slug='cvent-company-roster'
            """).fetchone()[0])
            self.assertEqual([row[0] for row in db.execute("""
                SELECT action FROM audit_events WHERE action='saved_sources_transferred'
            """)], ["saved_sources_transferred"])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        hosted_after = self.snapshot()
        for table in ("users", "people", "cvent_contact_evidence", "cvent_apollo_leads",
                      "activities", "tasks", "credit_ledger"):
            self.assertEqual(hosted_after[table], hosted_before[table], table)
        company_after = self.company_state()
        self.assertFalse(any(self.company_transfer(apply=True).values()))
        self.assertEqual(self.company_state(), company_after)

    def test_company_transfer_conflict_leaves_destination_intact(self):
        self.company_fixture()
        with closing(connect(self.destination)) as db, db:
            db.execute("""
                INSERT INTO imported_source_evidence(source_slug,external_id,account_id,source_url,observed_at,payload_json)
                VALUES ('cvent','company:1',?,'https://pilot.example',?,'{}')
            """, (account_id(db, "pilot-association"), OBSERVED))
        before = self.company_state()
        for apply in (False, True):
            with self.assertRaisesRegex(ValueError, "conflicts with destination company evidence"):
                self.company_transfer(apply=apply)
        self.assertEqual(self.company_state(), before)

    def test_company_transfer_late_run_failure_rolls_back_accounts_and_evidence(self):
        self.company_fixture()
        with closing(connect(self.destination)) as db:
            db.executescript("""
                CREATE TRIGGER fail_company_run_row BEFORE INSERT ON source_run_rows
                BEGIN
                    SELECT RAISE(ABORT, 'Synthetic source run failure');
                END;
            """)
        before = self.company_state()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "Synthetic source run failure"):
            self.company_transfer(apply=True)
        self.assertEqual(self.company_state(), before)

    def test_company_replay_preserves_changed_destination_review_decision(self):
        self.company_fixture()
        self.company_transfer(apply=True)
        with closing(connect(self.destination)) as db, db:
            db.execute("""
                UPDATE source_run_rows SET detail = 'Hosted reviewer investigated'
                WHERE result = 'review'
            """)
        before = self.company_state()
        with self.assertRaisesRegex(ValueError, "roster run conflicts with its destination copy"):
            self.company_transfer(apply=True)
        self.assertEqual(self.company_state(), before)
