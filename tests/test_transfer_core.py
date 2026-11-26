import unittest
from unittest.mock import Mock

from crm_core.transfer import COUNTERS, select_source_contacts, transfer_accounts, transfer_contact_data


class TransferMemory:
    def __init__(self):
        self.writes = []

    def accounts(self):
        return [{"id": 77, "identity_key": "first", "domain": "first.example"}]

    def add_accounts(self, additions):
        self.writes.append(tuple(row["identity_key"] for row in additions))
        return {"second": 78}

    def add_work(self, rows):
        self.writes.append(tuple(rows))

    def add_qualifications(self, rows):
        self.writes.append(tuple(rows))


class TransferCoreTest(unittest.TestCase):
    def test_r101_saved_contacts_keep_the_original_hostname_during_transfer(self):
        facts = {"account_id": 11, "account_domain": "atlas.example", "origin": "source",
                 "linkedin_url": "", "do_not_contact": False, "status": "not_contacted",
                 "unlocked": True, "email_verified": True}
        held = dict(facts, id=1, name="Held Contact", email="held@stra\u00dfe.de")
        eligible = dict(facts, id=2, name="Eligible Contact", email="eligible@strasse.de")
        evidence = {person["id"]: [{"account_id": 11, "source": "cvent", "source_record_id": 18,
                                    "observed_at": "2026-11-26"}] for person in (held, eligible)}
        rule = {"kind": "domain", "value": "xn--strae-oqa.de"}
        for source_rules, destination_rules in (([rule], []), ([], [rule])):
            with self.subTest(source_rules=bool(source_rules)):
                data = select_source_contacts([held, eligible], {}, evidence, source_rules, {})
                self.assertEqual(data["blocked"], {1} if source_rules else set())
                store = Mock()
                store.people.return_value = []
                store.apollo_links.return_value = {}
                store.evidence_links.return_value = []
                store.rules.return_value = destination_rules
                store.accounts.return_value = [{"id": 41, "canonical_id": 41, "domain": "atlas.example"}]
                store.add_person.return_value = 90
                for apply in (False, True):
                    counts = dict.fromkeys(COUNTERS, 0)
                    transfer_contact_data(store, data, {11: 41}, {18: 85}, counts, apply=apply)
                    self.assertEqual((counts["contacts_held"], counts["people_added"]), (1, 1))
                    if not apply:
                        store.add_person.assert_not_called()
                store.add_person.assert_called_once_with(41, eligible)

    def test_hb04_snapshot_apply_preserves_existing_ids_and_excludes_old_assignment(self):
        repository = TransferMemory()
        accounts = [
            {"id": 1, "identity_key": "first", "domain": "first.example", "stage": "qualified"},
            {"id": 2, "identity_key": "second", "domain": "second.example", "stage": "assigned"},
        ]
        counts = {}
        self.assertEqual(transfer_accounts(repository, accounts, {}, counts, apply=False), {1: 77, 2: None})
        self.assertEqual(repository.writes, [])
        self.assertEqual(transfer_accounts(repository, accounts, {}, counts, apply=True), {1: 77, 2: 78})
        self.assertEqual(repository.writes, [("second",), ((78, "discovered"),), ()])

    def test_hb04_seed_replay_requires_review_before_switching_a_source_link(self):
        from crm_core.transfer import seed_snapshot

        class SeedMemory:
            def ensure_account(self, identity, facts):
                pass

            def account_ids(self):
                return {"new": 41}

            def record_link(self, source, source_id):
                return {"account_id": 40}

            def write_record(self, record, account_id):
                raise AssertionError("A conflicting link cannot be written.")

        with self.assertRaisesRegex(ValueError, "Review required before changing the account link"):
            seed_snapshot(SeedMemory(), {"new": {}}, [
                {"source": "directory", "source_id": "listing", "identity_key": "new"},
            ])

    def test_hb04_snapshot_rejects_duplicate_identity_assumptions(self):
        repository = TransferMemory()
        with self.assertRaisesRegex(ValueError, "conflicts with a destination account"):
            transfer_accounts(repository, [{"id": 1, "identity_key": "other", "domain": "www.FIRST.example."}],
                              {}, {}, apply=True)
        self.assertEqual(repository.writes, [])
