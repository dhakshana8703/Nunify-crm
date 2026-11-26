import unittest
from dataclasses import replace
from datetime import datetime, timezone

from crm_core.activity import ActivityConflict, ActivitySubmission, OutreachBlocked, record_activity


class InMemoryActivities:
    def __init__(self):
        self.saved = []
        self.restriction = None

    def previous_activity(self, actor_id, key):
        return next(({"request_hash": item.request_hash} for item in self.saved
                     if item.submission.actor_id == actor_id and item.submission.key == key), None)

    def person_for(self, account_id, person_id):
        if (account_id, person_id) == (10, 5):
            return {"id": 5, "name": "Example Contact", "email": "person@example.test", "do_not_contact": 0}
        return None

    def restriction_for(self, account_id, person_id):
        return self.restriction

    def save(self, change):
        self.saved.append(change)


class ActivityCoreTest(unittest.TestCase):
    def setUp(self):
        self.account = {"id": 10, "owner_id": 7, "owner_timezone": "Asia/Kolkata", "stage": "assigned", "revision": 2}
        self.at = datetime(2026, 11, 26, 19, tzinfo=timezone.utc)
        self.store = InMemoryActivities()
        self.call = ActivitySubmission(4, "UTC", "call-1", 2, "call", "no_answer", person_key="5", due_tomorrow=True)

    def test_ef_01a_tomorrow_uses_owner_timezone_and_keeps_owner_under_coverage(self):
        self.assertTrue(record_activity(self.store, self.account, self.call, self.at))
        (change,) = self.store.saved
        self.assertEqual((change.direction, change.stage), ("outbound", "working"))
        self.assertEqual((change.task_owner_id, change.due_on, change.task_title),
                         (7, "2026-11-28", "Follow up with Example Contact"))
        self.assertFalse(record_activity(self.store, self.account, self.call, self.at))
        with self.assertRaises(ActivityConflict):
            record_activity(self.store, self.account, replace(self.call, note="Changed"), self.at)
        self.assertEqual(len(self.store.saved), 1)

    def test_ef_01a_blocks_outreach_and_can_record_the_opt_out_itself(self):
        self.store.restriction = "Opted out"
        with self.assertRaisesRegex(OutreachBlocked, "Opted out"):
            record_activity(self.store, self.account, self.call, self.at)
        self.assertEqual(self.store.saved, [])
        opt_out = replace(self.call, outcome="opted_out", due_tomorrow=False)
        self.assertTrue(record_activity(self.store, self.account, opt_out, self.at))
        (change,) = self.store.saved
        self.assertEqual((change.direction, change.person_status, change.opt_out_email),
                         ("inbound", "opted_out", "person@example.test"))
        self.assertTrue(change.do_not_contact)
        self.assertIsNone(change.due_on)
