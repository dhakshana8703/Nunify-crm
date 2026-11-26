import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

from crm_core.growth import fit_satisfies, queue_enrichment
from crm_core.handoffs import schedule_coverage, deliver_pending_handoff


AT = datetime(2026, 11, 20, 18, 29, tzinfo=timezone.utc)


class GrowthPort:
    def __init__(self):
        self.reservations = []

    def restriction_for(self, account_id, person_id=None, *, candidate_email=None):
        return None

    def rules(self):
        return {"qualified_at": 40, "auto_at": 60}

    def qualification(self, account_id):
        return {"score": 80, "override": None}

    def has_credit_job(self, account_id, action, person_id):
        return bool(self.reservations)

    def daily_quota(self, user_id):
        return 1

    def credit_usage(self, user_id, day):
        return 0

    def reserve_credit_job(self, job):
        self.reservations.append(job)


class HandoffPort:
    def __init__(self):
        self.calls = []

    def atomic(self):
        return nullcontext()

    def active_user(self, user_id, role):
        return user_id == 8 and role == "sdr"

    def coverage_overlaps(self, account_id, owner_id, starts_at, ends_at):
        return False

    def add_coverage(self, **details):
        self.calls.append(details)
        return 12

    def audit(self, *_args):
        pass

    def pending_handoff(self, at):
        return {"id": 5, "account_id": 1, "person_id": 2, "idempotency_key": "stable", "payload": {}}

    def blocked(self, account_id, person_id):
        return True

    def fail_before_delivery(self, handoff_id, reason):
        self.calls.append((handoff_id, reason))


class GrowthHandoffCoreBoundaryTest(unittest.TestCase):
    def test_fit_and_credit_reservation_are_decided_without_sql_or_flask(self):
        store = GrowthPort()
        user = {"id": 3, "role": "sdr", "timezone": "Asia/Kolkata"}
        self.assertFalse(fit_satisfies({"score": 100, "override": 0}, {"qualified_at": 40}))
        self.assertTrue(queue_enrichment(store, {"id": 1}, None, user, observed=AT))
        self.assertFalse(queue_enrichment(store, {"id": 1}, None, user, observed=AT))
        job, = store.reservations
        self.assertEqual((job["on_day"], job["budget_day"], job["action"]),
                         ("2026-11-20", "2026-11-20", "people_search"))

    def test_coverage_and_delivery_policy_run_without_sql_or_flask(self):
        store = HandoffPort()
        self.assertEqual(schedule_coverage(
            store, {"id": 1, "owner_id": 3, "revision": 4}, 4, 8,
            "2026-11-20T18:00:00.000000+00:00", "2026-11-20T19:00:00.000000+00:00", 7, observed=AT,
        ), 12)
        self.assertEqual(store.calls[0]["rep_id"], 8)
        self.assertEqual(deliver_pending_handoff(store, None, clock=lambda: AT), {"id": 5, "status": "failed"})
        self.assertIn("restricted", store.calls[-1][1])

    def test_qualification_target_selection_is_a_core_decision(self):
        from crm_core.growth import qualification_target

        candidate = dict(has_cvent=True, has_directory=False, owner_id=None, stage="discovered",
                         score=80, override=None)
        self.assertTrue(qualification_target(candidate, "cvent", {"qualified_at": 40}))
        self.assertFalse(qualification_target(candidate, "both", {"qualified_at": 40}))
        self.assertFalse(qualification_target(dict(candidate, owner_id=7), "cvent", {"qualified_at": 40}))
        self.assertTrue(qualification_target(dict(candidate, owner_id=7, score=None), "cvent", {"qualified_at": 40}))
        self.assertFalse(qualification_target(dict(candidate, stage="qualified"), "cvent", {"qualified_at": 40}))

    def test_worker_wires_growth_and_handoffs_without_their_flask_adapters(self):
        process = subprocess.run([
            sys.executable, "-B", "-c",
            "import sys; sys.modules['growth_ops'] = None; sys.modules['sales_extensions'] = None; import worker",
        ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
