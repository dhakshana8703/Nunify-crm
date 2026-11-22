import json
import re
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app import create_app
from crm_store import connect
from source_ops import work_one
from workflow_store import DEMO_PASSWORD


class ScraperIngestTest(unittest.TestCase):
    def setUp(self):
        instance = Path(__file__).resolve().parents[1] / "instance"
        self.folder = instance / f"scraper-intake-test-{uuid4().hex}"
        self.folder.mkdir(parents=True)
        self.addCleanup(self.clean)
        self.database = self.folder / "test.sqlite3"
        self.app = create_app(self.database, demo=True)
        self.app.testing = True
        self.admin = self.app.test_client()
        self.login(self.admin, "admin")
        self.key = self.create_key("partner")
        self.machine = self.app.test_client()

    def clean(self):
        for path in self.folder.iterdir():
            path.unlink()
        self.folder.rmdir()

    def token(self, client):
        with client.session_transaction() as session:
            return session["csrf"]

    def login(self, client, role):
        self.assertEqual(client.get("/login").status_code, 200)
        response = client.post("/login", data={
            "email": f"{role}@example.test", "password": DEMO_PASSWORD, "csrf_token": self.token(client),
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)

    def create_key(self, slug):
        response = self.admin.post("/sources/keys", data={
            "slug": slug, "name": "Partner workspace", "csrf_token": self.token(self.admin),
        })
        self.assertEqual(response.status_code, 200)
        return re.search(rb"<code>([A-Za-z0-9_-]{43})</code>", response.data)[1].decode()

    def api(self, route="", *, body=None, method="POST", slug="partner", key=None):
        return self.machine.open(f"/api/ingest/{slug}/runs{route}", method=method, json=body,
                                 headers={"Authorization": f"Bearer {key or self.key}"})

    def open_run(self, key):
        response = self.api(body={"idempotency_key": key, "input_name": "Saved run"})
        self.assertEqual(response.status_code, 201)
        return "/" + str(response.json["run_id"])

    def test_out_of_order_batches_keep_latest_data_and_open_default_is_preview(self):
        other_key = self.create_key("unrelated")
        unauthenticated = self.machine.post("/api/ingest/partner/runs", json={"idempotency_key": "anonymous"})
        self.assertEqual(unauthenticated.status_code, 401)
        run, replay = self.open_run("run-1"), self.api(body={"idempotency_key": "run-1", "input_name": "Saved run"})
        self.assertEqual((replay.status_code, replay.json["state"]), (200, "open"))
        foreign = self.api(run, key=other_key, slug="unrelated", method="GET")
        self.assertEqual(foreign.status_code, 404)
        numbered = {"idempotency_key": "latest", "batch_number": 2, "records": [
            {"name": "Open Trade Association", "domain": "www.trade.test", "category": "Latest"},
            {"name": "Broken Record", "domain": "local"},
            {"name": "Cvent Suppression Example", "domain": "cvent.com"},
        ]}
        batch = self.api(run + "/records", body=numbered)
        self.assertEqual((batch.status_code, batch.json["accepted"]), (201, 2))
        self.assertEqual([item["record"] for item in batch.json["errors"]], [2])
        incomplete = self.api(run + "/complete", body={"expected_batches": 2})
        self.assertEqual((incomplete.status_code, incomplete.json["state"]), (409, "open"))
        earlier = {"idempotency_key": "earlier", "batch_number": 1, "records": [
            {"source_id": "org-42", "name": "Open Trade Association", "website": "https://trade.test",
             "category": "Older"},
        ]}
        outdated = self.api(run + "/records", body=earlier)
        self.assertEqual((outdated.status_code, outdated.json["superseded"]), (201, 1))
        changed = self.api(run + "/records", body=dict(numbered, records=[{}]))
        self.assertEqual(changed.status_code, 409)
        completed = self.api(run + "/complete", body={"expected_batches": 2})
        self.assertEqual((completed.status_code, completed.json["state"], completed.json["staged_records"]),
                         (202, "ready", 2))
        source_id = completed.json["source_run_id"]
        self.assertEqual(self.api(run + "/records", body=numbered).json, batch.json)
        self.assertEqual(self.api(run + "/complete").json, completed.json)
        self.assertEqual(self.api(run + "/records", body=dict(numbered, idempotency_key="late")).status_code, 409)
        preview = self.admin.get(f"/sources/runs/{source_id}")
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"Open Trade Association", preview.data)
        with closing(connect(self.database)) as connection:
            self.assertIsNone(work_one(connection))
            payloads = [json.loads(row["payload_json"]) for row in connection.execute(
                "SELECT payload_json FROM source_run_rows WHERE run_id = ?", (source_id,),
            )]
            linked = next(row for row in payloads if row.get("source_id") == "org-42")
            self.assertEqual(linked["category"], "Latest")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
        approved = self.admin.post(f"/sources/runs/{source_id}/queue", data={"csrf_token": self.token(self.admin)})
        self.assertEqual(approved.status_code, 303)
        with closing(connect(self.database)) as connection:
            self.assertEqual(work_one(connection), source_id)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)
            self.assertEqual(set(row["result"] for row in connection.execute(
                "SELECT result FROM source_run_rows WHERE run_id = ?", (source_id,),
            )), {"imported", "suppressed"})

    def test_auto_mode_is_admin_only_and_locks_out_overlapping_promotion(self):
        path = "/sources/partner/promotion"
        manager = self.app.test_client()
        self.login(manager, "manager")
        denied = manager.post(path, data={"promotion": "automatic", "csrf_token": self.token(manager)})
        self.assertEqual(denied.status_code, 403)
        old = self.open_run("open-before-change")
        settings = self.admin.post(path, data={"promotion": "automatic", "csrf_token": self.token(self.admin)})
        self.assertEqual(settings.status_code, 303)
        self.assertEqual(self.api(old, method="GET").json["promotion"], "preview")
        records = [{"source_id": str(index), "name": f"Partner Association {index}",
                    "domain": f"association-{index}.test"} for index in range(50)]
        for key in ("first-auto", "second-auto"):
            run = self.open_run(key)
            batch = self.api(run + "/records", body={
                "idempotency_key": "body", "records": records,
            })
            self.assertEqual(batch.status_code, 201)
            response = self.api(run + "/complete", body={"expected_batches": 1})
            if key == "first-auto":
                self.assertEqual((response.status_code, response.json["state"]), (202, "queued"))
                with closing(connect(self.database)) as connection:
                    first = connection.execute("SELECT id FROM source_runs").fetchone()[0]
            else:
                self.assertEqual((response.status_code, response.json["state"]), (409, "open"))
                with closing(connect(self.database)) as connection:
                    self.assertEqual(work_one(connection), first)
                retry = self.api(run + "/complete", body={"expected_batches": 1})
                self.assertEqual((retry.status_code, retry.json["state"]), (202, "queued"))
                second = retry.json["source_run_id"]
                with closing(connect(self.database)) as connection:
                    self.assertEqual(work_one(connection), second)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 50)
                    self.assertEqual(connection.execute("""
                        SELECT COUNT(*) FROM source_run_rows WHERE run_id = ? AND result = 'unchanged'
                    """, (second,)).fetchone()[0], 50)

    def test_rate_limit_is_per_key_and_lifecycle_requests_are_logged(self):
        other = self.create_key("other-source")
        with patch("scraper_ingest.REQUESTS_PER_MINUTE", 2):
            run = self.open_run("rate-check")
            self.assertEqual(self.api(run, method="GET").status_code, 200)
            limited = self.api(run, method="GET")
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.headers["Retry-After"], "60")
            allowed = self.api(body={"idempotency_key": "other-run"}, slug="other-source", key=other)
            self.assertEqual(allowed.status_code, 201)
            refused = self.api(run, method="GET", key="bad key")
            self.assertEqual(refused.status_code, 401)
        with closing(connect(self.database)) as connection:
            rows = connection.execute("SELECT status_code, credential_hash FROM scraper_api_requests ORDER BY id").fetchall()
            self.assertEqual([row["status_code"] for row in rows], [201, 200, 429, 201, 401])
            self.assertIsNone(rows[-1]["credential_hash"])
            self.assertTrue(all(self.key != row["credential_hash"] for row in rows))
