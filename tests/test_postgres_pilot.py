"""Opt-in role and durability smoke test for a disposable PostgreSQL database."""

import os
import unittest
from contextlib import closing
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import test_admin_sdr_flow
from app import create_app
from crm_store import is_postgres


TEST_URL = os.environ.get("CRM_TEST_POSTGRES_URL")


@unittest.skipUnless(TEST_URL, "Set CRM_TEST_POSTGRES_URL to run the PostgreSQL rehearsal.")
class PostgresPilotTest(test_admin_sdr_flow.AdminSDRFlowTest):
    def database_target(self):
        import psycopg2
        from psycopg2 import sql

        if not is_postgres(TEST_URL):
            self.fail("CRM_TEST_POSTGRES_URL must point to a disposable PostgreSQL database.")
        schema = f"nunify_test_{uuid4().hex}"
        with closing(psycopg2.connect(TEST_URL)) as conn, conn, conn.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public")
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        self.addCleanup(self.drop_test_schema, schema)
        url = urlsplit(TEST_URL)
        query = dict(parse_qsl(url.query))
        options = query.get("options", "")
        query["options"] = f"{options} -csearch_path={schema},public".strip()
        return urlunsplit(url._replace(query=urlencode(query)))

    def drop_test_schema(self, schema):
        import psycopg2
        from psycopg2 import sql

        with closing(psycopg2.connect(TEST_URL)) as conn, conn, conn.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    def check_integrity(self, db):
        constraints = db.execute("""
            SELECT c.convalidated
            FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE n.nspname = current_schema() AND c.contype = 'f'
        """).fetchall()
        self.assertTrue(constraints)
        self.assertTrue(all(row[0] for row in constraints))

    def test_saved_source_replay_preserves_assignment_and_follow_up(self):
        super().test_saved_source_replay_preserves_assignment_and_follow_up()
        self.app = create_app(self.database)
        self.app.config.update(TESTING=True, CRM_CLOCK=lambda: self.clock)
        rep = self.login("riya", "/today")
        self.assertIn(b"Example Trade Association", rep.get("/my-accounts").data)
        self.assertEqual(rep.get("/today").data.count(b"Call after the holiday"), 1)
