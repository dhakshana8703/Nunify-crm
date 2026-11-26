"""Acceptance coverage for enrichment response safety and suppression rule history."""

import io
import json
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from app import create_app
from crm_core.ingest import evaluate_intake
from crm_store import connect
from crm_store import initialize as initialize_crm
from growth_ops import enrich_one
from ingest_store import SourceStore
from workflow_store import DEMO_PASSWORD, is_blocked, suppression_rules
from workflow_store import initialize as initialize_workflow
from workflow_store import transaction


class SuppressionFoundationTest(unittest.TestCase):
    clock = datetime(2026, 11, 26, 11, tzinfo=timezone.utc)

    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / "instance" / f"suppression-test-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(self.clean_up)
        self.database = self.database_target()
        self.app = create_app(self.database, demo=True)
        self.app.config.update(TESTING=True, CRM_CLOCK=lambda: self.clock)
        self.admin = self.login("admin", "/suppression")
        self.rep = self.login("riya", "/today")
        with closing(connect(self.database)) as connection, connection:
            self.account_id = connection.execute("""
                INSERT INTO accounts(identity_key, name, domain, organization_type)
                VALUES ('organizer', 'Example Organizer', 'organizer.example', 'Association')
            """).lastrowid
            connection.execute("""
                INSERT INTO account_work(account_id, owner_id, stage)
                VALUES (?, (SELECT id FROM users WHERE email = 'riya@example.test'), 'assigned')
            """, (self.account_id,))
            connection.execute("""
                INSERT INTO qualifications(account_id, score, signals, observed_at) VALUES (?, 70, '[]', ?)
            """, (self.account_id, self.clock.isoformat()))
            self.person_id = connection.execute("""
                INSERT INTO people(account_id, name, origin) VALUES (?, 'Example Contact', 'apollo_offline')
            """, (self.account_id,)).lastrowid

    def database_target(self):
        return self.directory / "test.sqlite3"

    def clean_up(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def login(self, actor, landing):
        client = self.app.test_client()
        self.assertEqual(client.get("/login").status_code, 200)
        response = self.post(client, "/login", email=f"{actor}@example.test", password=DEMO_PASSWORD)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(client.get(landing).status_code, 200)
        return client

    def post(self, client, url, **data):
        with client.session_transaction() as session:
            token = session["csrf"]
        return client.post(url, data={"csrf_token": token, **data})

    def add_rule(self, kind, value, reason, reason_note=""):
        self.assertEqual(self.post(self.admin, "/suppression", kind=kind, value=value, reason=reason,
                                    reason_note=reason_note).status_code, 303)

    def queue_unlock(self):
        response = self.post(self.rep, f"/accounts/{self.account_id}/enrich", person_id=self.person_id)
        self.assertEqual(response.status_code, 303)

    def check_blocked_result(self, provider, reason):
        with closing(connect(self.database)) as connection:
            job_id = enrich_one(connection, provider, clock=lambda: self.clock)
            self.assertIsNotNone(job_id)
            self.assertIsNone(enrich_one(connection, provider, clock=lambda: self.clock))
            provider.assert_called_once()
            job = connection.execute("SELECT state, error FROM credit_jobs WHERE id = ?", (job_id,)).fetchone()
            self.assertEqual(job["state"], "blocked")
            self.assertEqual(job["error"], f"Suppression blocked publishing the enrichment result ({reason}).")
            contact = connection.execute("SELECT email, unlocked, email_verified FROM people WHERE id = ?",
                                         (self.person_id,)).fetchone()
            self.assertEqual(tuple(contact), ("", 0, 0))
            self.assertEqual([tuple(row) for row in connection.execute("SELECT job_id, credits FROM credit_ledger")],
                             [(job_id, 1)])

    def test_r101_paid_actions_explain_account_and_contact_restrictions_before_queuing(self):
        route = f"/accounts/{self.account_id}/enrich"
        with closing(connect(self.database)) as connection, connection:
            connection.execute("UPDATE qualifications SET score = 45 WHERE account_id = ?", (self.account_id,))
            connection.execute("UPDATE people SET email = 'held@organizer.example' WHERE id = ?", (self.person_id,))
        self.assertEqual(self.post(self.admin, "/suppression", kind="email", value="held@organizer.example",
                                    reason="opt_out").status_code, 303)
        unlock = self.post(self.rep, route, person_id=self.person_id)
        self.assertEqual(unlock.status_code, 409)
        self.assertIn(b"Email unlock is blocked by suppression (Opted out).", unlock.data)
        self.assertEqual(self.post(self.admin, "/suppression", kind="domain", value="organizer.example",
                                    reason="customer").status_code, 303)
        for submitted, message in (({}, b"Enrichment is blocked by suppression (Customer)."),
                                   ({"person_id": self.person_id}, b"Email unlock is blocked by suppression")):
            with self.subTest(submitted=submitted):
                response = self.post(self.rep, route, **submitted)
                self.assertEqual(response.status_code, 409)
                self.assertIn(message, response.data)
        with closing(connect(self.database)) as connection:
            provider = Mock()
            self.assertIsNone(enrich_one(connection, provider, clock=lambda: self.clock))
            provider.assert_not_called()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM credit_jobs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 0)

    def test_r101_admin_forms_record_reasons_and_provenance(self):
        page = self.admin.get("/suppression")
        self.assertIn(b'<select name="reason">', page.data)
        for code in ("competitor", "customer", "open_deal", "opt_out", "bounced", "manual"):
            self.assertIn(f'<option value="{code}">'.encode(), page.data)
        self.assertEqual(self.post(self.admin, "/suppression", kind="domain", value="blocked.example",
                                    reason="customer", reason_note="Existing relationship",
                                    source="hubspot", created_by=500).status_code, 303)
        contents = b"kind,value,reason,reason_note\nemail,held@other.example,bounced,Mailbox rejected\n"
        self.assertEqual(self.post(self.admin, "/suppression",
                                    rules=(io.BytesIO(contents), "rules.csv")).status_code, 303)
        with closing(connect(self.database, read_only=True)) as connection:
            rows = connection.execute("""
                SELECT s.value, s.reason, s.reason_note, s.source, u.email
                FROM suppression s JOIN users u ON u.id = s.created_by
                WHERE s.value IN ('blocked.example', 'held@other.example') ORDER BY s.value
            """).fetchall()
            self.assertEqual([tuple(row) for row in rows], [
                ("blocked.example", "customer", "Existing relationship", "manual", "admin@example.test"),
                ("held@other.example", "bounced", "Mailbox rejected", "csv", "admin@example.test"),
            ])
        page = self.admin.get("/suppression?search=Mailbox")
        for label in (b"held@other.example", b"email - Bounced - CSV", b"Mailbox rejected"):
            self.assertIn(label, page.data)
        self.assertNotIn(b"blocked.example", page.data)

    def test_r101_idn_domain_form_and_csv_share_one_rule_for_an_imported_company(self):
        display = "b\u00fccher.\u0440\u0444"
        encoded = "xn--bcher-kva.xn--p1ai"
        with closing(connect(self.database)) as connection, transaction(connection):
            state, _ = evaluate_intake(SourceStore(connection), "upload", {
                "name": "Example Books", "domain": "events." + display, "source_id": "idn-company",
            })
            self.assertEqual(state, "imported")
            account = connection.execute("SELECT id FROM accounts WHERE domain = ?", ("events." + encoded,)).fetchone()[0]
            rep = connection.execute("SELECT id FROM users WHERE email = 'riya@example.test'").fetchone()[0]
            connection.execute("INSERT INTO account_work(account_id, owner_id, stage) VALUES (?, ?, 'assigned')",
                               (account, rep))
            connection.execute("INSERT INTO qualifications(account_id, score, signals, observed_at) VALUES (?, 75, '[]', ?)",
                               (account, self.clock.isoformat()))
            person = connection.execute("INSERT INTO people(account_id, name, origin) VALUES (?, 'Saved', 'apollo_offline')",
                                        (account,)).lastrowid

        self.add_rule("domain", display.upper() + ".", "customer")
        duplicate = f"kind,value,reason\ndomain,{encoded},manual\n".encode()
        uploaded = self.post(self.admin, "/suppression", rules=(io.BytesIO(duplicate), "duplicate.csv"))
        self.assertEqual(uploaded.status_code, 303)
        with closing(connect(self.database, read_only=True)) as connection:
            rule = connection.execute("SELECT reason, source FROM suppression WHERE value = ?", (encoded,)).fetchall()
            self.assertEqual([tuple(row) for row in rule], [("customer", "manual")])
            flags = connection.execute("SELECT suppressed, suppress_reason FROM accounts WHERE id = ?", (account,)).fetchone()
            self.assertEqual(tuple(flags), (True, "customer"))
        self.assertNotIn(b"Example Books", self.rep.get("/my-accounts").data)
        self.assertIn(b"Example Books", self.rep.get("/my-accounts?show_suppressed=1").data)
        for fields, action in (({}, b"Enrichment"), ({"person_id": person}, b"Email unlock")):
            response = self.post(self.rep, f"/accounts/{account}/enrich", **fields)
            self.assertEqual(response.status_code, 409)
            self.assertIn(action + b" is blocked by suppression (Customer).", response.data)
        with closing(connect(self.database)) as connection:
            provider = Mock()
            self.assertIsNone(enrich_one(connection, provider, clock=lambda: self.clock))
            provider.assert_not_called()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 0)

    def test_r101_idn_csv_keeps_wildcard_subdomain_scope_and_rejects_decorations(self):
        display = "stra\u00dfe.de"
        contents = f"kind,value,reason\ndomain,*.www.{display},customer\n".encode("utf-8")
        uploaded = self.post(self.admin, "/suppression", rules=(io.BytesIO(contents), "unicode.csv"))
        self.assertEqual(uploaded.status_code, 303)
        for invalid in ("https://www.example.org", "www.example.org/path", "www.example.org:443", "-bad.example.org"):
            with self.subTest(invalid=invalid):
                batch = f"kind,value,reason\ndomain,valid.example,manual\ndomain,{invalid},manual\n".encode()
                self.assertEqual(self.post(self.admin, "/suppression", rules=(io.BytesIO(batch), "bad.csv")).status_code, 400)
        with closing(connect(self.database, read_only=True)) as connection:
            added = connection.execute("SELECT value FROM suppression WHERE source = 'csv'").fetchall()
            self.assertEqual([row[0] for row in added], ["*.www.xn--strae-oqa.de"])
            for host in (display, f"events.{display}", "team.www.strasse.de", "www.xn--strae-oqa.de.example"):
                with self.subTest(host=host):
                    self.assertFalse(is_blocked(connection, self.account_id, candidate_email=f"contact@{host}"))
        self.queue_unlock()
        self.check_blocked_result(Mock(return_value={"email": f"contact@team.www.{display}"}), "Customer")

    def test_r101_idn_upgrade_refreshes_flags_and_keeps_existing_rule_ids(self):
        legacy_value = "stra\u00dfe.de"
        with closing(connect(self.database)) as connection:
            with transaction(connection):
                connection.execute("DELETE FROM workflow_settings WHERE key = 'suppression_idn_hosts'")
                connection.execute("UPDATE accounts SET domain = 'xn--strae-oqa.de' WHERE id = ?", (self.account_id,))
                rule = connection.execute("""
                    INSERT INTO suppression(kind, value, reason, source, created_at)
                    VALUES ('domain', ?, 'customer', 'legacy', ?)
                """, (legacy_value, self.clock.isoformat())).lastrowid
            for _ in range(2):
                initialize_workflow(connection)
            data = connection.execute("SELECT suppressed, suppress_reason FROM accounts WHERE id = ?",
                                      (self.account_id,)).fetchone()
            self.assertEqual(tuple(data), (True, "customer"))
            stored = connection.execute("SELECT value, source FROM suppression WHERE id = ?", (rule,)).fetchone()
            self.assertEqual(tuple(stored), (legacy_value, "legacy"))

    def test_r101_rejects_unknown_reasons_and_non_admin_access(self):
        manager = self.login("manager", "/assignment")
        for user in (manager, self.rep):
            self.assertEqual(user.get("/suppression").status_code, 403)
            self.assertEqual(self.post(user, "/suppression", kind="domain", value="denied.example",
                                        reason="competitor").status_code, 403)
            self.assertEqual(self.post(user, "/suppression/1/remove").status_code, 403)
        invalid = self.post(self.admin, "/suppression", kind="domain", value="blocked.example", reason="unknown")
        self.assertEqual(invalid.status_code, 400)
        self.assertIn(b"Choose a suppression reason", invalid.data)
        for contents in (
            b"kind,value,reason\ndomain,valid.example,manual\nemail,z@valid.example,unknown\n",
            b"kind,value,reason\ndomain,valid.example,manual\nemail,z@valid.example,bounced,extra\n",
            b"kind,value,reason,source\ndomain,valid.example,manual,hubspot\n",
        ):
            with self.subTest(contents=contents):
                response = self.post(self.admin, "/suppression", rules=(io.BytesIO(contents), "rules.csv"))
                self.assertEqual(response.status_code, 400)
                with closing(connect(self.database, read_only=True)) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM suppression").fetchone()[0], 4)

    def test_ef03_rechecks_restrictions_added_during_provider_call(self):
        self.queue_unlock()

        def deliver(_job, *, idempotency_key):
            self.assertTrue(idempotency_key)
            self.add_rule("account", self.account_id, "manual", "Outreach restricted while lookup is in flight")
            return {"email": "found@organizer.example"}

        self.check_blocked_result(Mock(side_effect=deliver), "Outreach restricted while lookup is in flight")

    def test_ef03_blocks_an_address_identified_by_the_provider_and_keeps_the_charge_once(self):
        self.add_rule("email", "restricted@organizer.example", "opt_out")
        self.queue_unlock()
        provider = Mock(return_value={"email": "Restricted@organizer.example"})
        self.check_blocked_result(provider, "Opted out")
        self.queue_unlock()
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM credit_jobs").fetchone()[0], 1)

    def test_ef04_invalid_headers_and_invalid_rows_add_no_rules(self):
        for contents in (b"type,address,description\n", b"kind,value,reason\ndomain,valid.example,manual\ninvalid,no,manual\n"):
            with self.subTest(contents=contents):
                response = self.post(self.admin, "/suppression", rules=(io.BytesIO(contents), "rules.csv"))
                self.assertEqual(response.status_code, 400)
                with closing(connect(self.database, read_only=True)) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM suppression").fetchone()[0], 4)

    def test_ef04_removal_preserves_the_rule_and_actor_then_allows_readding(self):
        self.add_rule("domain", "organizer.example", "customer", "Existing customer")
        with closing(connect(self.database, read_only=True)) as connection:
            rule_id = connection.execute("SELECT id FROM suppression WHERE value = 'organizer.example'").fetchone()[0]
            self.assertTrue(is_blocked(connection, self.account_id))
        self.assertEqual(self.post(self.admin, f"/suppression/{rule_id}/remove").status_code, 303)
        self.assertEqual(self.post(self.admin, f"/suppression/{rule_id}/remove").status_code, 303)
        with closing(connect(self.database, read_only=True)) as connection:
            stored = connection.execute("SELECT * FROM suppression WHERE id = ?", (rule_id,)).fetchone()
            self.assertIsNotNone(stored)
            self.assertIsNotNone(stored["removed_at"])
            self.assertFalse(is_blocked(connection, self.account_id))
            events = connection.execute("""
                SELECT u.email, e.details FROM audit_events e JOIN users u ON u.id = e.actor_id
                WHERE e.action = 'suppression_remove'
            """).fetchall()
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["email"], "admin@example.test")
            self.assertEqual(json.loads(event["details"]), {
                "rule_id": rule_id, "kind": "domain", "value": "organizer.example", "reason": "customer",
                "source": "manual", "reason_note": "Existing customer",
                "created_by": stored["created_by"], "created_at": stored["created_at"],
            })
        self.assertNotIn(b"organizer.example", self.admin.get("/suppression").data)
        self.add_rule("domain", "organizer.example", "customer", "Re-added customer")
        with closing(connect(self.database, read_only=True)) as connection:
            stored = connection.execute("""
                SELECT reason, reason_note, source, removed_at FROM suppression WHERE id = ?
            """, (rule_id,)).fetchone()
            self.assertEqual(tuple(stored), ("customer", "Re-added customer", "manual", None))
            self.assertTrue(is_blocked(connection, self.account_id))

    def test_ef04_old_rules_survive_the_migration_and_read_only_transfer(self):
        legacy_database = self.directory / "legacy.sqlite3"
        initialize_crm(legacy_database)
        with closing(connect(legacy_database)) as connection, connection:
            connection.execute("""
                CREATE TABLE suppression (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL COLLATE NOCASE,
                    reason TEXT NOT NULL, created_by INTEGER, created_at TEXT NOT NULL, UNIQUE (kind, value)
                )
            """)
            connection.execute("""
                INSERT INTO suppression (id, kind, value, reason, created_at)
                VALUES (40, 'domain', 'legacy.example', 'Old restriction', '2026-11-20')
            """)
        with closing(connect(legacy_database, read_only=True)) as connection:
            self.assertEqual([rule["id"] for rule in suppression_rules(connection)], [40])
        with closing(connect(legacy_database)) as connection:
            initialize_workflow(connection)
            initialize_workflow(connection)
            rule = connection.execute("""
                SELECT reason, reason_note, source, removed_at FROM suppression WHERE id = 40
            """).fetchone()
            self.assertEqual(tuple(rule), ("manual", "Old restriction", "legacy", None))
