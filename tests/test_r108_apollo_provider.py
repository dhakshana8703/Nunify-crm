import io
import unittest
from decimal import Decimal
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from apollo_provider import ApolloProvider, ApolloResponseError


class ApolloProviderTest(unittest.TestCase):
    def setUp(self):
        self.transport = Mock()
        self.provider = ApolloProvider("test-key", transport=self.transport)
        self.search = dict(action="people_search", domain="https://www.events.example.org/meetings",
                           title="Head of Events")
        self.unlock = dict(action="email_unlock", domain="events.example.org",
                           apollo_person_id="person-123")

    def use_response(self, body, status=200):
        response = io.BytesIO(body)
        response.status = status
        self.transport.return_value = response

    def call(self, job):
        return self.provider(job, idempotency_key="local-only-reference")

    def test_r1082_search_is_free_and_preserves_the_locked_candidate_identity(self):
        self.use_response(b'''{"people": [
            {"id": "former", "first_name": "Former", "title": "Lead",
             "organization": {"primary_domain": "elsewhere.example"}},
            {"id": "person-123", "first_name": "Avery", "last_name_obfuscated": "Ex***e",
             "title": "Head of Events", "email": "unexpected@example.org"}]}''')
        self.assertEqual(self.call(self.search), {"credits": 0, "apollo_person_id": "person-123",
                                                 "name": "Avery Ex***e", "title": "Head of Events"})
        request = self.transport.call_args.args[0]
        url = urlsplit(request.full_url)
        self.assertEqual((request.get_method(), url.scheme, url.netloc, url.path),
                         ("POST", "https", "api.apollo.io", "/api/v1/mixed_people/api_search"))
        self.assertEqual(parse_qs(url.query)["q_organization_domains_list[]"], ["example.org"])
        self.assertEqual(parse_qs(url.query)["person_titles[]"], ["Head of Events"])
        self.assertEqual(request.get_header("X-api-key"), "test-key")
        self.assertNotIn("local-only-reference", repr(request.header_items()))
        self.transport.assert_called_once()

    def test_search_sends_multiple_titles_for_one_company_in_one_request(self):
        self.use_response(b'''{"people": [
            {"id": "former", "first_name": "Former", "title": "Executive Director",
             "organization": {"primary_domain": "elsewhere.example"}},
            {"id": "current", "first_name": "Avery", "title": "Executive Director",
             "organization": {"primary_domain": "members.example.org"}}]}''')
        titles = ["Director of Meetings and Events", "Executive Director"]
        self.assertEqual(self.call(dict(self.search, titles=titles)), {
            "credits": 0, "apollo_person_id": "current", "name": "Avery", "title": "Executive Director",
        })
        query = parse_qs(urlsplit(self.transport.call_args.args[0].full_url).query)
        self.assertEqual(query["person_titles[]"], titles)
        self.assertEqual(query["q_organization_domains_list[]"], ["example.org"])
        self.assertEqual(query["include_similar_titles"], ["true"])
        self.transport.assert_called_once()

    def test_search_rejects_empty_or_malformed_title_lists_without_a_request(self):
        for titles in ([], ["Head of Events", " "], [None], "Head of Events"):
            with self.subTest(titles=titles):
                with self.assertRaises(ValueError):
                    self.call(dict(self.search, titles=titles))
        self.transport.assert_not_called()

    def test_r1082_exact_person_unlock_preserves_fraction_and_disables_other_reveals(self):
        self.use_response(b'''{"credits_consumed": 0.125, "matches": [{"id":"person-123",
            "email": "avery@sales.example.org", "email_status": "verified",
            "organization": {"primary_domain": "www.example.org"}}]}''')
        self.assertEqual(self.call(self.unlock), {"credits": Decimal("0.125"),
                                                 "email": "avery@sales.example.org", "email_verified": True})
        request = self.transport.call_args.args[0]
        url = urlsplit(request.full_url)
        self.assertEqual((request.get_method(), url.path), ("POST", "/api/v1/people/bulk_match"))
        self.assertEqual(request.data, b'{"details": [{"id": "person-123"}]}')
        self.assertEqual(parse_qs(url.query), {"reveal_personal_emails": ["false"],
                                              "reveal_phone_number": ["false"],
                                              "run_waterfall_email": ["false"],
                                              "run_waterfall_phone": ["false"]})
        self.transport.assert_called_once()

    def test_r1082_no_match_or_no_search_results_returns_zero(self):
        self.use_response(b'{"credits_consumed": 0, "matches": [null]}')
        self.assertEqual(self.call(self.unlock), {"credits": 0})
        self.use_response(b'{"people": []}')
        self.assertEqual(self.call(self.search), {"credits": 0})

    def test_r1082_confirmed_charge_survives_email_identity_rejections(self):
        rejected = {
            "different person": b'{"id":"other", "email":"avery@example.org", "email_status":"verified"}',
            "unconfirmed email": b'{"id":"person-123", "email":"avery@example.org", "email_status":"guessed"}',
            "different email domain": b'{"id":"person-123", "email":"avery@elsewhere.example", "email_status":"verified", "organization":{"primary_domain":"example.org"}}',
            "different current employer": b'{"id":"person-123", "email":"avery@example.org", "email_status":"verified", "organization":{"primary_domain":"elsewhere.example"}}',
            "invalid match shape": b'false',
        }
        for name, match in rejected.items():
            with self.subTest(name=name):
                self.use_response(b'{"credits_consumed": 0.25, "matches": [' + match + b']}')
                self.assertEqual(self.call(self.unlock), {"credits": Decimal("0.25")})

    def test_r1082_missing_or_invalid_charge_is_uncertain_and_transport_is_called_once(self):
        for body in (b'{"matches": [null]}', b'{"credits_consumed": true}',
                     b'{"credits_consumed": -1}', b'{"credits_consumed": NaN}', b'not JSON'):
            with self.subTest(body=body):
                self.transport.reset_mock()
                self.use_response(body)
                with self.assertRaises(ApolloResponseError):
                    self.call(self.unlock)
                self.transport.assert_called_once()

    def test_r1082_transport_failure_does_not_retry_or_expose_details(self):
        self.transport.side_effect = TimeoutError("internal transport diagnostic with test-key")
        with self.assertRaises(ApolloResponseError) as error:
            self.call(self.unlock)
        self.assertNotIn("test-key", str(error.exception))
        self.transport.assert_called_once()

    def test_r1082_invalid_target_or_unsupported_action_cannot_call_provider(self):
        for job in (dict(self.unlock, apollo_person_id=None), dict(self.unlock, domain="web.cvent.com"),
                    dict(self.unlock, action="phone_unlock"), dict(self.search, title="")):
            with self.subTest(job=job):
                with self.assertRaises(ValueError):
                    self.call(job)
        self.transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
