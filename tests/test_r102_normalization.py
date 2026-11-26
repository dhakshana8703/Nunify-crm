"""R1-02.1 acceptance coverage for the offline registrable-domain policy."""

import unittest

import organization_identity


class RootDomainNormalizationTest(unittest.TestCase):
    def test_r102_1_normalizes_urls_hosts_and_email_with_the_public_suffix_list(self):
        cases = {
            " https://www.national.alpfa.org/events ": "alpfa.org",
            "EVENTS.example.co.uk.": "example.co.uk",
            "person@team.example.org.au": "example.org.au",
            "https://events.bücher.de:443/2026": "xn--bcher-kva.de",
            "planner@team.lakeshore.example": "lakeshore.example",
            "north.example.test": "example.test",
            "www.city.kawasaki.jp": "city.kawasaki.jp",
            "www.ck": "www.ck",
            "m.events.alpha.github.io": "alpha.github.io",
            "m.events.beta.github.io": "beta.github.io",
        }
        for supplied, expected in cases.items():
            with self.subTest(supplied=supplied):
                self.assertEqual(organization_identity.root_domain(supplied), expected)

    def test_r102_1_rejects_inputs_with_no_usable_domain(self):
        cases = (
            "", None, "localhost", "https://192.0.2.1/path", "https://[2001:db8::1]",
            "co.uk", "github.io", "north.ck", "https://user@example.org/path",
            "https://example.org:invalid", "user@@example.org", "user@example.org/path",
            "web..example.org", "http://", "no valid host.example.org",
        )
        for supplied in cases:
            with self.subTest(supplied=supplied):
                self.assertEqual(organization_identity.root_domain(supplied), "")

    def test_r102_1_keeps_distinct_internationalized_and_ascii_organizations_apart(self):
        self.assertEqual(organization_identity.organization_host("www.events.faß.de"), "events.xn--fa-hia.de")
        self.assertEqual(organization_identity.root_domain("team.faß.de"), "xn--fa-hia.de")
        self.assertEqual(organization_identity.root_domain("team.fass.de"), "fass.de")

    def test_r102_1_saved_host_does_not_drop_www_if_it_identifies_a_tenant(self):
        self.assertEqual(organization_identity.organization_host("https://www.ck/contact"), "www.ck")
        host = organization_identity.organization_host("https://www.github.io/contact")
        self.assertEqual(organization_identity.root_domain(host), "www.github.io")
