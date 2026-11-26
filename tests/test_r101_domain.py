"""R1-01 company identity, hostname and contact suppression boundaries."""

import re
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from app import create_app
from crm_core.ingest import evaluate_intake, evaluate_saved
from crm_core.suppression import matching_rule
from crm_store import connect
from cvent_import import _record as cvent_record
from growth_ops import enrich_one
from ingest_store import SourceStore
from test_source_core import POLICY, listing
from workflow_store import DEMO_PASSWORD, is_blocked, transaction


class R101DomainTest(unittest.TestCase):
    def test_r101_3_root_and_nested_domains_match_whole_labels(self):
        for value in ("cventevents.com", "*.cventevents.com"):
            rule = {"kind": "domain", "value": value, "reason": "competitor"}
            for host in ("cventevents.com", "marketing.cventevents.com", "intl.SALES.CVENTEVENTS.COM."):
                with self.subTest(rule=value, host=host):
                    self.assertIs(matching_rule([rule], 42, host), rule)
            for host in ("notcventevents.com", "cventevents.com.example", "organizer.example"):
                with self.subTest(rule=value, host=host):
                    self.assertIsNone(matching_rule([rule], 42, host))

    def test_r101_3_registration_host_is_evidence_and_contact_matching_is_scoped(self):
        account = {"id": 42, "domain": "organizer.example", "registration_host": "events.cventevents.com"}
        domain = {"kind": "domain", "value": "cventevents.com", "reason": "competitor"}
        opt_out = {"kind": "email", "value": "planner@organizer.example", "reason": "opt_out"}
        rules = [domain, opt_out]
        self.assertIsNone(matching_rule(rules, account["id"], account["domain"]))
        self.assertIs(matching_rule(rules, account["id"], account["domain"], ("Planner@ORGANIZER.example",)), opt_out)
        self.assertIs(matching_rule(rules, account["id"], account["domain"], ("staff@nested.cventevents.com",)), domain)

    def test_r101_3_explicit_wildcard_and_unrelated_contact_hosts_keep_hostname_scope(self):
        wildcard = {"kind": "domain", "value": "*.national.alpfa.org", "reason": "customer"}
        domain = dict(wildcard, value="national.alpfa.org")
        self.assertIsNone(matching_rule([wildcard], 42, "alpfa.org"))
        self.assertIsNone(matching_rule([wildcard], 42, "events.alpfa.org"))
        self.assertIs(matching_rule([wildcard], 42, "mail.national.alpfa.org"), wildcard)
        self.assertIsNone(matching_rule([domain], 43, "independent.example", ("contact@events.alpfa.org",)))
        self.assertIs(matching_rule([domain], 43, "independent.example", ("contact@mail.national.alpfa.org",)), domain)

    def test_r101_3_company_scope_preserves_separate_tenants_on_shared_host(self):
        rule = {"kind": "domain", "value": "branch.team.github.io", "reason": "customer"}
        self.assertIs(matching_rule([rule], 42, "team.github.io"), rule)
        self.assertIsNone(matching_rule([rule], 43, "other.github.io"))


