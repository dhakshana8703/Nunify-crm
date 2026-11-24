import re
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app import create_app
from crm_store import connect
from workflow_store import DEMO_PASSWORD


class BulkAssignmentTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1] / "instance" / f"bulk-assignment-test-{uuid4().hex}"
        self.folder.mkdir(parents=True)
        self.addCleanup(self.remove_files)
        self.database = self.folder / "test.sqlite3"
        self.app = create_app(self.database, demo=True)
        self.app.config.update(TESTING=True, CRM_CLOCK=lambda: datetime(2026, 11, 20, 12, tzinfo=timezone.utc))
        with closing(connect(self.database)) as connection, connection:
            self.users = dict(connection.execute("SELECT email, id FROM users"))
            self.both = self.add_account(connection, "Alpine Society", ("cvent", "directory"))
            self.directory = self.add_account(connection, "Boreal Institute", ("directory",))
            self.cvent = self.add_account(connection, "Coastal Events", ("cvent",), organization_type="Corporate")
            self.other = self.add_account(connection, "Desert Network", ())
            connection.execute("""
                INSERT INTO imported_source_evidence
                    (source_slug, external_id, account_id, source_url, observed_at, payload_json)
                VALUES ('upload', 'other-reference', ?, '', '2026-11-20', '{}')
            """, (self.other,))
            connection.execute("""
                INSERT INTO ingest_sources(slug, name, adapter) VALUES ('partner', 'Partner Directory', 'external')
            """)
            connection.execute("""
                INSERT INTO imported_source_evidence
                    (source_slug, external_id, account_id, source_url, observed_at, payload_json)
                VALUES ('partner', 'partner-reference', ?, '', '2026-11-20', '{}')
            """, (self.other,))
            connection.execute("""
                INSERT INTO qualifications(account_id, score, signals, observed_at)
                VALUES (?, 72, '[]', '2026-11-20')
            """, (self.directory,))
            for index in range(25):
                self.add_account(connection, f"Extra Organization {index:02}", ("directory",))

    def remove_files(self):
        for path in self.folder.iterdir():
            path.unlink()
        self.folder.rmdir()

    def add_account(self, connection, name, sources, *, organization_type="Association"):
        identity = name.lower().replace(" ", "-")
        account_id = connection.execute("""
            INSERT INTO accounts(identity_key, name, domain, organization_type)
            VALUES (?, ?, ?, ?)
        """, (identity, name, f"{identity}.example", organization_type)).lastrowid
        for source in sources:
            connection.execute("""
                INSERT INTO source_records(source, source_id, source_url, source_file, account_id, details_json, raw_json)
                VALUES (?, ?, '', 'test', ?, '{}', '{}')
            """, (source, identity, account_id))
        return account_id

    def sign_in(self, name):
        client = self.app.test_client()
        token = self.csrf(client.get("/login"))
        response = client.post("/login", data={
            "email": f"{name}@example.test", "password": DEMO_PASSWORD, "csrf_token": token,
        })
        self.assertEqual(response.status_code, 303)
        return client

    def csrf(self, response):
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))[1]

    def selections(self, response, field="account"):
        self.assertEqual(response.status_code, 200)
        return re.findall(rf'name="{field}" value="([0-9]+:[0-9]+)"', response.get_data(as_text=True))

    def owner(self, account_id):
        with closing(connect(self.database, read_only=True)) as connection:
            row = connection.execute("SELECT owner_id FROM account_work WHERE account_id = ?", (account_id,)).fetchone()
            return row["owner_id"] if row else None

    def test_source_and_score_filters_with_page_scoped_select_all(self):
        manager = self.sign_in("manager")
        riya = self.users["riya@example.test"]
        jordan = self.users["jordan@example.test"]
        for source, expected in (
            ("both", [self.both]), ("multi", [self.both, self.other]),
            ("cvent", [self.both, self.cvent]), ("other", [self.other]),
        ):
            self.assertEqual(self.selections(manager.get(f"/assignment?source={source}")), [
                f"{account_id}:0" for account_id in expected
            ])
        self.assertIn(b"Partner Directory", manager.get("/assignment?source=other").data)

        route = "/assignment?source=directory&sort=fit_score"
        page = manager.get(route)
        self.assertEqual(self.selections(page)[0], f"{self.directory}:0")
        self.assertIn(b"Fit score</dt><dd>72", page.data)
        self.assertEqual(manager.post(f"/assignment/{self.directory}", data={
            "csrf_token": self.csrf(page), "owner_id": riya, "revision": 0,
        }).status_code, 303)

        first_page = manager.get(route)
        second_page = manager.get(route + "&page=2")
        page_tokens = self.selections(first_page, "unassigned_on_page")
        remaining = self.selections(second_page)
        self.assertEqual((len(page_tokens), len(remaining)), (24, 2))
        self.assertNotIn(f"{self.directory}:1", page_tokens)
        result = manager.post("/assignment/bulk?source=directory&sort=fit_score", data={
            "csrf_token": self.csrf(first_page), "owner_id": jordan, "assign_page": "yes",
            "unassigned_on_page": page_tokens,
        })
        self.assertEqual((result.status_code, result.location), (303, route))
        self.assertTrue(all(self.owner(int(token.split(":")[0])) == jordan for token in page_tokens))
        self.assertTrue(all(self.owner(int(token.split(":")[0])) is None for token in remaining))
        self.assertEqual(self.owner(self.directory), riya)
        self.assertIsNone(self.owner(self.cvent))

    def test_company_picker_can_assign_a_company_from_another_page(self):
        manager = self.sign_in("manager")
        rep = self.sign_in("riya")
        with closing(connect(self.database, read_only=True)) as connection:
            company_id = connection.execute(
                "SELECT id FROM accounts WHERE name = ?", ("Extra Organization 24",)
            ).fetchone()["id"]

        first_page = manager.get("/assignment?source=directory")
        self.assertNotIn(f"{company_id}:0", self.selections(first_page))
        self.assertIn(b"Choose a company", first_page.data)
        self.assertNotIn(b'type="search" name="search"', first_page.data)
        self.assertNotIn(b"Clear search", first_page.data)
        company_picker = re.search(
            r'<select[^>]*name="company"[^>]*>(.*?)</select>',
            first_page.get_data(as_text=True), re.DOTALL,
        )
        self.assertIsNotNone(company_picker)
        self.assertIn('<option value="">All companies</option>', company_picker[1])
        self.assertIn(f'value="{company_id}"', company_picker[1])

        legacy_search = manager.get("/assignment?source=directory&search=Boreal")
        self.assertEqual(self.selections(legacy_search), [f"{self.directory}:0"])
        self.assertRegex(
            legacy_search.get_data(as_text=True),
            r'<button\b[^>]*\bform="assignment-filters"[^>]*>Clear search</button>',
        )
        empty_search = manager.get("/assignment?source=directory&search=missing-company")
        self.assertEqual(self.selections(empty_search), [])
        self.assertIn(b"Clear search", empty_search.data)

        route = f"/assignment?source=directory&company={company_id}"
        selected = manager.get(route + "&search=Alpine")
        self.assertEqual(self.selections(selected), [f"{company_id}:0"])
        selection = f'<option value="{company_id}" selected>'
        self.assertIn(selection, selected.get_data(as_text=True))
        self.assertNotIn(b"Clear search", selected.data)
        self.assertEqual(rep.get(route).status_code, 403)

        reset = manager.get("/assignment?source=directory&company=")
        self.assertEqual(self.selections(reset), self.selections(first_page))
        self.assertNotIn(selection, reset.get_data(as_text=True))

        excluded = manager.get(f"/assignment?source=cvent&company={company_id}")
        self.assertEqual(self.selections(excluded), [f"{self.both}:0", f"{self.cvent}:0"])
        self.assertNotIn(selection, excluded.get_data(as_text=True))

        self.assertEqual(manager.post(f"/assignment/{company_id}?source=directory&company={company_id}", data={
            "csrf_token": self.csrf(selected), "owner_id": self.users["riya@example.test"], "revision": 0,
        }).location, route)
        self.assertEqual(self.owner(company_id), self.users["riya@example.test"])
        self.assertIn(b"Extra Organization 24", rep.get("/my-accounts").data)

    def test_transfer_needs_confirmation_and_stale_bulk_save_is_atomic(self):
        manager = self.sign_in("manager")
        rep = self.sign_in("riya")
        riya = self.users["riya@example.test"]
        jordan = self.users["jordan@example.test"]
        token = self.csrf(manager.get("/assignment"))
        self.assertEqual(rep.post("/assignment/bulk", data={
            "csrf_token": self.csrf(rep.get("/today")), "owner_id": jordan, "account": f"{self.both}:0",
        }).status_code, 403)
        self.assertEqual(manager.post("/assignment/bulk", data={
            "owner_id": riya, "account": f"{self.both}:0",
        }).status_code, 400)
        self.assertEqual(manager.post(f"/assignment/{self.both}", data={
            "csrf_token": token, "owner_id": riya, "revision": 0,
        }).status_code, 303)
        with closing(connect(self.database)) as connection, connection:
            connection.executemany("""
                INSERT INTO tasks(account_id, owner_id, title, due_on, status)
                VALUES (?, ?, 'Follow up', '2026-11-22', ?)
            """, [(self.both, riya, "open"), (self.both, riya, "done")])
            connection.execute("""
                INSERT INTO coverages(account_id, owner_id, rep_id, starts_at, ends_at, created_by)
                VALUES (?, ?, ?, '2026-11-19T00:00:00Z', '2026-11-22T00:00:00Z', ?)
            """, (self.both, riya, jordan, self.users["manager@example.test"]))

        data = {"csrf_token": token, "owner_id": jordan, "account": [f"{self.directory}:0", f"{self.both}:1"]}
        self.assertEqual(manager.post("/assignment/bulk", data=data).status_code, 400)
        self.assertIsNone(self.owner(self.directory))
        stale = {**data, "allow_transfer": "yes", "account": [f"{self.directory}:0", f"{self.both}:0"]}
        self.assertEqual(manager.post("/assignment/bulk", data=stale).status_code, 409)
        self.assertIsNone(self.owner(self.directory))
        self.assertEqual(self.owner(self.both), riya)

        self.assertEqual(manager.post("/assignment/bulk", data={**data, "allow_transfer": "yes"}).status_code, 303)
        self.assertEqual((self.owner(self.directory), self.owner(self.both)), (jordan, jordan))
        with closing(connect(self.database, read_only=True)) as connection:
            task_owners = dict(connection.execute("SELECT status, owner_id FROM tasks WHERE account_id = ?", (self.both,)))
            self.assertEqual(task_owners, {"open": jordan, "done": riya})
            self.assertIsNotNone(connection.execute("SELECT cancelled_at FROM coverages").fetchone()[0])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_events WHERE action = 'assignment'").fetchone()[0], 3)

    def test_round_robin_preview_and_confirm_respect_total_owned_cap(self):
        manager = self.sign_in("manager")
        rep = self.sign_in("riya")
        riya = self.users["riya@example.test"]
        jordan = self.users["jordan@example.test"]
        listing = manager.get("/assignment?org_type=Corporate")
        self.assertEqual(self.selections(listing), [f"{self.cvent}:0"])
        self.assertIn(b'formaction="/assignment/round-robin?org_type=Corporate"', listing.data)
        token = self.csrf(listing)
        self.assertEqual(manager.post(f"/assignment/{self.other}", data={
            "csrf_token": token, "owner_id": riya, "revision": 0,
        }).status_code, 303)
        with closing(connect(self.database)) as connection, connection:
            connection.execute("""
                INSERT INTO account_work(account_id, stage) VALUES (?, 'qualified')
            """, (self.directory,))
            connection.executemany("""
                INSERT INTO activities(account_id, actor_id, channel, outcome, direction, occurred_at)
                VALUES (?, ?, 'call', 'no_answer', ?, ?)
            """, (
                (self.other, riya, "outbound", "2026-11-19T12:00:00Z"),
                (self.other, riya, "internal", "2026-11-19T12:00:00Z"),
                (self.other, riya, "outbound", "2026-11-12T12:00:00Z"),
            ))
        self.assertEqual(self.selections(manager.get("/assignment?filter=qualified_unassigned")), [f"{self.directory}:0"])
        path = "/assignment?source=directory&org_type=Association"
        listing = manager.get(path)
        self.assertIn(b"Outbound touches - 7 days</dt><dd>1", listing.data)
        selection = self.selections(listing)[:4]
        accounts = [int(value.split(":")[0]) for value in selection]
        self.assertEqual(manager.post("/assignment/round-robin", data={
            "csrf_token": token, "account": selection, "rep_id": riya, "cap": 0,
        }).status_code, 400)
        response = manager.post("/assignment/round-robin?source=directory&org_type=Association", data={
            "csrf_token": token, "account": selection, "rep_id": [riya, jordan], "cap": 2,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Preview round-robin allocation", response.data)
        self.assertIn(b"1 selected account will stay unassigned", response.data)
        allocation = re.search(rb'name="allocation" value="([^"]+)"', response.data)[1].decode()
        self.assertTrue(all(self.owner(account) is None for account in accounts))
        self.assertEqual(rep.post("/assignment/round-robin", data={
            "csrf_token": self.csrf(rep.get("/today")), "allocation": allocation,
        }).status_code, 403)
        confirm = "/assignment/round-robin?source=directory&org_type=Association"
        applied = manager.post(confirm, data={"csrf_token": token, "allocation": allocation})
        self.assertEqual((applied.status_code, applied.location), (303, path))
        self.assertEqual(tuple(self.owner(account) for account in accounts), (jordan, riya, jordan, None))
        with closing(connect(self.database, read_only=True)) as connection:
            self.assertEqual(connection.execute("""
                SELECT COUNT(*) FROM audit_events
                WHERE action = 'assignment' AND json_extract(details, '$.method') = 'round_robin'
            """).fetchone()[0], 3)

    def test_round_robin_rejects_a_stale_workload_or_another_managers_confirmation(self):
        manager = self.sign_in("manager")
        admin = self.sign_in("admin")
        token = self.csrf(manager.get("/assignment"))
        admin_token = self.csrf(admin.get("/assignment"))
        response = manager.post("/assignment/round-robin", data={
            "csrf_token": token,
            "account": [f"{self.both}:0", f"{self.directory}:0"],
            "rep_id": [self.users["riya@example.test"], self.users["jordan@example.test"]],
            "cap": 1,
        })
        self.assertEqual(response.status_code, 200)
        allocation = re.search(rb'name="allocation" value="([^"]+)"', response.data)[1].decode()
        self.assertEqual(admin.post("/assignment/round-robin", data={
            "csrf_token": admin_token, "allocation": allocation,
        }).status_code, 403)
        self.assertEqual(admin.post(f"/assignment/{self.other}", data={
            "csrf_token": admin_token, "owner_id": self.users["riya@example.test"], "revision": 0,
        }).status_code, 303)
        self.assertEqual(manager.post("/assignment/round-robin", data={
            "csrf_token": token, "allocation": allocation,
        }).status_code, 409)
        self.assertIsNone(self.owner(self.both))
        self.assertIsNone(self.owner(self.directory))


if __name__ == "__main__":
    unittest.main()
