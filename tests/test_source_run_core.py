import unittest

from crm_core import source_runs as source_policy

from crm_core.source_runs import SourceRunConflict, queue_run_again, submit_legacy_batch


class RunMemory:
    def __init__(self):
        self.key_current = True
        self.existing = None
        self.active_id = None
        self.saved = []

    def key_is_current(self, source, key_hash):
        return self.key_current

    def prior_request(self, source, key):
        return self.existing

    def active(self, source):
        return {"id": self.active_id} if self.active_id else None

    def stage(self, source, input_name, trigger, state="queued", **options):
        self.saved.append((source, trigger, options))
        return 11

    def queue_again(self, run_id, promote, observed):
        self.saved.append((run_id, promote))

    def record(self, actor_id, action, details):
        self.saved.append((actor_id, action, details))


class SourceRunCoreTest(unittest.TestCase):
    def test_hb03_source_access_and_detachment_are_core_decisions(self):
        self.assertIsNone(source_policy.require_source_admin({"role": "admin"}))
        with self.assertRaises(PermissionError):
            source_policy.require_source_admin({"role": "manager"})
        with self.assertRaises(LookupError):
            source_policy.saved_connector("unknown", {"cvent"}, {})
        with self.assertRaisesRegex(source_policy.SourceRunConflict, "disconnected"):
            source_policy.saved_connector("cvent", {"cvent"}, {})
        self.assertEqual(source_policy.saved_connector("cvent", {"cvent"}, {"cvent": "reader"}), "reader")

    def test_hb03_legacy_batch_is_replay_safe_and_stops_a_revoked_key(self):
        store = RunMemory()
        call = lambda digest: submit_legacy_batch(store, "events", "token", "request", digest, [{"name": "Group"}])
        self.assertEqual(call("saved"), (11, "queued", True))
        store.existing = {"id": 11, "state": "complete", "request_hash": "saved"}
        self.assertEqual(call("saved"), (11, "complete", False))
        with self.assertRaises(SourceRunConflict):
            call("changed")
        store.key_current = False
        with self.assertRaises(PermissionError):
            call("saved")
        self.assertEqual(len(store.saved), 1)

    def test_hb03_disconnected_run_cannot_promote_and_retry_preserves_phase(self):
        store = RunMemory()
        run = {"id": 12, "source_slug": "cvent", "adapter": "cvent", "state": "ready"}
        with self.assertRaises(SourceRunConflict):
            queue_run_again(store, run, set(), 2, None)
        self.assertEqual(store.saved, [])
        queue_run_again(store, run, {"cvent"}, 2, None)
        self.assertEqual(store.saved[0], (12, True))
        queue_run_again(store, dict(run, state="failed"), {"cvent"}, 2, None)
        self.assertEqual(store.saved[2], (12, False))
