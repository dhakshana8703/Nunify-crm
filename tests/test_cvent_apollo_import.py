import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from crm_store import SCHEMA as CRM_SCHEMA
from cvent_apollo_import import import_leads
from growth_ops import initialize as initialize_growth
from workflow_store import initialize as initialize_workflow


class CventApolloImportTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / "instance" / f"test-apollo-import-{uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.cache = self.directory / "cache.sqlite3"
        self.addCleanup(self.remove_cache)
        self.crm = sqlite3.connect(":memory:")
        self.addCleanup(self.crm.close)
        self.crm.row_factory = sqlite3.Row
        self.crm.execute("PRAGMA foreign_keys = ON")
        self.crm.executescript(CRM_SCHEMA)
        initialize_workflow(self.crm)
        initialize_growth(self.crm)
        with closing(sqlite3.connect(self.cache)) as cache:
            cache.executescript("""
                CREATE TABLE apollo_candidates (
                    apollo_id TEXT PRIMARY KEY, company_id INTEGER, company_name TEXT, domain TEXT,
                    name TEXT, title TEXT, persona TEXT, is_unlocked INTEGER,
                    email TEXT, linkedin_url TEXT, discovered_at TEXT, unlocked_at TEXT,
                    first_name TEXT, last_name TEXT
                );
                CREATE TABLE apollo_people_cache (
                    lookup_key TEXT PRIMARY KEY, apollo_id TEXT, email TEXT, raw_json TEXT
                );
            """)
        self.admin_id = self.user("admin")
        self.manager_id = self.user("manager")
        self.rep_id = self.user("sdr")
        self.account_id = self.account("Partner Network", "partner.test")

    def remove_cache(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def user(self, role):
        with self.crm:
            return self.crm.execute("""
                INSERT INTO users(name, email, role, timezone, password_hash)
                VALUES ('Fixture User', ?, ?, 'Asia/Kolkata', 'unused')
            """, (f"{role}@example.test", role)).lastrowid

    def account(self, name, domain, *, source="cvent", proof="Corroborated organizer website"):
        with self.crm:
            account_id = self.crm.execute("""
                INSERT INTO accounts(identity_key, name, domain, organization_type)
                VALUES (?, ?, ?, 'Association')
            """, (domain, name, domain)).lastrowid
            source_id = str(uuid4())
            url = f"https://web.cvent.com/event/{source_id}/summary"
            details = {"organizer_domain": domain, "organizer_company": name, "identity_evidence": proof}
            self.crm.execute("""
                INSERT INTO source_records(source, source_id, source_url, source_file, account_id, details_json, raw_json)
                VALUES (?, ?, ?, 'events.json', ?, ?, '{}')
            """, (source, source_id, url, account_id, json.dumps(details)))
        return account_id

    def lead(self, apollo_id, name, *, company_id=41, company="Partner Network", domain="partner.test",
             unlocked=False, email="", linkedin="", verified=False):
        with closing(sqlite3.connect(self.cache)) as cache, cache:
            cache.execute("""
                INSERT OR REPLACE INTO apollo_candidates
                    (apollo_id, company_id, company_name, domain, name, title, persona, is_unlocked,
                     email, linkedin_url, discovered_at, unlocked_at, first_name, last_name)
                VALUES (?, ?, ?, ?, ?, 'Original Role', 'P1', ?, ?, ?, '2026-11-19', ?, ?, ?)
            """, (apollo_id, company_id, company, domain, name, int(unlocked), email, linkedin,
                  "2026-11-20" if unlocked else None, name.split()[0], name.split()[-1]))
            if email:
                profile = {"id": apollo_id, "email": email, "email_status": "verified" if verified else "unverified"}
                cache.execute("""
                    INSERT OR REPLACE INTO apollo_people_cache(lookup_key, apollo_id, email, raw_json)
                    VALUES (?, ?, ?, ?)
                """, (apollo_id, apollo_id, email, json.dumps(profile)))

    def apply(self):
        return import_leads(self.crm, self.cache, apply=True, actor_id=self.admin_id)

    def test_admin_apply_uses_organizer_evidence_and_preserves_channel_access(self):
        self.account("Directory Only", "directory.test", source="directory")
        self.account("Weak Organizer", "weak.test", proof="")
        self.lead("locked", "Locked Fixture", email="locked@partner.test",
                  linkedin="https://www.linkedin.com/in/locked-fixture", verified=True)
        self.lead("verified", "Verified Fixture", unlocked=True, email="verified@partner.test", verified=True)
        self.lead("linkedin", "LinkedIn Fixture", unlocked=True, linkedin="https://linkedin.com/in/linkedin-fixture")
        self.lead("unverified", "Unverified Fixture", unlocked=True, email="unverified@partner.test")
        self.lead("wrong-name", "Wrong Name", company="Conflicting Organizer")
        self.lead("directory-only", "Directory Fixture", company_id=42, company="Directory Only", domain="directory.test")
        self.lead("weak", "Weak Fixture", company_id=43, company="Weak Organizer", domain="weak.test")
        self.lead("blocked", "Blocked Fixture", unlocked=True, email="blocked@partner.test")
        with self.crm:
            self.crm.execute("""
                INSERT INTO suppression(kind, value, reason, created_at)
                VALUES ('email', 'blocked@partner.test', 'opt_out', '2026-11-20')
            """)

        before = self.crm.total_changes
        preview = import_leads(self.crm, self.cache)
        self.assertEqual(preview["new_people"], 4)
        self.assertEqual(preview["review_identity"], 3)
        self.assertEqual(preview["suppressed"], 1)
        with self.assertRaises(PermissionError):
            import_leads(self.crm, self.cache, apply=True, actor_id=self.manager_id)
        self.assertEqual(self.crm.total_changes, before)
        self.assertEqual(self.apply(), preview)

        people = {row["name"]: tuple(row)[1:] for row in self.crm.execute("""
            SELECT name, account_id, origin, unlocked, email, email_verified, linkedin_url FROM people
        """)}
        self.assertEqual(people, {
            "Locked Fixture": (self.account_id, "apollo", 0, "", 0, ""),
            "Verified Fixture": (self.account_id, "apollo", 1, "verified@partner.test", 1, ""),
            "LinkedIn Fixture": (self.account_id, "apollo", 1, "", 0, "https://www.linkedin.com/in/linkedin-fixture"),
            "Unverified Fixture": (self.account_id, "apollo", 1, "unverified@partner.test", 0, ""),
        })
        self.assertEqual(self.crm.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 0)

    def test_unlock_refresh_preserves_rep_work_and_ignores_old_locked_data(self):
        self.lead("stable", "Original Fixture")
        self.assertEqual(self.apply()["new_people"], 1)
        with self.crm:
            person_id = self.crm.execute("SELECT id FROM people").fetchone()[0]
            self.crm.execute("""
                UPDATE people SET name='Rep edited name', title='Rep edited role', persona='P2',
                                  phone='555-0100', status='replied', details_revision=3 WHERE id=?
            """, (person_id,))
            self.crm.execute("""
                INSERT INTO account_work(account_id, owner_id, stage, revision)
                VALUES (?, ?, 'discovered', 6)
            """, (self.account_id, self.rep_id))
            job_id = self.crm.execute("""
                INSERT INTO credit_jobs(account_id, actor_id, action, state, on_day, budget_day, request_key, created_at)
                VALUES (?, ?, 'people_search', 'done', '2026-11-20', '2026-11-20', 'fixture-job', '2026-11-20')
            """, (self.account_id, self.rep_id)).lastrowid
            self.crm.execute("""
                INSERT INTO credit_ledger(job_id, user_id, account_id, action, credits, on_day, at)
                VALUES (?, ?, ?, 'people_search', 1, '2026-11-20', '2026-11-20')
            """, (job_id, self.rep_id, self.account_id))
        protected = ("account_work", "credit_jobs", "credit_ledger")
        baseline = {table: self.crm.execute(f"SELECT * FROM {table}").fetchall() for table in protected}
        self.lead("stable", "Changed Fixture", unlocked=True, email="changed@partner.test", verified=True)
        preview = import_leads(self.crm, self.cache)
        self.assertEqual(preview["updated_people"], 1)
        self.assertEqual(self.apply(), preview)

        def saved_person():
            return tuple(self.crm.execute("""
                SELECT id, name, title, persona, phone, status, details_revision, unlocked, email, email_verified
                FROM people
            """).fetchone())

        expected = (person_id, "Rep edited name", "Rep edited role", "P2", "555-0100", "replied", 4, 1,
                    "changed@partner.test", 1)
        self.assertEqual(saved_person(), expected)
        before = self.crm.total_changes
        self.assertEqual(self.apply()["existing_people"], 1)
        self.lead("stable", "Original Fixture")
        self.assertEqual(self.apply()["existing_people"], 1)
        self.assertEqual(saved_person(), expected)
        self.assertEqual(self.crm.total_changes, before)
        self.assertEqual({table: self.crm.execute(f"SELECT * FROM {table}").fetchall() for table in protected}, baseline)
