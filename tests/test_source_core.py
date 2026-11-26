"""EF-02 account promotion rules through a storage-free port."""

import unittest


class SourceMemory:
    def __init__(self, *accounts):
        self.accounts = list(accounts)
        self.saved = {}
        self.imported = {}
        self.contacts = []
        self.blocked_ids = set()
        self.blocked_domains = set()
        self.blocked_addresses = set()

    def account_candidates(self, domain):
        return [account for account in self.accounts if (
            domain == account["domain"] or domain.endswith("." + account["domain"])
            or account["domain"].endswith("." + domain)
        )]

    def directory_accounts(self):
        ids = {item["account_id"] for (source, _), item in self.saved.items() if source == "directory"}
        return [account for account in self.accounts if account["id"] in ids]

    def saved_record(self, source, source_id):
        return self.saved.get((source, source_id))

    def imported_record(self, source, source_id):
        return self.imported.get((source, source_id))

    def organization_blocked(self, domain):
        return domain in self.blocked_domains

    def account_blocked(self, account_id):
        return account_id in self.blocked_ids

    def address_blocked(self, address):
        return address in self.blocked_addresses or address.rpartition("@")[2] in self.blocked_domains

    def create_account(self, name, domain, organization_type):
        identifier = max((account["id"] for account in self.accounts), default=0) + 1
        self.accounts.append(dict(id=identifier, name=name, domain=domain, organization_type=organization_type))
        return identifier

    def save_record(self, source, row, account_id, details):
        account = next(account for account in self.accounts if account["id"] == account_id)
        key = (source, row["source_id"])
        saved = dict(account_id=account_id, details=details, domain=account["domain"], review_reason=None,
                     source_url=row["source_url"], raw=row["raw"])
        changed = self.saved.get(key) != saved
        self.saved[key] = saved
        return changed

    def save_imported_record(self, source, source_id, source_url, account_id, raw):
        account = next(account for account in self.accounts if account["id"] == account_id)
        self.imported[(source, source_id)] = dict(account_id=account_id, domain=account["domain"], raw=raw)

    def save_contact(self, account_id, contact):
        self.contacts.append((account_id, contact))


def event(domain="trade.example", organizer="Example Trade Association", source_id="event-1"):
    return dict(error="", duplicate=False, reason="", shared_domain=False, domain=domain,
                source_id=source_id, source_url="https://events.example/expo", source_file="events.json",
                details=dict(organizer_company=organizer, client_domain="registration.example", identity_evidence=""),
                raw={"original": "value"})


def listing(domain="trade.example", name="Example Trade Association", source_id="listing-1"):
    return dict(error="", duplicate_id=False, reason=None, shared_host=False, source_id=source_id,
                source_url="https://directory.example/expo", source_file="listings.json",
                details=dict(host=domain, name=name, listing_type="National", members="2,500", category="Industry"),
                raw={"original": "listing"})


POLICY = dict(types=["National"], min_members=1000, include=["Industry"], exclude=[])


class SourceCoreTest(unittest.TestCase):
    def test_saved_preview_has_no_writes_and_overlap_replays_on_one_account(self):
        from crm_core.ingest import evaluate_saved

        repo = SourceMemory()
        record = event()
        self.assertEqual(evaluate_saved(repo, "cvent", record, {})[0], "imported")
        self.assertEqual(repo.accounts, [])
        self.assertEqual(repo.saved, {})
        self.assertEqual(evaluate_saved(repo, "cvent", record, {}, promote=True)[0], "imported")
        self.assertEqual(evaluate_saved(repo, "directory", listing(), POLICY, promote=True)[0], "imported")
        self.assertEqual(evaluate_saved(repo, "cvent", record, {}, promote=True)[0], "unchanged")
        self.assertEqual(len(repo.accounts), 1)
        self.assertEqual({item["account_id"] for item in repo.saved.values()}, {1})
        self.assertEqual(repo.saved[("cvent", "event-1")]["raw"], {"original": "value"})

    def test_parent_chapter_conflicts_stay_in_review_and_suppression_stops_promotion(self):
        from crm_core.ingest import evaluate_saved

        repo = SourceMemory(dict(id=7, name="Example Trade Association", domain="trade.example"))
        chapter = listing("north.trade.example", "Example Northern Chapter")
        self.assertEqual(evaluate_saved(repo, "directory", chapter, POLICY, promote=True)[0], "review")
        repo.blocked_ids.add(7)
        self.assertEqual(evaluate_saved(repo, "cvent", event(), {}, promote=True)[0], "suppressed")
        self.assertEqual(len(repo.accounts), 1)
        self.assertEqual(repo.saved, {})

    def test_flat_intake_keeps_source_id_and_never_saves_restricted_outreach(self):
        from crm_core.ingest import evaluate_intake

        repo = SourceMemory()
        repo.blocked_addresses.add("blocked@trade.example")
        row = {"source_id": "external-1", "domain": "trade.example", "name": "Example Trade Association",
               "contact_name": "Example Contact", "contact_email": "blocked@trade.example"}
        self.assertEqual(evaluate_intake(repo, "partner", row, preview=True)[0], "staged")
        self.assertEqual(repo.accounts, [])
        self.assertEqual(evaluate_intake(repo, "partner", row)[0], "review")
        self.assertEqual(repo.contacts, [])
        self.assertEqual(evaluate_intake(repo, "partner", row)[0], "unchanged")
        self.assertEqual(evaluate_intake(repo, "partner", {**row, "domain": "other.example"})[0], "review")
        self.assertEqual(len(repo.accounts), 1)
        self.assertEqual(repo.imported[("partner", "external-1")]["account_id"], 1)


if __name__ == "__main__":
    unittest.main()