class CompanyDomainSuppressionTest(unittest.TestCase):
    clock = datetime(2026, 11, 26, 11, tzinfo=timezone.utc)

    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / "instance" / f"company-holds-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(self.clean_up)
        self.database = self.database_target()
        self.app = create_app(self.database, demo=True)
        self.app.config.update(TESTING=True, CRM_CLOCK=lambda: self.clock)
        self.admin = self.login("admin")
        self.rep = self.login("riya")
        self.listing = listing("national.alpfa.org", "ALPFA", "alpfa-national")

        with closing(connect(self.database)) as connection, transaction(connection):
            store = SourceStore(connection)
            self.alpfa = store.create_account("ALPFA", "alpfa.org", "Association")
            event_id = str(uuid4())
            event = cvent_record({
                "event_uuid": event_id, "event_name": "Shared Registration Meeting",
                "cvent_url": f"https://web.cvent.com/event/{event_id}/summary",
                "resolved_url": "https://national.alpfa.org/partner-meeting",
                "client_domain": "national.alpfa.org",
                "organizer": "Independent Organizer", "organizer_website": "https://independent.example",
            }, {}, {})
            event.update(duplicate=False, shared_domain=False)
            self.assertEqual(evaluate_saved(store, "cvent", event, {}, promote=True)[0], "imported")
            self.independent = connection.execute(
                "SELECT id FROM accounts WHERE domain = 'independent.example'",
            ).fetchone()[0]
            for account_id in (self.alpfa, self.independent):
                connection.execute("""
                    INSERT INTO account_work(account_id, owner_id, stage, revision)
                    VALUES (?, (SELECT id FROM users WHERE email = 'riya@example.test'), 'assigned', 1)
                """, (account_id,))
                connection.execute("""
                    INSERT INTO qualifications(account_id, score, signals, observed_at) VALUES (?, 70, '[]', ?)
                """, (account_id, self.clock.isoformat()))
            self.contacts = {
                address: connection.execute("""
                    INSERT INTO people(account_id, name, email, origin) VALUES (?, ?, ?, 'apollo_offline')
                """, (self.alpfa, name, address)).lastrowid
                for name, address in (
                    ("ALPFA Parent Contact", "parent@alpfa.org"),
                    ("ALPFA Other Chapter Contact", "chapter@events.alpfa.org"),
                    ("ALPFA External Host Contact", "planned@agency.example"),
                )
            }

    def database_target(self):
        return self.directory / "test.sqlite3"

    def clean_up(self):
        for file in self.directory.iterdir():
            file.unlink()
        self.directory.rmdir()

    def login(self, actor):
        client = self.app.test_client()
        self.assertEqual(client.get("/login").status_code, 200)
        response = self.post(client, "/login", email=f"{actor}@example.test", password=DEMO_PASSWORD)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(client.get(response.location).status_code, 200)
        return client

    def post(self, client, url, **data):
        with client.session_transaction() as session:
            token = session["csrf"]
        return client.post(url, data={"csrf_token": token, **data})

    def add_rule(self, kind, value, reason):
        self.assertEqual(self.post(self.admin, "/suppression", kind=kind, value=value, reason=reason).status_code, 303)

    def test_r101_subdomain_rule_covers_parent_account_before_observation_and_later_import(self):
        self.add_rule("domain", "national.alpfa.org", "customer")
        with closing(connect(self.database)) as connection, transaction(connection):
            store = SourceStore(connection)
            with self.subTest("stored company status"):
                self.assertEqual(tuple(connection.execute(
                    "SELECT suppressed, suppress_reason FROM accounts WHERE id = ?", (self.alpfa,),
                ).fetchone()), (True, "customer"))
                self.assertTrue(is_blocked(connection, self.alpfa))
            with self.subTest("later source on a different company subdomain"):
                later = listing("east.alpfa.org", "ALPFA", "alpfa-east")
                self.assertEqual(evaluate_saved(store, "directory", later, POLICY, promote=True)[0], "suppressed")
            with self.subTest("registration evidence is preserved and excluded from the match"):
                self.assertFalse(is_blocked(connection, self.independent))
                self.assertFalse(is_blocked(connection, self.independent, candidate_email="planner@events.alpfa.org"))
                self.assertTrue(is_blocked(connection, self.independent, candidate_email="planner@national.alpfa.org"))
                self.assertEqual(connection.execute("""
                    SELECT client_domain FROM cvent_read WHERE company_id = ?
                """, (self.independent,)).fetchone()[0], "national.alpfa.org")
            with self.subTest("unrelated organization can import a sibling-host contact"):
                uploader = {
                    "name": "Independent Organizer", "domain": "independent.example",
                    "contact_name": "External Planner", "contact_email": "planner@events.alpfa.org",
                }
                self.assertEqual(evaluate_intake(store, "upload", dict(uploader, source_id="eligible"))[0], "imported")
                exact = dict(uploader, source_id="held", contact_email="planner@national.alpfa.org")
                self.assertEqual(evaluate_intake(store, "upload", exact)[0], "review")
                self.assertEqual([row["email"] for row in connection.execute(
                    "SELECT email FROM people WHERE account_id = ?", (self.independent,),
                )], ["planner@events.alpfa.org"])

    def test_r101_observed_subdomain_holds_company_contacts_and_refuses_paid_actions_and_outreach(self):
        with closing(connect(self.database)) as connection, transaction(connection):
            result = evaluate_saved(SourceStore(connection), "directory", self.listing, POLICY, promote=True)
            self.assertEqual(result[0], "imported")
            self.assertEqual(connection.execute("""
                SELECT account_id FROM source_records WHERE source = 'directory' AND source_id = 'alpfa-national'
            """).fetchone()[0], self.alpfa)
        self.add_rule("domain", "national.alpfa.org", "customer")
        with self.subTest("scoped list"):
            default = self.rep.get("/my-accounts")
            self.assertNotIn(b"ALPFA", default.data)
            self.assertIn(b"Independent Organizer", default.data)
            inspection = self.rep.get("/my-accounts?show_suppressed=1")
            self.assertIn(b"ALPFA", inspection.data)
            self.assertIn(b"Customer", inspection.data)

        account_path = f"/accounts/{self.alpfa}"
        with self.subTest("people search"):
            response = self.post(self.rep, account_path + "/enrich")
            self.assertEqual(response.status_code, 409)
            self.assertIn(b"Enrichment is blocked by suppression (Customer).", response.data)
        for index, (address, person_id) in enumerate(self.contacts.items()):
            with self.subTest(action="unlock", address=address):
                unlock = self.post(self.rep, account_path + "/enrich", person_id=person_id)
                self.assertEqual(unlock.status_code, 409)
                self.assertIn(b"Email unlock is blocked by suppression (Customer).", unlock.data)
            with self.subTest(action="outreach", address=address):
                outreach = self.post(self.rep, account_path + "/activities", revision=1, channel="email",
                                     outcome="sent", person_id=person_id, action_key=f"held-alpfa-{index}")
                self.assertEqual(outreach.status_code, 403)
        with closing(connect(self.database)) as connection, self.subTest("no queued paid work, credits or outreach"):
            for table in ("credit_jobs", "credit_ledger", "activities"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            provider = Mock()
            self.assertIsNone(enrich_one(connection, provider, clock=lambda: self.clock))
            provider.assert_not_called()

    def test_r101_exact_email_opt_out_leaves_other_company_contacts_and_account_eligible(self):
        address = "parent@alpfa.org"
        self.add_rule("email", address, "opt_out")
        with closing(connect(self.database)) as connection:
            self.assertEqual(tuple(connection.execute(
                "SELECT suppressed, suppress_reason FROM accounts WHERE id = ?", (self.alpfa,),
            ).fetchone()), (False, None))
            for email, person_id in self.contacts.items():
                with self.subTest(email=email):
                    self.assertEqual(is_blocked(connection, self.alpfa, person_id), email == address)
        self.assertIn(b"ALPFA", self.rep.get("/my-accounts").data)

    def test_r101_company_hold_refuses_unlock_and_outreach_after_account_merge(self):
        person = self.contacts["planned@agency.example"]
        with closing(connect(self.database)) as connection, transaction(connection):
            retired = SourceStore(connection).create_account("ALPFA", "alpfa-legacy.example", "Association")
            connection.execute("UPDATE people SET account_id = ? WHERE id = ?", (retired, person))

        self.add_rule("domain", "national.alpfa.org", "customer")
        preview = self.post(self.admin, "/account-merges/preview", first_account_id=retired, second_account_id=self.alpfa)
        self.assertEqual(preview.status_code, 200)
        token = re.search(rb'name="merge_preview" value="([^"]+)"', preview.data)
        self.assertIsNotNone(token)
        merged = self.post(self.admin, "/account-merges/confirm", merge_preview=token[1].decode())
        self.assertEqual(merged.status_code, 303)
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual(connection.execute("SELECT to_id FROM account_merges WHERE from_id = ?", (retired,)).fetchone()[0],
                             self.alpfa)
            self.assertEqual(tuple(connection.execute(
                "SELECT account_id, email FROM people WHERE id = ?", (person,),
            ).fetchone()), (self.alpfa, "planned@agency.example"))
            revision = connection.execute("SELECT revision FROM account_work WHERE account_id = ?", (self.alpfa,)).fetchone()[0]
        account_path = f"/accounts/{self.alpfa}"
        unlock = self.post(self.rep, account_path + "/enrich", person_id=person)
        self.assertEqual(unlock.status_code, 409)
        self.assertIn(b"Email unlock is blocked by suppression (Customer).", unlock.data)
        outreach = self.post(self.rep, account_path + "/activities", revision=revision, channel="email",
                             outcome="sent", person_id=person, action_key="held-alpfa-after-merge")
        self.assertEqual(outreach.status_code, 403)
        with closing(connect(self.database, read_only=True)) as connection:
            for table in ("credit_jobs", "credit_ledger", "activities"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_r101_upgrade_refreshes_company_flags_once_without_rewriting_old_rule(self):
        with closing(connect(self.database)) as connection, transaction(connection):
            connection.execute("DELETE FROM workflow_settings WHERE key = 'suppression_company_domains'")
            rule_id = connection.execute("""
                INSERT INTO suppression(kind, value, reason, source, reason_note, created_at)
                VALUES ('domain', 'national.alpfa.org', 'customer', 'manual', 'Prior relationship', ?)
            """, (self.clock.isoformat(),)).lastrowid
            expected_rule = tuple(connection.execute("SELECT * FROM suppression WHERE id = ?", (rule_id,)).fetchone())
            self.assertEqual(tuple(connection.execute("""
                SELECT suppressed, suppress_reason FROM accounts WHERE id = ?
            """, (self.alpfa,)).fetchone()), (False, None))

        for _ in range(2):
            create_app(self.database)
            with closing(connect(self.database)) as connection:
                self.assertEqual(tuple(connection.execute("""
                    SELECT suppressed, suppress_reason FROM accounts WHERE id = ?
                """, (self.alpfa,)).fetchone()), (True, "customer"))
                self.assertEqual(tuple(connection.execute(
                    "SELECT * FROM suppression WHERE id = ?", (rule_id,),
                ).fetchone()), expected_rule)
