import json
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from app import create_app
from crm_store import connect
from workflow_store import DEMO_PASSWORD


class AccountProfileTest(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1] / "instance" / f"account-profile-test-{uuid4().hex}"
        folder.mkdir(parents=True)
        self.database = folder / "test.sqlite3"
        self.addCleanup(folder.rmdir)
        self.addCleanup(self.database.unlink, missing_ok=True)
        self.app = create_app(self.database, demo=True)
        self.app.config["TESTING"] = True
        with closing(connect(self.database)) as connection, connection:
            self.account_id = connection.execute("""
                INSERT INTO accounts(identity_key, name, domain, organization_type)
                VALUES ('summit', 'Summit College', 'summit.example', 'Unknown')
            """).lastrowid
            for source, source_id, evidence in (
                ("cvent", "cvent-id", {"event_name": "Conference", "evidence_keys": []}),
                ("directory", "directory-id", {"id": "directory-id", "name": "Summit College",
                                               "country": "India", "members": "2400"}),
            ):
                connection.execute("""
                    INSERT INTO source_records
                        (source, source_id, source_url, source_file, account_id, details_json, raw_json)
                    VALUES (?, ?, '', 'test', ?, ?, '{}')
                """, (source, source_id, self.account_id, json.dumps(evidence)))

    def csrf(self, client):
        with client.session_transaction() as data:
            return data["csrf"]

    def sign_in(self, role):
        client = self.app.test_client()
        self.assertEqual(client.get("/login").status_code, 200)
        response = client.post("/login", data={
            "email": f"{role}@example.test", "password": DEMO_PASSWORD, "csrf_token": self.csrf(client),
        })
        self.assertEqual(response.status_code, 303)
        return client

    def test_source_facts_and_admin_type_update_are_role_scoped(self):
        path = f"/accounts/{self.account_id}"
        manager, admin = self.sign_in("manager"), self.sign_in("admin")
        page = manager.get(path)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Confirmed by event evidence", page.data)
        self.assertIn(b"Organization country</dt><dd>India", page.data)
        self.assertIn(b"Directory members</dt><dd>2400", page.data)
        self.assertNotIn(b"Edit organization type", page.data)
        route = path + "/organization-type"
        data = {"organization_type": "University", "previous_type": "Unknown"}
        self.assertEqual(manager.post(route, data=dict(data, csrf_token=self.csrf(manager))).status_code, 403)
        self.assertEqual(admin.get(path).status_code, 200)
        data["csrf_token"] = self.csrf(admin)
        self.assertEqual(admin.post(route, data=data).status_code, 303)
        self.assertEqual(admin.post(route, data=data).status_code, 409)
        with closing(connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT organization_type FROM accounts").fetchone()[0], "University")
            entry = connection.execute("SELECT details FROM audit_events WHERE action = 'organization_type_changed'").fetchone()
            self.assertEqual(json.loads(entry["details"]), {"previous": "Unknown", "type": "University"})


if __name__ == "__main__":
    unittest.main()
