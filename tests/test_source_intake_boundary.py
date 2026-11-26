import json
import unittest

from crm_core.source_intake import stage_record


class IntakeStoreDouble:
    def __init__(self):
        self.rows = {}

    def record(self, run_id, identity):
        return self.rows.get((run_id, identity))

    def records_for_site(self, run_id, site_identity):
        return [row for (source_run, _), row in self.rows.items()
                if run_id == source_run and row["site_identity"] == site_identity]

    def set_record_identity(self, run_id, prior_identity, identity, record):
        row = self.rows.pop((run_id, prior_identity))
        self.rows[(run_id, identity)] = dict(row, identity=identity, payload_json=json.dumps(record))

    def write_record(self, run_id, prior_identity, identity, site_identity, batch_number, record_number, record):
        if prior_identity:
            self.rows.pop((run_id, prior_identity))
        self.rows[(run_id, identity)] = dict(identity=identity, site_identity=site_identity,
                                            batch_number=batch_number, payload_json=json.dumps(record))


class SourceIntakeBoundaryTest(unittest.TestCase):
    def test_hb03_out_of_order_identity_upgrade_preserves_newest_evidence(self):
        store = IntakeStoreDouble()
        old = {"name": "Fixture Association", "website": "fixture.example", "source_id": "stable-fixture-id"}
        latest = {"name": "Fixture Association", "website": "fixture.example", "contact_role": "Newer role"}
        self.assertTrue(stage_record(store, 1, latest, 2, 1))
        self.assertFalse(stage_record(store, 1, old, 1, 1))
        self.assertEqual(json.loads(store.rows[(1, "id:stable-fixture-id")]["payload_json"]),
                         dict(latest, source_id="stable-fixture-id"))
        self.assertTrue(stage_record(store, 1, dict(latest, contact_role="Latest role"), 3, 1))
        self.assertEqual(len(store.rows), 1)
        self.assertEqual(json.loads(store.rows[(1, "id:stable-fixture-id")]["payload_json"])["source_id"],
                         "stable-fixture-id")
