import unittest
from datetime import datetime, timezone

from crm_core.assignment import AssignmentBlocked, StaleAssignment, assign_account, require_revision


class InMemoryAssignments:
    def __init__(self, restriction=None):
        self.restriction = restriction
        self.changes = []

    def restriction_for(self, account_id):
        return self.restriction

    def save(self, change):
        self.changes.append(change)


class AssignmentCoreTest(unittest.TestCase):
    def setUp(self):
        self.account = {"id": 1, "owner_id": None, "stage": "discovered", "revision": 0}
        self.at = datetime(2026, 11, 26, 10, tzinfo=timezone.utc)

    def test_ef_01_plan_the_assignment_without_flask_or_a_database(self):
        store = InMemoryAssignments()
        self.assertTrue(assign_account(store, self.account, 4, 8, self.at, method="round_robin"))
        (change,) = store.changes
        self.assertEqual((change.account_id, change.previous_owner_id, change.owner_id), (1, None, 8))
        self.assertEqual((change.actor_id, change.stage, change.revision), (4, "assigned", 1))
        self.assertEqual((change.changed_at, change.method), (self.at, "round_robin"))

    def test_ef_01_transfer_keeps_stage_and_repeating_the_same_owner_is_quiet(self):
        store = InMemoryAssignments()
        account = dict(self.account, owner_id=7, stage="qualified", revision=3)
        self.assertFalse(assign_account(store, account, 4, 7, self.at))
        self.assertEqual(store.changes, [])
        self.assertTrue(assign_account(store, account, 4, 8, self.at))
        (change,) = store.changes
        self.assertEqual((change.previous_owner_id, change.stage, change.revision), (7, "qualified", 4))

    def test_ef_01_restriction_and_stale_revision_stop_assignment(self):
        store = InMemoryAssignments(restriction="Competitor")
        with self.assertRaisesRegex(AssignmentBlocked, "Competitor"):
            assign_account(store, self.account, 4, 8, self.at)
        self.assertEqual(store.changes, [])
        require_revision(self.account, 0)
        with self.assertRaises(StaleAssignment):
            require_revision(self.account, 1)
