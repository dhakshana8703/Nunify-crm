import unittest
from datetime import date, datetime, timezone

from crm_core.account_facts import business_day, derive_account_facts


TODAY = date(2026, 11, 26)


class AccountFactPolicyTest(unittest.TestCase):
    def facts(self, *, events=(), listings=(), qualification=None, cvent_roster=False, today=TODAY):
        return derive_account_facts(
            observed_on=today, events=iter(events), listings=iter(listings),
            qualification=qualification, cvent_roster=cvent_roster,
        )

    def test_roster_confirms_cvent_without_creating_event_facts(self):
        facts = self.facts(
            events=[("company:52", "2026-11-27")], cvent_roster=True,
            qualification={"signals": [{"label": "Registration platform - Swoogo"}]},
        )
        self.assertEqual(facts, {
            "platform": "cvent", "platform_detail": None, "platform_confidence": "confirmed",
            "next_event_date": None, "events_per_year": None, "members": None, "country": None, "region": None,
        })

    def test_linked_event_wins_over_other_platform_with_or_without_date(self):
        facts = self.facts(
            events=[("undated-event", None)],
            qualification={"signals": [{"label": "Registration platform - Swoogo"}]},
        )
        self.assertEqual((facts["platform"], facts["platform_confidence"]), ("cvent", "confirmed"))
        self.assertIsNone(facts["events_per_year"])

    def test_detected_platform_requires_consistent_evidence(self):
        def platform(*labels):
            result = self.facts(qualification={"signals": [
                {"label": f"Registration platform - {label}"} for label in labels
            ]})
            return (result["platform"], result["platform_detail"], result["platform_confidence"])

        self.assertEqual(platform("Swoogo", "swoogo"), ("other_detected", "Swoogo", "detected"))
        self.assertEqual(platform("Swoogo", "Whova"), ("unknown", None, "unknown"))
        self.assertEqual(platform("None"), ("none", None, "confirmed"))
        self.assertEqual(platform("None", "Swoogo"), ("unknown", None, "unknown"))
        self.assertEqual(platform("Unknown"), ("unknown", None, "unknown"))

    def test_next_event_and_frequency_use_distinct_valid_dates(self):
        events = [
            ("outside", "2025-11-26"),
            ("previous", "2025-11-27"),
            ("next", "2026-11-29"),
            ("today", "2026-11-26"),
            ("today", "2026-11-26"),
            ("invalid", "2026-11-31"),
            ("not a normalized date", "20261125"),
            ("conflicting", "2026-11-27"),
            ("conflicting", "2026-03-01"),
        ]
        facts = self.facts(events=events)
        self.assertEqual((facts["next_event_date"], facts["events_per_year"]), ("2026-11-26", 2))
        corrected = [(key, "2026-11-25" if key == "today" else value) for key, value in events]
        self.assertEqual(self.facts(events=corrected)["next_event_date"], "2026-11-29")
        self.assertEqual(self.facts(events=[("future", "2026-11-29")])["events_per_year"], 0)

    def test_trailing_year_clamps_leap_day_boundary(self):
        facts = self.facts(today=date(2028, 2, 29), events=[
            ("cutoff", "2027-02-28"), ("within", "2027-03-01"), ("today", "2028-02-29"),
        ])
        self.assertEqual((facts["events_per_year"], facts["next_event_date"]), (2, "2028-02-29"))

    def test_linked_country_aliases_resolve_to_the_documented_sales_regions(self):
        cases = [
            (("United States", "U.S.A."), "United States", "US"),
            (("India", "IN"), "India", "India"),
            (("UK", "Great Britain"), "United Kingdom", "Europe"),
            (("United Arab Emirates", "UAE"), "United Arab Emirates", "Middle East"),
            (("Canada", "CA"), "Canada", "Other"),
            (("KG", "Kyrgyzstan"), "Kyrgyzstan", "Other"),
        ]
        for names, country, region in cases:
            with self.subTest(names=names):
                facts = self.facts(listings=[{"country": name} for name in names])
                self.assertEqual((facts["country"], facts["region"]), (country, region))

    def test_missing_or_conflicting_company_country_remains_unknown(self):
        cases = [
            (), ({"country": "N/A"},), ({"country": "Europe"},), ({"country": "Atlantis"},),
            ({"country": "US"}, {"country": "India"}),
        ]
        for listings in cases:
            with self.subTest(listings=listings):
                facts = self.facts(events=[("event-on-another-continent", "2026-11-28")], listings=listings)
                self.assertEqual((facts["country"], facts["region"]), (None, None))

    def test_member_counts_must_be_exact_and_consistent(self):
        facts = self.facts(listings=[{"members": "2,400"}, {"members": 2400}, {"members": "2,000-3,000"}])
        self.assertEqual(facts["members"], 2400)
        for values in [("1,000+", "n/a"), ("1000", "2000"), ("10,00", True), ("Over 2000", "")]:
            with self.subTest(values=values):
                self.assertIsNone(self.facts(listings=[{"members": value} for value in values])["members"])

    def test_business_day_respects_india_midnight(self):
        observed = datetime(2026, 11, 26, 18, 30, tzinfo=timezone.utc)
        self.assertEqual(business_day(observed, "Asia/Kolkata"), date(2026, 11, 27))
        self.assertEqual(business_day(observed, "UTC"), TODAY)
