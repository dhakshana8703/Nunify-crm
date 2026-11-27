"""Explicit HTTP adapter for Apollo's free search and billed work-email reveal."""

import json
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from organization_identity import EMAIL_WITH_DOMAIN, is_platform_domain, organization_host, root_domain, text_value


class ApolloResponseError(RuntimeError):
    """Apollo did not return a usable confirmation."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _organization_matches(person, domain):
    organization = person.get("organization")
    if organization is None:
        return True
    if not isinstance(organization, dict):
        return False
    return all(not value or root_domain(value) == domain
               for value in (organization.get("primary_domain"), organization.get("website_url")))


class ApolloProvider:
    requires_confirmation = True

    def __init__(self, key, *, transport=None):
        self._key = text_value(key)
        if not self._key:
            raise ValueError("An Apollo key is required.")
        self._transport = transport if transport is not None else build_opener(_NoRedirects()).open

    def _post(self, endpoint, *, params, payload=None):
        request = Request(f"https://api.apollo.io/api/v1/{endpoint}?{urlencode(params, doseq=True)}",
                          data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                          headers={"X-Api-Key": self._key, "Accept": "application/json",
                                   "Content-Type": "application/json", "Cache-Control": "no-cache"},
                          method="POST")
        try:
            with self._transport(request, timeout=15) as response:
                if response.status != 200:
                    raise ApolloResponseError("Apollo did not confirm the request.")
                limit = 1024 * 1024
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise ApolloResponseError("Apollo's response exceeds the size limit.")
                result = json.loads(body, parse_float=Decimal)
        except HTTPError as error:
            error.close()
            raise ApolloResponseError("Apollo did not confirm the request.") from None
        except (OSError, ValueError):
            raise ApolloResponseError("Apollo did not return a readable confirmation.") from None
        if not isinstance(result, dict):
            raise ApolloResponseError("Apollo did not return a usable confirmation.")
        return result

    def __call__(self, job, *, idempotency_key):
        # The job reference is local. Apollo does not document request deduplication for these endpoints.
        host = organization_host(job.get("domain"))
        domain = root_domain(host)
        if not domain or is_platform_domain(host):
            raise ValueError("A verified company domain is required for Apollo.")
        if job["action"] == "people_search":
            return self._search(domain, job.get("titles", (job.get("title"),)), include_similar="titles" in job)
        if job["action"] == "email_unlock":
            return self._unlock(domain, text_value(job.get("apollo_person_id")))
        raise ValueError("This Apollo action is unavailable.")

    def _search(self, domain, titles, *, include_similar):
        if not isinstance(titles, (tuple, list)):
            raise ValueError("Provide job titles as a list for Apollo people search.")
        titles = tuple(dict.fromkeys(text_value(title) for title in titles))
        if not titles or "" in titles or len(titles) >= 100:
            raise ValueError("Provide one to 99 nonempty job titles for Apollo people search.")
        params = {"q_organization_domains_list[]": domain, "person_titles[]": titles, "per_page": 10}
        if include_similar:
            params["include_similar_titles"] = "true"
        result = self._post("mixed_people/api_search", params=params)
        people = result.get("people")
        if not isinstance(people, list):
            raise ApolloResponseError("Apollo did not return a candidate list.")
        for person in people:
            if not isinstance(person, dict) or not _organization_matches(person, domain):
                continue
            person_id = text_value(person.get("id"))
            name = text_value(person.get("name")) or " ".join(filter(None, (
                text_value(person.get("first_name")), text_value(person.get("last_name_obfuscated")),
            )))
            if person_id and name:
                fallback_title = titles[0] if len(titles) == 1 else ""
                return {"credits": 0, "apollo_person_id": person_id, "name": name,
                        "title": text_value(person.get("title")) or fallback_title}
        return {"credits": 0}

    def _unlock(self, domain, person_id):
        if not person_id:
            raise ValueError("The contact needs its Apollo person ID before email unlock.")
        result = self._post("people/bulk_match", params={"reveal_personal_emails": "false",
                                                        "reveal_phone_number": "false",
                                                        "run_waterfall_email": "false",
                                                        "run_waterfall_phone": "false"},
                            payload={"details": [{"id": person_id}]})
        amount = result.get("credits_consumed")
        if type(amount) not in {int, Decimal} or not Decimal(amount).is_finite() or amount < 0:
            raise ApolloResponseError("Apollo did not confirm the credit amount.")
        confirmed = {"credits": amount}
        matches = result.get("matches")
        if isinstance(matches, list) and len(matches) == 1 and isinstance(matches[0], dict):
            person = matches[0]
            email = text_value(person.get("email"))
            if (person.get("id") == person_id and person.get("email_status") == "verified"
                    and EMAIL_WITH_DOMAIN.fullmatch(email) and "\x00" not in email
                    and root_domain(email) == domain and _organization_matches(person, domain)):
                confirmed.update(email=email, email_verified=True)
        return confirmed
