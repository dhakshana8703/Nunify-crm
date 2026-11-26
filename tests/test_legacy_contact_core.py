import unittest


EVENT = {"event_uuid": "0251dfa4-924b-4b27-b545-6067a6790692", "cvent_url": "https://cvent.test/evidence",
         "contact_name": "Sam Rivera", "contact_email": "sam.rivera@atlas.example"}
LEAD = {"apollo_id": "apollo-1", "company_id": 30, "company_name": "Atlas Forum", "domain": "atlas.example",
        "name": "Sam Rivera", "title": "Coordinator", "persona": "P2", "email": "sam.rivera@atlas.example",
        "linkedin_url": "https://linkedin.com/in/Sam-Rivera", "is_unlocked": 1, "discovered_at": None,
        "unlocked_at": "2026-11-24T00:00:00+00:00"}


class MemoryLegacyContacts:
    def __init__(self):
        self.actions = []
        self.holds = []

    def active_admin(self, actor_id):
        return actor_id == 7

    def prepare_cvent(self):
        self.actions.append("prepare-cvent")

    def prepare_apollo(self):
        self.actions.append("prepare-apollo")

    def linked_events(self):
        return [{"id": 12, "source_id": EVENT["event_uuid"], "source_url": EVENT["cvent_url"],
                 "account_id": 2, "domain": "atlas.example"}]

    def rules(self):
        return self.holds

    def aliases(self, account_ids):
        return {2: [(9, "old-atlas.example")]} if 2 in account_ids else {}

    def existing_people(self):
        return []

    def contact_restrictions(self):
        return []

    def person_by_email(self, account_id, email):
        return None

    def link_exists(self, person_id, source_record_id):
        return False

    def create_cvent_contact(self, candidate):
        self.actions.append((candidate.account_id, candidate.email))
        return 9

    def link_event(self, person_id, source_record_id):
        self.actions.append((person_id, source_record_id))

    def accounts(self):
        return [{"id": 2, "domain": "atlas.example"}]

    def saved_organizers(self):
        return [(2, {"organizer_domain": "atlas.example", "organizer_company": "Atlas Forum",
                     "identity_evidence": "directory"})]

    def mapped_leads(self):
        return {}

    def record_summary(self, actor_id, event, counts):
        self.actions.append((actor_id, event, counts["new_people"]))

    def create_apollo_lead(self, lead):
        self.actions.append((lead["account_id"], lead["email_verified"], lead["unlocked"], lead["linkedin_url"]))


class LegacyContactCoreTest(unittest.TestCase):
    def test_r101_legacy_imports_check_the_original_hostname(self):
        from crm_core.legacy_contacts import import_cvent_contacts, import_saved_leads

        rule = {"kind": "domain", "value": "xn--strae-oqa.de"}
        unicode_address = "sam.rivera@stra\u00dfe.de"
        ascii_address = "sam.rivera@strasse.de"

        class CventContactMemory(MemoryLegacyContacts):
            def linked_events(self):
                return [dict(row, domain="strasse.de") for row in super().linked_events()]

        for apply in (False, True):
            options = {"apply": apply, "actor_id": 7}
            for address, suppressed in ((unicode_address, 1), (ascii_address, 0)):
                with self.subTest(source="Cvent", apply=apply, address=address):
                    store = CventContactMemory()
                    store.holds = [rule]
                    summary = import_cvent_contacts(store, [dict(EVENT, contact_email=address)], **options)
                    self.assertEqual((summary["suppressed_contact_rows"], summary["new_people"]),
                                     (suppressed, 1 - suppressed))
                with self.subTest(source="Apollo", apply=apply, address=address):
                    store = MemoryLegacyContacts()
                    store.holds = [rule]
                    summary = import_saved_leads(store, [dict(LEAD, email=address)], [], **options)
                    self.assertEqual((summary["suppressed"], summary["new_people"]), (suppressed, 1 - suppressed))

    def test_r101_legacy_apollo_still_matches_an_existing_opted_out_address(self):
        from crm_core.legacy_contacts import import_saved_leads

        class OptedOutContactMemory(MemoryLegacyContacts):
            def existing_people(self):
                return [{"id": 13, "account_id": 8, "email": LEAD["email"], "linkedin_url": "",
                         "status": "opted_out", "do_not_contact": 0}]

        store = OptedOutContactMemory()
        summary = import_saved_leads(store, [dict(LEAD, email=LEAD["email"].upper())], [])
        self.assertEqual((summary["suppressed"], summary["new_people"]), (1, 0))

    def test_r102_legacy_contact_imports_honor_retired_account_id_and_domain_rules(self):
        from crm_core.legacy_contacts import import_cvent_contacts, import_saved_leads

        for kind, value in (("account", "9"), ("domain", "old-atlas.example")):
            with self.subTest(kind=kind):
                store = MemoryLegacyContacts()
                store.holds = [{"kind": kind, "value": value}]
                cvent = import_cvent_contacts(store, [EVENT])
                apollo = import_saved_leads(store, [LEAD], [])
                self.assertEqual((cvent["suppressed_contact_rows"], cvent["new_people"]), (1, 0))
                self.assertEqual((apollo["suppressed"], apollo["new_people"]), (1, 0))

    def test_saved_event_import_uses_offline_evidence_and_rejects_duplicate_event_rows(self):
        from crm_core.legacy_contacts import import_cvent_contacts

        store = MemoryLegacyContacts()
        summary = import_cvent_contacts(store, [EVENT, EVENT], apply=False)
        self.assertEqual((summary["duplicate_linked_event_rows"], summary["new_people"], store.actions), (2, 0, []))
        with self.assertRaises(PermissionError):
            import_cvent_contacts(store, [EVENT], apply=True, actor_id=8)
        self.assertEqual(store.actions, [])
        summary = import_cvent_contacts(store, [EVENT], apply=True, actor_id=7)
        self.assertEqual(summary["eligible_accounts"], 1)
        self.assertEqual(store.actions, ["prepare-cvent", (2, "sam.rivera@atlas.example"), (9, 12),
                                         (7, "cvent_contacts_imported", 1)])

    def test_saved_apollo_import_protects_account_identity_and_verifies_matching_email(self):
        from crm_core.legacy_contacts import import_saved_leads

        store = MemoryLegacyContacts()
        distinct_name = dict(LEAD, company_name="Different Association")
        review = import_saved_leads(store, [distinct_name], [], apply=False)
        self.assertEqual((review["review_identity"], review["new_people"], store.actions), (1, 0, []))
        proofs = [{"apollo_id": LEAD["apollo_id"], "email": LEAD["email"], "profile": {
            "id": LEAD["apollo_id"], "email": LEAD["email"], "email_status": "verified",
        }}]
        summary = import_saved_leads(store, [LEAD], proofs, apply=True, actor_id=7)
        self.assertEqual((summary["linked_accounts"], summary["verified_emails"]), (1, 1))
        self.assertEqual(store.actions, ["prepare-apollo", (2, 1, 1, "https://www.linkedin.com/in/sam-rivera"),
                                         (7, "cvent_apollo_leads_imported", 1)])
