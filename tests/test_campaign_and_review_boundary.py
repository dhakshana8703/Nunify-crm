import unittest

from crm_core.campaign import eligible, plan_sequence_event
from crm_core.source_review import ReviewError, reviewed_link


class CampaignAndReviewBoundaryTest(unittest.TestCase):
    def test_hb03_campaign_admission_and_opt_out_are_core_decisions(self):
        person = dict(unlocked=True, email_verified=True, email="fixture@example.test")
        self.assertTrue(eligible(person, {"stage": "working"}, blocked=False))
        self.assertFalse(eligible(person, {"stage": "handed_off"}, blocked=False))
        self.assertFalse(eligible(person, {"stage": "working"}, blocked=True))
        self.assertEqual(
            plan_sequence_event("unsubscribed", "replied", "working", False),
            ("opted_out", "inbound", True, "opted_out", None, "opt_out"),
        )
        self.assertIsNone(plan_sequence_event("bounced", "opted_out", "working", True)[3])
        self.assertEqual(
            plan_sequence_event("replied", "handed_off", "handed_off", False),
            ("replied", "inbound", True, None, None, None),
        )

    def test_hb03_review_requires_proof_and_keeps_original_evidence(self):
        row = {"source_slug": "directory"}
        payload = {"details": {"name": "National Fixture Association", "host": "fixture.example"},
                   "raw": {"name": "National Fixture Association"}}
        account = {"name": "National Fixture Association", "domain": "fixture.example"}
        with self.assertRaises(ReviewError):
            reviewed_link(row, payload, account, "https://unrelated.example", "", blocked=False, linked=False)
        with self.assertRaises(ReviewError):
            reviewed_link(row, payload, account, "https://fixture.example", "", blocked=True, linked=False)
        with self.assertRaises(ReviewError):
            reviewed_link(row, payload, account, "https://fixture.example", "", blocked=False, linked=True)
        details = reviewed_link(row, payload, account, "https://fixture.example", "", blocked=False, linked=False)
        self.assertEqual(details["identity_review_url"], "https://fixture.example")
        self.assertEqual(payload["details"], {"name": "National Fixture Association", "host": "fixture.example"})
        self.assertEqual(payload["raw"], {"name": "National Fixture Association"})
