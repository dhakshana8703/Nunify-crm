import unittest

from crm_core import account_names
from source_ops import _file_rows


ACCOUNT = {
    "id": 1, "domain": "national.example-council.co.uk", "name": "Examplecouncil",
    "name_source": "unreviewed", "name_source_ref": None,
}


class AccountNamePolicyTest(unittest.TestCase):
    def test_r1031_precedence_and_domain_fallback_use_linked_identity(self):
        organizer = dict(
            id=1, domain=ACCOUNT["domain"], source="cvent", source_id="event-one",
            source_url="https://web.cvent.com/register",
            details={"organizer_company": "Event Organizer Council", "event_name": "Annual Conference"},
        )
        directory = dict(
            id=1, domain=ACCOUNT["domain"], source="directory", source_id="listing-one",
            source_url="https://directory.example/profile", details={"name": "Directory Council"},
        )
        site = dict(
            id=1, domain=ACCOUNT["domain"], source_slug="upload", external_id="official-site",
            source_url="https://www.example-council.co.uk/about",
            payload={"og:site_name": "Official Council", "website_title": "Official Council | Home"},
        )
        registration = dict(site, source_url="https://web.cvent.com/register",
                            payload={"og:site_name": "Unrelated Registration"})
        apollo = dict(id=1, domain=ACCOUNT["domain"], external_id="apollo-one",
                      source_url="https://example-council.co.uk", name="Apollo Council")
        cases = [
            ([organizer, directory], [site, registration], [apollo], "apollo", "Apollo Council"),
            ([organizer, directory], [site, registration], [], "organizer", "Event Organizer Council"),
            ([directory], [site, registration], [], "directory", "Directory Council"),
            ([], [site, registration], [], "website", "Official Council"),
            ([], [registration], [], "domain", "Example Council"),
        ]
        for saved, imported, apollo_rows, source, name in cases:
            with self.subTest(source=source):
                evidence = account_names.name_evidence(saved, imported, apollo_rows)
                preferred = account_names.account_naming(ACCOUNT, evidence.get(1, ()))["preferred"]
                self.assertEqual((preferred["source"], preferred["name"]), (source, name))

    def test_r1031_platform_has_no_domain_or_website_name_fallback(self):
        account = dict(ACCOUNT, domain="medc.cventevents.com", name="Registration")
        website = dict(id=1, domain=account["domain"], source_slug="upload", external_id="platform",
                       source_url="https://medc.cventevents.com/registration",
                       payload={"og:site_name": "Shared Platform"})
        evidence = account_names.name_evidence([], [website])
        self.assertEqual(account_names.account_naming(account, evidence.get(1, ()))["state"], "missing")

    def test_r1031_authoritative_disagreement_stays_visible_after_a_name_was_saved(self):
        first = dict(id=1, domain=ACCOUNT["domain"], external_id="one",
                     source_url="https://example-council.co.uk", name="Example Council North")
        second = dict(first, external_id="two", name="Example Council South")
        evidence = account_names.name_evidence([], [], [first, second])
        record = account_names.account_naming(dict(ACCOUNT, name_source="organizer"), evidence[1])
        self.assertEqual(record["state"], "conflicting")
        self.assertIsNone(record["preferred"])

    def test_r1031_domain_fallback_decodes_international_names(self):
        record = account_names.account_naming(dict(ACCOUNT, domain="xn--bcher-kva.de"), [])
        self.assertEqual(record["preferred"]["name"], "Bücher")

    def test_r1031_website_titles_from_an_external_feed_require_explicit_review(self):
        website = dict(id=1, domain=ACCOUNT["domain"], source_slug="partner", external_id="one",
                       source_url="https://example-council.co.uk",
                       payload={"website_title": "External Reported Name"})
        evidence = account_names.name_evidence([], [website])
        record = account_names.account_naming(ACCOUNT, evidence[1])
        self.assertEqual(record["preferred"]["source"], "domain")
        self.assertIn("External Reported Name", [item["name"] for item in record["candidates"]])

    def test_r1031_admin_upload_preserves_open_graph_name_priority(self):
        uploaded = _file_rows(iter([
            ["name", "domain", "og:site_name", "website_title"],
            ["Examplecouncil", ACCOUNT["domain"], "Official Council", "Official Council | Home"],
        ]))
        _, row = uploaded[0]
        site = dict(id=1, domain=ACCOUNT["domain"], source_slug="upload", external_id="one",
                    source_url="https://example-council.co.uk", payload=row)
        evidence = account_names.name_evidence([], [site])
        self.assertEqual(account_names.account_naming(ACCOUNT, evidence[1])["preferred"]["name"], "Official Council")

    def test_r1033_domain_fallback_preserves_spelling_and_flags_existing_full_names(self):
        acronym = account_names.account_naming(dict(ACCOUNT, domain="afge.org", name="AFGE"), [])
        self.assertEqual(acronym["preferred"]["name"], "AFGE")
        expanded = account_names.account_naming(dict(ACCOUNT, domain="dell.com", name="Dell Technologies"), [])
        self.assertEqual(expanded["state"], "review_domain")
        self.assertIsNone(expanded["preferred"])


if __name__ == "__main__":
    unittest.main()
