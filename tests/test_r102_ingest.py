"""R1-02.6 shared root identity for current source admission and replays."""

import io
import json
import os
import re
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from app import create_app
from crm_core.ingest import evaluate_intake, evaluate_saved
from crm_core.transfer import transfer_accounts
from crm_store import connect, import_snapshot
from ingest_store import SourceStore
from source_connectors import configured_saved_exports
from source_ops import work_one
from test_source_core import POLICY, event, listing
from transfer_store import SqlSnapshotTransfer
from workflow_store import DEMO_PASSWORD, save_suppression_rule, seed_demo_users, transaction


NAME = "Example Trade Association"


class RootDomainIngestTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / "instance" / f"r102-ingest-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(self.clean_up)
        self.database = self.database_target()
        self.app = create_app(self.database)

    def clean_up(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def database_target(self):
        return self.directory / "test.sqlite3"

    def test_r1022_simultaneous_imports_preserve_one_root(self):
        gate = Barrier(2)

        def admit(index):
            with closing(connect(self.database)) as connection:
                gate.wait(timeout=15)
                with transaction(connection):
                    return evaluate_intake(SourceStore(connection), "upload", {
                        "name": NAME, "domain": f"{index}.concurrent.example", "source_id": f"upload-{index}",
                    })[0]

        with ThreadPoolExecutor(max_workers=2) as threads:
            self.assertEqual(list(threads.map(admit, (1, 2))), ["imported", "imported"])
        with closing(connect(self.database, read_only=True)) as connection:
            accounts = [tuple(row) for row in connection.execute("SELECT id, root_domain FROM accounts")]
            self.assertEqual(len(accounts), 1)
            account_id, root = accounts[0]
            self.assertEqual(root, "concurrent.example")
            evidence = [tuple(row) for row in connection.execute("""
                SELECT external_id, account_id FROM imported_source_evidence ORDER BY external_id
            """)]
            self.assertEqual(evidence, [("upload-1", account_id), ("upload-2", account_id)])

    def test_r1026_sibling_hosts_share_one_account_with_persisted_source_replays(self):
        cvent = event("events.trade.example.co.uk", NAME, "event-id")
        directory = listing("national.trade.example.co.uk", NAME, "listing-id")
        partner = {"name": NAME, "website": "https://partner.trade.example.co.uk", "source_id": "same-id"}
        upload = {"name": NAME, "domain": "upload.trade.example.co.uk", "source_id": "same-id"}
        # The registrable domain for these hosts is example.co.uk.
        with closing(connect(self.database)) as connection, transaction(connection):
            connection.execute("INSERT INTO ingest_sources(slug, name, adapter) VALUES ('partner', 'Partner', 'external')")
            store = SourceStore(connection)
            self.assertEqual(evaluate_saved(store, "cvent", cvent, {}, promote=True)[0], "imported")
            self.assertEqual(evaluate_saved(store, "directory", directory, POLICY, promote=True)[0], "imported")
            self.assertEqual(evaluate_intake(store, "partner", partner)[0], "imported")
            self.assertEqual(evaluate_intake(store, "upload", upload)[0], "imported")
            count = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            self.assertEqual(count, 1)
            account = connection.execute("SELECT id, domain, root_domain FROM accounts").fetchone()
            self.assertEqual((account["domain"], account["root_domain"]), ("events.trade.example.co.uk", "example.co.uk"))

            self.assertEqual(evaluate_saved(store, "cvent", cvent, {}, promote=True)[0], "unchanged")
            self.assertEqual(evaluate_saved(store, "directory", directory, POLICY, promote=True)[0], "unchanged")
            self.assertEqual(evaluate_intake(store, "partner", partner)[0], "unchanged")
            self.assertEqual(evaluate_intake(store, "upload", upload)[0], "unchanged")
            moved = dict(partner, website="https://another.example.com")
            self.assertEqual(evaluate_intake(store, "partner", moved)[0], "review")

            saved = {row["source"]: row for row in connection.execute("SELECT * FROM source_records")}
            self.assertEqual(json.loads(saved["cvent"]["details_json"])["organizer_domain"], cvent["domain"])
            self.assertEqual(json.loads(saved["directory"]["details_json"])["host"], directory["details"]["host"])
            self.assertEqual(json.loads(saved["cvent"]["raw_json"]), cvent["raw"])
            self.assertEqual(json.loads(saved["directory"]["raw_json"]), directory["raw"])
            evidence = {row["source_slug"]: row for row in connection.execute("SELECT * FROM imported_source_evidence")}
            self.assertEqual({row["account_id"] for row in (*saved.values(), *evidence.values())}, {account["id"]})
            self.assertEqual({row["external_id"] for row in evidence.values()}, {"same-id"})
            self.assertEqual(json.loads(evidence["partner"]["payload_json"]), partner)
            self.assertEqual(json.loads(evidence["upload"]["payload_json"]), upload)

    def test_r1026_ambiguity_and_company_suppression_prevent_attachment(self):
        with closing(connect(self.database)) as connection, transaction(connection):
            store = SourceStore(connection)
            self.assertEqual(evaluate_saved(store, "directory", listing(), POLICY, promote=True)[0], "imported")
            chapter = listing("north.trade.example", "Example Northern Chapter", source_id="chapter")
            self.assertEqual(evaluate_saved(store, "directory", chapter, POLICY, promote=True)[0], "review")
            self.assertEqual(evaluate_saved(store, "cvent", event("medc.cventevents.com"), {}, promote=True)[0], "suppressed")
            save_suppression_rule(connection, "domain", "blocked.trade.example", "competitor", None, None)
            self.assertEqual(evaluate_saved(store, "cvent", event("blocked.trade.example"), {}, promote=True)[0], "suppressed")
            self.assertEqual(evaluate_saved(store, "cvent", event("events.trade.example"), {}, promote=True)[0], "suppressed")
            self.assertEqual(evaluate_saved(store, "directory", listing(), POLICY, promote=True)[0], "suppressed")
            self.assertEqual(evaluate_intake(store, "upload", {
                "name": NAME, "domain": "trade.example", "source_id": "blocked-upload",
            })[0], "suppressed")
            allowed = event("events.organizer.example", "Other Association", source_id="eligible")
            allowed["details"]["client_domain"] = "medc.cventevents.com"
            self.assertEqual(evaluate_saved(store, "cvent", allowed, {}, promote=True)[0], "imported")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 2)

    def test_r1025_preexisting_subdomain_rule_blocks_later_import_of_company_root(self):
        with closing(connect(self.database)) as connection, transaction(connection):
            store = SourceStore(connection)
            save_suppression_rule(connection, "domain", "national.alpfa.org", "customer", None, None)
            self.assertEqual(evaluate_intake(store, "upload", {
                "name": "ALPFA", "domain": "alpfa.org", "source_id": "after-rule",
                "contact_name": "Example Contact", "contact_email": "example@outside.example",
            })[0], "suppressed")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM people").fetchone()[0], 0)

    def test_r101_import_contact_domain_uses_its_dns_identity(self):
        with closing(connect(self.database)) as connection, transaction(connection):
            store = SourceStore(connection)
            save_suppression_rule(connection, "domain", "xn--strae-oqa.de", "customer", None, None)
            record = {"name": NAME, "domain": "trade.example", "contact_name": "Example Contact"}
            suppressed = evaluate_intake(store, "upload", dict(
                record, source_id="held-idn", contact_email="contact@stra\u00dfe.de",
            ))
            self.assertEqual(suppressed[0], "review")
            self.assertIn("outreach restriction", suppressed[1])
            allowed = evaluate_intake(store, "upload", dict(
                record, source_id="permitted-ascii", contact_email="contact@strasse.de",
            ))
            self.assertEqual(allowed[0], "imported")
            self.assertEqual([row[0] for row in connection.execute("SELECT email FROM people")], ["contact@strasse.de"])

    def test_r1026_curated_seed_reuses_an_existing_root_without_renaming_the_account(self):
        with closing(connect(self.database)) as connection, transaction(connection):
            account_id = SourceStore(connection).create_account(NAME, "events.trade.example", "Association")
        incoming = {"curated": {"name": NAME, "domain": "trade.example", "organization_type": "Association"}}
        records = [{
            "source": "directory", "source_id": "new-listing", "source_url": "https://directory.example/id",
            "source_file": "listings.json", "identity_key": "curated", "reason": None,
            "details": {"host": "trade.example"}, "raw": {"website": "https://trade.example"},
        }]
        import_snapshot(self.database, incoming, records)
        import_snapshot(self.database, incoming, records)
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)
            self.assertEqual(tuple(connection.execute("SELECT id, domain, name FROM accounts").fetchone()),
                             (account_id, "events.trade.example", NAME))
            self.assertEqual(connection.execute("SELECT account_id FROM source_records").fetchone()[0], account_id)

    def test_r1026_prepared_transfer_reuses_a_corroborated_destination_root(self):
        incoming = {"id": 100, "identity_key": "prepared-key", "name": NAME, "domain": "national.trade.example",
                    "organization_type": "Association", "name_source": "unreviewed", "name_source_ref": None,
                    "stage": "qualified"}
        with closing(connect(self.database)) as connection, transaction(connection):
            destination = SourceStore(connection).create_account(NAME, "events.trade.example", "Association")
            store = SqlSnapshotTransfer(connection)
            for apply in (False, True, True):
                counts = {}
                self.assertEqual(transfer_accounts(store, [incoming], {}, counts, apply=apply), {100: destination})
                self.assertEqual(counts["accounts_added"], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)
            with self.assertRaisesRegex(ValueError, "conflicts with a destination account"):
                transfer_accounts(store, [dict(incoming, name="An Unrelated Northern Chapter")], {}, {}, apply=True)

    def test_r1026_legacy_root_conflicts_stay_in_review_and_do_not_create_another_account(self):
        with closing(connect(self.database)) as connection, transaction(connection):
            connection.executemany("""
                INSERT INTO accounts(identity_key, name, domain, organization_type) VALUES (?, ?, ?, 'Association')
            """, [
                ("legacy-first", NAME, "events.trade.example"),
                ("legacy-second", NAME, "national.trade.example"),
            ])
            self.assertEqual(evaluate_intake(SourceStore(connection), "upload", {
                "name": NAME, "domain": "profile.trade.example",
            })[0], "review")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 2)

    def test_r1026_retired_domain_and_existing_source_id_resolve_to_the_survivor(self):
        with closing(connect(self.database)) as connection, transaction(connection):
            store = SourceStore(connection)
            survivor = store.create_account(NAME, "trade.example", "Association")
            old_payload = {"name": NAME, "domain": "old-network.example", "source_id": "original"}
            self.assertEqual(evaluate_intake(store, "upload", old_payload)[0], "imported")
            retired = connection.execute("SELECT id FROM accounts WHERE domain = 'old-network.example'").fetchone()[0]
            connection.execute("UPDATE imported_source_evidence SET account_id = ? WHERE account_id = ?",
                               (survivor, retired))
            connection.execute("UPDATE accounts SET root_domain = NULL WHERE id = ?", (retired,))
            connection.execute("INSERT INTO account_merges VALUES (?, ?, '2026-11-26')", (retired, survivor))

            self.assertEqual(evaluate_intake(store, "upload", old_payload)[0], "unchanged")
            self.assertEqual(evaluate_intake(store, "upload", dict(old_payload, source_id="subsequent"))[0], "imported")
            self.assertEqual({row["account_id"] for row in connection.execute(
                "SELECT account_id FROM imported_source_evidence",
            )}, {survivor})
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 2)

    def test_r1026_admin_source_and_worker_paths_share_one_root(self):
        event_id, listing_id = str(uuid4()), str(uuid4())
        saved = {
            "master_cvent_events.json": {
                "event_uuid": event_id, "cvent_url": f"https://web.cvent.com/event/{event_id}/summary",
                "organizer": NAME, "company_domain": "events.trade.example", "event_name": "Example Annual Meeting",
            },
            "all_associations.json": {
                "name": NAME, "type": "National", "category": "Business", "member_count": "2500",
                "website": "https://national.trade.example",
                "source_url": f"https://directoryofassociations.com/view/?di={listing_id}",
            },
        }
        for filename, row in saved.items():
            (self.directory / filename).write_text(json.dumps([row]), encoding="utf-8")
        with patch.dict(os.environ, CRM_SOURCE_CONNECTORS="cvent,directory"):
            connectors = configured_saved_exports(cvent_dir=self.directory, directory_dir=self.directory)
        self.app.config.update(TESTING=True, SOURCE_CONNECTORS=connectors)
        with closing(connect(self.database)) as connection:
            seed_demo_users(connection)
        admin = self.app.test_client()
        self.assertEqual(admin.get("/login").status_code, 200)

        def post(path, **data):
            with admin.session_transaction() as session:
                token = session["csrf"]
            return admin.post(path, data={"csrf_token": token, **data})

        signed_in = post("/login", email="admin@example.test", password=DEMO_PASSWORD)
        self.assertEqual(signed_in.status_code, 303)
        self.assertEqual(admin.get(signed_in.location).status_code, 200)
        upload = io.BytesIO(f"source_id,name,domain\nupload-1,{NAME},list.trade.example\n".encode())
        with closing(connect(self.database)) as connection:
            for slug in ("cvent", "directory"):
                staged = post(f"/sources/{slug}/queue")
                self.assertEqual(staged.status_code, 303)
                run_id = int(staged.location.rsplit("/", 1)[1])
                self.assertEqual(work_one(connection, connectors=connectors), run_id)
                self.assertEqual(post(staged.location + "/queue").status_code, 303)
                self.assertEqual(work_one(connection, connectors=connectors), run_id)
            staged = post("/sources/upload", source_file=(upload, "companies.csv"))
            self.assertEqual(staged.status_code, 303)
            self.assertEqual(post(staged.location + "/queue").status_code, 303)
            self.assertEqual(work_one(connection, connectors=connectors), int(staged.location.rsplit("/", 1)[1]))

            issued = post("/sources/keys", name="Partner", slug="partner")
            self.assertEqual(issued.status_code, 200)
            key = re.search(rb"<code>([A-Za-z0-9_-]{43})</code>", issued.data)[1].decode()
            machine = self.app.test_client()
            headers = {"Authorization": f"Bearer {key}"}
            opened = machine.post("/api/ingest/partner/runs", headers=headers, json={"idempotency_key": "root-run"})
            self.assertEqual(opened.status_code, 201)
            api_path = f"/api/ingest/partner/runs/{opened.json['run_id']}"
            batch = machine.post(api_path + "/records", headers=headers, json={
                "idempotency_key": "root-batch", "batch_number": 1,
                "records": [{"source_id": "partner-1", "name": NAME, "domain": "api.trade.example"}],
            })
            self.assertEqual(batch.status_code, 201)
            completed = machine.post(api_path + "/complete", headers=headers, json={"expected_batches": 1})
            self.assertEqual(completed.status_code, 202)
            external_id = completed.json["source_run_id"]
            self.assertEqual(post(f"/sources/runs/{external_id}/queue").status_code, 303)
            self.assertEqual(work_one(connection, connectors=connectors), external_id)

            account = connection.execute("SELECT id, domain, root_domain FROM accounts").fetchall()
            self.assertEqual([(row["domain"], row["root_domain"]) for row in account], [
                ("events.trade.example", "trade.example"),
            ])
            links = connection.execute("""
                SELECT source, source_id, account_id FROM source_records
                UNION ALL SELECT source_slug, external_id, account_id FROM imported_source_evidence
            """).fetchall()
            self.assertEqual({tuple(row) for row in links}, {
                ("cvent", event_id, account[0]["id"]), ("directory", listing_id.upper(), account[0]["id"]),
                ("upload", "upload-1", account[0]["id"]), ("partner", "partner-1", account[0]["id"]),
            })
