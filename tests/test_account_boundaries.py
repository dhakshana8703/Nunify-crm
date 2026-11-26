import unittest
from datetime import datetime, timezone

from crm_core.access import AccountUnassigned, require_account, visible_accounts
from crm_core.account_names import account_naming, apply_name, name_proposal
from crm_core.account_profile import describe_profile, set_organization_type


class AccountMemory:
    def __init__(self):
        self.row = {"id": 10, "owner_id": 2, "name": "Old Name", "domain": "group.example",
                    "name_source": "unreviewed", "name_source_ref": None, "organization_type": "Unknown",
                    "suppressed": True}
        self.proposals = [name_proposal("directory", "Example Group", "directory:listing-1", "https://group.example",
                                       domain="group.example")]
        self.grant = {"rep_id": 3}
        self.blocked = "Competitor"
        self.changes = []

    def account(self, account_id, today=None):
        return self.row if account_id == self.row["id"] else None

    def accounts(self, today):
        return [self.row]

    def coverage(self, account, observed):
        return self.grant

    def restriction(self, account_id):
        return self.blocked

    def evidence(self, account_id):
        return {account_id: self.proposals}

    def save_name(self, account_id, actor_id, before, state, source_url, reason, observed):
        self.changes.append((account_id, state, source_url))

    def save_type(self, account_id, actor_id, previous, selected, observed):
        self.changes.append((account_id, previous, selected))


class AccountBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.store = AccountMemory()
        self.observed = datetime(2026, 11, 26, tzinfo=timezone.utc)
        self.admin = {"id": 1, "role": "admin"}

    def test_hb04_account_scope_and_visibility_run_with_fake_storage(self):
        owner = {"id": 2, "role": "sdr"}
        coverer = {"id": 3, "role": "sdr"}
        peer = {"id": 4, "role": "sdr"}
        self.assertEqual(require_account(self.store, owner, 10, "2026-11-26", self.observed, write=True)["id"], 10)
        rows = visible_accounts(self.store, coverer, "2026-11-26", self.observed, include_suppressed=True)
        self.assertEqual([(item["relation"], item["blocked"]) for item in rows], [("Coverage", "Competitor")])
        self.assertEqual(visible_accounts(self.store, owner, "2026-11-26", self.observed), [])
        with self.assertRaises(PermissionError):
            require_account(self.store, peer, 10, "2026-11-26", self.observed)
        self.store.row["owner_id"] = None
        with self.assertRaises(AccountUnassigned):
            require_account(self.store, self.admin, 10, "2026-11-26", self.observed, write=True)

    def test_hb04_name_approval_uses_current_evidence_and_store_port(self):
        review = account_naming(self.store.row, self.store.proposals)
        apply_name(self.store, self.admin, 10, self.observed, decision="approve", version=review["version"],
                   candidate=self.store.proposals[0]["key"])
        self.assertEqual(self.store.changes, [(10, ("Example Group", "directory", "directory:listing-1"),
                                               "https://group.example")])

    def test_hb04_profile_derivation_and_recording_are_separate(self):
        profile = describe_profile([{"country": "India"}, {"country": "not available"}],
                                   {"score": 10, "signals": [{"label": "Registration platform - Whova"}]}, cvent=True)
        self.assertEqual((profile["country"], profile["platform"], profile["fit_score"]), ("India", "Cvent", 10))
        set_organization_type(self.store, self.admin, self.store.row, "Unknown", "Association", self.observed)
        self.assertEqual(self.store.changes, [(10, "Unknown", "Association")])
