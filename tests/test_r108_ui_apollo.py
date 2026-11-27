"""R1-08.2 hosted Apollo actions through the signed-in account page."""

import unittest
from contextlib import closing
from decimal import Decimal
from unittest.mock import Mock

from app import create_app
from crm_core.growth import request_enrichment
from crm_store import connect
from growth_store import SqlGrowthStore
from test_postgres_pilot import PostgresDatabase, TEST_URL
import test_r106_stages
import test_r108_credit_reporting
from workflow_store import save_suppression_rule, transaction


OBSERVED = test_r106_stages.OBSERVED


class ApolloUiTest(unittest.TestCase):
    database_target = test_r106_stages.AccountStageTest.database_target
    make_account = test_r106_stages.AccountStageTest.make_account

    def setUp(self):
        self.database = self.database_target()
        self.app = create_app(self.database, demo=True)
        self.provider = Mock(requires_confirmation=True, return_value={
            "credits": 0, "name": "Sam Example", "title": "Head of Events", "apollo_person_id": "result-id",
        })
        self.app.config.update(
            TESTING=True, CRM_CLOCK=lambda: OBSERVED, APOLLO_WEB_ENABLED=True,
            APOLLO_PROVIDER=self.provider, APOLLO_ALLOW_PAID=False,
        )

    def qualified(self, db, key):
        account_id = self.make_account(db, key)
        owner_id = db.execute("SELECT id FROM users WHERE email = 'riya@example.test'").fetchone()[0]
        db.execute("INSERT INTO account_work (account_id, owner_id, stage) VALUES (?, ?, 'assigned')",
                   (account_id, owner_id))
        db.execute("INSERT INTO qualifications (account_id, score, signals, observed_at) VALUES (?, 90, '[]', ?)",
                   (account_id, OBSERVED.isoformat()))
        return account_id

    def candidate(self, db, account_id, provider_id="unlock-id"):
        return db.execute("""
            INSERT INTO people (account_id, name, origin, apollo_person_id)
            VALUES (?, 'Sam Example', 'apollo', ?)
        """, (account_id, provider_id)).lastrowid

    def login(self, name):
        client = test_r108_credit_reporting.CreditReportingTest.login(self, name)
        self.assertEqual(client.get("/today").status_code, 200)
        return client

    def submit(self, client, account_id, **fields):
        return self.post(client, f"/accounts/{account_id}/enrich", **fields)

    def post(self, client, path, **fields):
        with client.session_transaction() as session:
            token = session["csrf"]
        return client.post(path, data={"csrf_token": token, **fields})

    def test_search_completes_only_submitted_job_and_posts_no_credits(self):
        with closing(connect(self.database)) as db, transaction(db):
            store = SqlGrowthStore(db)
            actor = db.execute("SELECT * FROM users WHERE role = 'manager'").fetchone()
            unrelated_search = self.qualified(db, "other-search")
            unrelated_paid = self.qualified(db, "other-paid")
            for account_id, person_id in ((unrelated_search, None), (unrelated_paid, self.candidate(db, unrelated_paid))):
                account = store.account(account_id, OBSERVED.date().isoformat())
                self.assertTrue(request_enrichment(store, account, person_id, actor, observed=OBSERVED))
            target = self.qualified(db, "selected-search")
        seller = self.login("riya")
        self.assertTrue(b"Find contacts with Apollo" in seller.get(f"/accounts/{target}").data)
        self.assertEqual(self.submit(seller, target).status_code, 303)
        self.assertEqual(self.submit(seller, target).status_code, 303)
        self.provider.assert_called_once()
        self.assertEqual(self.provider.call_args.args[0]["account_id"], target)
        self.assertTrue(b"People search - Done" in seller.get(f"/accounts/{target}").data)
        with closing(connect(self.database)) as db:
            rows = db.execute("SELECT account_id, action, state FROM credit_jobs ORDER BY id").fetchall()
            self.assertEqual([tuple(row) for row in rows], [
                (unrelated_search, "people_search", "queued"), (unrelated_paid, "email_unlock", "queued"),
                (target, "people_search", "done"),
            ])
            self.assertEqual(db.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 0)

    def test_empty_association_search_can_be_repeated_once(self):
        with closing(connect(self.database)) as db, transaction(db):
            account_id = self.qualified(db, "association-search")
            db.execute("UPDATE accounts SET organization_type = 'Association' WHERE id = ?", (account_id,))
        self.provider.side_effect = [{"credits": 0}, self.provider.return_value]
        seller = self.login("riya")
        self.assertEqual(self.submit(self.login("admin"), account_id).status_code, 303)
        with closing(connect(self.database)) as db:
            previous = db.execute("SELECT id, error FROM credit_jobs WHERE account_id = ?", (account_id,)).fetchone()
        page = seller.get(f"/accounts/{account_id}").data.decode()
        self.assertIn("Search again with Apollo", page)
        self.assertRegex(page, rf'name="previous_search_id"\s+value="{previous["id"]}"')
        self.assertTrue({"Executive Director", "Director of Meetings"} <= set(self.provider.call_args.args[0]["titles"]))

        fields = {"previous_search_id": previous["id"]}
        self.assertEqual(self.submit(seller, account_id, **fields).status_code, 303)
        self.assertTrue(self.submit(seller, account_id, **fields).status_code in {303, 409})
        self.assertEqual(self.provider.call_count, 2)
        page = seller.get(f"/accounts/{account_id}").data.decode()
        self.assertIn("Sam Example", page)
        self.assertNotIn("Search again with Apollo", page)
        with closing(connect(self.database)) as db:
            jobs = db.execute("SELECT id, state, error FROM credit_jobs WHERE account_id = ? ORDER BY id", (account_id,)).fetchall()
            self.assertEqual(len(jobs), 2)
            self.assertEqual(tuple(jobs[0]), (previous["id"], "done", previous["error"]))
            self.assertEqual(jobs[1]["state"], "done")
            roles = db.execute("""
                SELECT u.role FROM credit_jobs j JOIN users u ON u.id = j.actor_id
                WHERE j.account_id = ? ORDER BY j.id
            """, (account_id,)).fetchall()
            self.assertEqual([row["role"] for row in roles], ["admin", "sdr"])
            self.assertEqual(db.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 0)

    def test_unknown_type_search_uses_multiple_event_leadership_titles(self):
        with closing(connect(self.database)) as db, transaction(db):
            account_id = self.qualified(db, "unknown-type-search")
        self.assertEqual(self.submit(self.login("riya"), account_id).status_code, 303)
        self.provider.assert_called_once()
        titles = set(self.provider.call_args.args[0]["titles"])
        self.assertTrue({"Head of Events", "Event Director"} <= titles)

    def test_stale_empty_result_cannot_repeat_newer_empty_search(self):
        with closing(connect(self.database)) as db, transaction(db):
            account_id = self.qualified(db, "repeated-empty-search")
        self.provider.return_value = {"credits": 0}
        seller = self.login("riya")
        self.assertEqual(self.submit(seller, account_id).status_code, 303)
        with closing(connect(self.database)) as db:
            first_id = db.execute("SELECT id FROM credit_jobs WHERE account_id = ?", (account_id,)).fetchone()[0]
        self.assertEqual(self.submit(seller, account_id, previous_search_id=first_id).status_code, 303)
        self.assertTrue(self.submit(seller, account_id, previous_search_id=first_id).status_code in {303, 409})
        self.assertEqual(self.provider.call_count, 2)
        with closing(connect(self.database)) as db:
            jobs = db.execute("SELECT id, state FROM credit_jobs WHERE account_id = ? ORDER BY id", (account_id,)).fetchall()
            self.assertEqual([row["state"] for row in jobs], ["done", "done"])
            self.assertEqual(db.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 0)
        page = seller.get(f"/accounts/{account_id}").data.decode()
        self.assertRegex(page, rf'name="previous_search_id"\s+value="{jobs[1]["id"]}"')

    def test_paid_unlock_requires_opt_in_and_posts_confirmed_charge_once(self):
        with closing(connect(self.database)) as db, transaction(db):
            account_id = self.qualified(db, "allowed-unlock")
            person_id = self.candidate(db, account_id)
        seller = self.login("riya")
        self.assertFalse(b"Unlock Sam Example" in seller.get(f"/accounts/{account_id}").data)
        self.assertEqual(self.submit(seller, account_id, person_id=person_id).status_code, 403)
        self.provider.assert_not_called()
        with closing(connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM credit_jobs").fetchone()[0], 0)

        self.app.config["APOLLO_ALLOW_PAID"] = True
        self.provider.return_value = {
            "credits": "0.375", "email": "sam@allowed-unlock.example", "email_verified": True,
        }
        self.assertTrue(b"Unlock Sam Example" in seller.get(f"/accounts/{account_id}").data)
        self.assertEqual(self.submit(seller, account_id, person_id=person_id).status_code, 303)
        self.assertEqual(self.submit(seller, account_id, person_id=person_id).status_code, 303)
        self.provider.assert_called_once()
        self.assertEqual(self.provider.call_args.args[0]["apollo_person_id"], "unlock-id")
        with closing(connect(self.database)) as db:
            postings = db.execute("SELECT account_id, person_id, source, credits FROM credit_ledger").fetchall()
            self.assertEqual(len(postings), 1)
            posting = postings[0]
            self.assertEqual((posting["account_id"], posting["person_id"], posting["source"]),
                             (account_id, person_id, "apollo"))
            self.assertEqual(Decimal(str(posting["credits"])), Decimal("0.375"))
            candidate = SqlGrowthStore(db).person(account_id, person_id)
            self.assertEqual(candidate["email"], "sam@allowed-unlock.example")
            self.assertTrue(candidate["email_verified"])

    def test_peer_account_and_suppression_block_provider_calls(self):
        with closing(connect(self.database)) as db, transaction(db):
            private_account = self.qualified(db, "private-company")
            suppressed = self.qualified(db, "suppressed-company")
            admin_id = db.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()[0]
            save_suppression_rule(db, "domain", "suppressed-company.example", "opt_out", admin_id, OBSERVED,
                                  source="manual", reason_note="")
        self.assertEqual(self.submit(self.login("jordan"), private_account).status_code, 403)
        self.assertEqual(self.submit(self.login("riya"), suppressed).status_code, 409)
        self.provider.assert_not_called()
        with closing(connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM credit_jobs").fetchone()[0], 0)

    def test_queued_search_is_resumed_only_by_the_requester(self):
        with closing(connect(self.database)) as db, transaction(db):
            account_id = self.qualified(db, "queued-company")
            store = SqlGrowthStore(db)
            actor = db.execute("SELECT * FROM users WHERE email = 'riya@example.test'").fetchone()
            account = store.account(account_id, OBSERVED.date().isoformat())
            self.assertTrue(request_enrichment(store, account, None, actor, observed=OBSERVED))
        self.assertEqual(self.submit(self.login("manager"), account_id).status_code, 303)
        self.provider.assert_not_called()
        self.assertEqual(self.submit(self.login("riya"), account_id).status_code, 303)
        self.provider.assert_called_once()
        with closing(connect(self.database)) as db:
            self.assertEqual([row["state"] for row in db.execute("SELECT state FROM credit_jobs")], ["done"])

    def test_missing_apollo_key_hides_search_and_prevents_queueing(self):
        with closing(connect(self.database)) as db, transaction(db):
            account_id = self.qualified(db, "missing-key")
        self.app.config["APOLLO_PROVIDER"] = None
        seller = self.login("riya")
        page = seller.get(f"/accounts/{account_id}").data
        self.assertTrue(b"Apollo needs to be configured for this CRM" in page)
        self.assertFalse(b"Find contacts with Apollo" in page)
        self.assertEqual(self.submit(seller, account_id).status_code, 503)
        with closing(connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM credit_jobs").fetchone()[0], 0)

    def test_operations_retry_dispatches_the_failed_free_job_only(self):
        with closing(connect(self.database)) as db, transaction(db):
            store = SqlGrowthStore(db)
            actor = db.execute("SELECT * FROM users WHERE role = 'manager'").fetchone()
            untouched = self.qualified(db, "old-paid-retry")
            person_id = self.candidate(db, untouched)
            account = store.account(untouched, OBSERVED.date().isoformat())
            self.assertTrue(request_enrichment(store, account, person_id, actor, observed=OBSERVED))
            account_id = self.qualified(db, "failed-free-retry")
        self.provider.side_effect = [TimeoutError, self.provider.return_value]
        self.assertEqual(self.submit(self.login("riya"), account_id).status_code, 303)
        with closing(connect(self.database)) as db:
            failed = db.execute("SELECT id, state FROM credit_jobs WHERE account_id = ?", (account_id,)).fetchone()
            self.assertEqual(failed["state"], "failed")
        admin = self.login("admin")
        retry_path = f"/operations/credits/{failed['id']}/retry"
        self.assertTrue(retry_path.encode() in admin.get("/operations").data)
        self.assertEqual(self.post(admin, retry_path).status_code, 303)
        self.assertEqual(self.provider.call_count, 2)
        self.assertEqual([call.args[0]["id"] for call in self.provider.call_args_list], [failed["id"]] * 2)
        with closing(connect(self.database)) as db:
            rows = db.execute("SELECT account_id, state FROM credit_jobs ORDER BY id").fetchall()
            self.assertEqual([tuple(row) for row in rows], [(untouched, "queued"), (account_id, "done")])
            self.assertEqual(db.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 0)

    def test_uncertain_unlock_cannot_send_again_on_repeat(self):
        with closing(connect(self.database)) as db, transaction(db):
            account_id = self.qualified(db, "uncertain-unlock")
            person_id = self.candidate(db, account_id)
        self.app.config["APOLLO_ALLOW_PAID"] = True
        self.provider.side_effect = TimeoutError
        seller = self.login("riya")
        self.assertEqual(self.submit(seller, account_id, person_id=person_id).status_code, 303)
        self.assertEqual(self.submit(seller, account_id, person_id=person_id).status_code, 303)
        self.provider.assert_called_once()
        with closing(connect(self.database)) as db:
            jobs = db.execute("SELECT state, reconciliation_required FROM credit_jobs").fetchall()
            self.assertEqual([tuple(row) for row in jobs], [("failed", 1)])
            self.assertEqual(db.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 0)


@unittest.skipUnless(TEST_URL, "Set CRM_TEST_POSTGRES_URL for disposable PostgreSQL tests.")
class PostgresApolloUiTest(PostgresDatabase, ApolloUiTest):
    pass
