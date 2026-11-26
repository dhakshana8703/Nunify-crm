"""Decide which source evidence can be promoted into CRM accounts."""

import hashlib
import json
import re
from typing import Protocol

from crm_core.account_identity import corroborated_account
from organization_identity import (
    is_platform_domain, normalize_dns_hostname, normalized_words, organization_host, root_domain,
)


PLACEHOLDER_NAMES = {
    "unknown", "n a", "none", "not specified", "event organizer", "cventevents", "events", "registration", "home",
}
MEMBERS = re.compile(r"^\s*(\d{1,3}(?:,\d{3})+|\d+)(?![\d,])")


class SourceRepository(Protocol):
    """Account and evidence storage within the caller's unit of work."""

    def account_candidates(self, root: str) -> list[dict]: ...
    def directory_accounts(self) -> list[dict]: ...
    def saved_record(self, source: str, source_id: str) -> dict | None: ...
    def imported_record(self, source: str, source_id: str) -> dict | None: ...
    def organization_blocked(self, domain: str) -> bool: ...
    def account_blocked(self, account_id: int) -> bool: ...
    def address_blocked(self, address: str) -> bool: ...
    def create_account(self, name: str, domain: str, organization_type: str) -> int | None: ...
    def save_record(self, source: str, row: dict, account_id: int, details: dict) -> bool: ...
    def save_imported_record(self, source: str, source_id: str, source_url: str, account_id: int, raw: str) -> None: ...
    def save_contact(self, account_id: int, contact: tuple[str, str, str]) -> None: ...


def _match_account(repository, domain, name):
    if not domain:
        if len(normalized_words(name).split()) >= 2:
            matches = [account for account in repository.directory_accounts() if (
                normalized_words(name) == normalized_words(account["name"]) and organization_host(account["domain"])
            )]
            if len(matches) == 1:
                return _match_account(repository, matches[0]["domain"], name)
        return None, "The organizer needs a verified website or a unique corroborating organization record."

    root = root_domain(domain)
    if not root:
        return None, "A registrable organization domain is required."
    candidates = repository.account_candidates(root)
    account = corroborated_account(candidates, name)
    if account is not None:
        return dict(account, id=account.get("canonical_id", account["id"])), ""
    return None, "The domain is shared with a differently named or ambiguous account." if candidates else ""


def _organization_type(name):
    words = set(normalized_words(name).split())
    if words & {"association", "federation", "society"}:
        return "Association"
    if words & {"university", "college"}:
        return "University"
    return "Unknown"


def _apply_saved(repository, source, row, details, existing, account_id, name, domain, organization_type, promote):
    if not promote:
        if existing:
            return "unchanged", "Already linked. Promotion can refresh its source evidence."
        if account_id is not None:
            return "imported", "Eligible to link to the existing account."
        return "imported", "Eligible to create an account."

    if account_id is None:
        account_id = repository.create_account(name, domain, organization_type)
        if account_id is None:
            kind = "event" if source == "cvent" else "listing"
            return "review", f"An existing account identity conflicts with this {kind}."
    if repository.save_record(source, row, account_id, details):
        return "imported", "Linked to the organization."
    return "unchanged", "Source record already imported."


def _cvent(repository, row, promote):
    if row["error"]:
        return "invalid", row["error"]
    if row["duplicate"]:
        return "review", "The export contains this Cvent event ID more than once."
    details = dict(row["details"])
    organizer = details["organizer_company"]
    observed_domain = organization_host(row["domain"])
    if observed_domain and repository.organization_blocked(observed_domain):
        return "suppressed", "The verified organizer matches an outreach restriction."
    existing = repository.saved_record("cvent", row["source_id"])
    if existing:
        account_id = existing["account_id"]
        previous = existing["details"]
        previous_domain = organization_host(previous.get("organizer_domain") or existing["domain"])
        if (account_id is None or row["reason"] or normalized_words(organizer) != normalized_words(previous.get("organizer_company"))
                or details["client_domain"] != previous.get("client_domain")
                or observed_domain and root_domain(observed_domain) != root_domain(previous_domain)):
            return "review", "The saved organizer evidence changed. The existing event link is preserved."
        details["identity_evidence"] = previous.get("identity_evidence", "")
        organizer_domain = previous.get("organizer_domain") or observed_domain
        domain = observed_domain or previous_domain
    else:
        if row["reason"]:
            return "review", row["reason"]
        words = normalized_words(organizer)
        if words in {"cvent", "cvent inc", "cvent inc llc", "cvent llc"}:
            return "suppressed", "The saved organizer identifies Cvent itself."
        if not organizer or words in PLACEHOLDER_NAMES or len(organizer) > 200:
            return "review", "The saved event has no usable organization name."
        if row["shared_domain"]:
            return "review", "This organizer website is shared by differently named organizations."
        account, reason = _match_account(repository, observed_domain, organizer)
        if reason:
            return "review", reason
        account_id = account["id"] if account else None
        domain = observed_domain
        if account:
            domain = domain or organization_host(account["domain"])
            if not details["identity_evidence"]:
                details["identity_evidence"] = (
                    "Organizer name and domain corroborated by the existing CRM account." if observed_domain
                    else "Exact organizer name corroborated by the existing directory account."
                )
        organizer_domain = domain

    retained_domains = {domain, organizer_domain} - {"", observed_domain}
    if ((account_id is not None and repository.account_blocked(account_id))
            or any(repository.organization_blocked(host) for host in retained_domains)):
        return "suppressed", "The verified organizer matches an outreach restriction."
    if is_platform_domain(domain) or is_platform_domain(organizer_domain):
        return "review", "The saved organization domain is a shared registration or communications platform."
    if organizer_domain:
        details["organizer_domain"] = organizer_domain
    outcome, detail = _apply_saved(repository, "cvent", row, details, existing, account_id, organizer, domain,
                                   _organization_type(organizer), promote)
    if "date_review" in details and outcome != "review":
        detail += f" Event dates need review. {details['date_review']}"
    return outcome, detail


def _listing_filter_reason(details, policy):
    kind = normalized_words(details["listing_type"])
    if kind not in {normalized_words(value) for value in policy["types"]}:
        return "Association type is outside the current promotion filter."
    member_count = MEMBERS.match(str(details["members"] or ""))
    count_digits = member_count[1].replace(",", "") if member_count else ""
    if not count_digits or len(count_digits) > 12 or int(count_digits) < policy["min_members"]:
        return "Membership is missing or below the current promotion minimum."
    category = f" {normalized_words(details['category'])} "

    def matches(terms):
        return any(phrase and f" {phrase} " in category for phrase in map(normalized_words, terms))

    if matches(policy["exclude"]) or (policy["include"] and not matches(policy["include"])):
        return "Directory category is outside the current promotion filter."
    return ""


def _directory(repository, row, policy, promote):
    if row.get("error"):
        return "invalid", row["error"]
    if row["duplicate_id"]:
        return "review", "The export contains the same directory listing ID more than once."
    if row.get("reason"):
        return "review", row["reason"]
    details = row["details"]
    domain = details["host"]
    name = details["name"]
    existing = repository.saved_record("directory", row["source_id"])
    if existing:
        account_id = existing["account_id"]
        if account_id is None:
            return "review", existing["review_reason"]
        previous = existing["details"]
        if previous.get("host") != domain or normalized_words(previous.get("name")) != normalized_words(name):
            return "review", "The listing changed its identity evidence. The existing link is preserved."
        details = {**details, **{field: previous[field] for field in ("identity_evidence", "identity_review_url")
                                if field in previous}}
    else:
        reason = _listing_filter_reason(details, policy)
        if reason:
            return "staged", reason
        if not domain:
            return "review", "A valid organization website is required to promote this listing."
        if row["shared_host"]:
            return "review", "Distinct directory listings share this website or a related domain."
        account, conflict = _match_account(repository, domain, name)
        if conflict:
            return "review", conflict
        account_id = account["id"] if account else None

    if (account_id is not None and repository.account_blocked(account_id)) or (domain and repository.organization_blocked(domain)):
        return "suppressed", "The account or organization website matches an outreach restriction."
    return _apply_saved(repository, "directory", row, details, existing, account_id, name, domain, "Association", promote)


def evaluate_saved(repository: SourceRepository, source, row, policy, *, promote=False):
    """Preview or promote a row interpreted by an active saved-export reader."""
    if source == "cvent":
        return _cvent(repository, row, promote)
    if source == "directory":
        return _directory(repository, row, policy, promote)
    raise ValueError("This saved export has no CRM promotion policy.")


def _cell(value):
    return "" if value is None else str(value).strip()


def _contact(repository, payload):
    name = _cell(payload.get("contact_name"))
    original = _cell(payload.get("contact_email"))
    if not (name or original):
        return None, ""
    local, _, original_host = original.rpartition("@")
    host = normalize_dns_hostname(original_host)
    address = f"{local.casefold()}@{host}"
    if (not name or len(name) > 200 or len(address) > 254 or not local
            or any(char in local for char in " @\t\r\n") or organization_host(host) != host or not host):
        return None, "The contact needs a name and valid email before import."
    if repository.address_blocked(address):
        return None, "The contact matches an outreach restriction."
    return (name, _cell(payload.get("contact_title"))[:200], address), ""


def evaluate_intake(repository: SourceRepository, source, payload, *, preview=False):
    """Preview or promote a flat record from a file upload or a registered source."""
    name = _cell(payload.get("name"))
    domain = organization_host(_cell(payload.get("domain") or payload.get("website")))
    source_id = _cell(payload.get("source_id"))
    if not name or len(name) > 200 or not root_domain(domain) or len(source_id) > 200:
        return "invalid", "Provide an organization name and valid public website or domain."
    if repository.organization_blocked(domain):
        return "suppressed", "Domain is on the suppression list."
    if not source_id:
        source_id = hashlib.sha256((domain + "\0" + normalized_words(name)).encode()).hexdigest()
    evidence = repository.imported_record(source, source_id)
    if evidence is not None:
        previous = json.loads(evidence["raw"])
        previous_domain = organization_host(_cell(previous.get("domain") or previous.get("website")))
        if previous_domain != domain:
            return "review", "This source ID already belongs to a different account."
        account_id = evidence["account_id"]
    else:
        account, review = _match_account(repository, domain, name)
        if review:
            return "review", review
        account_id = account["id"] if account else None
    if account_id is not None and repository.account_blocked(account_id):
        return "suppressed", "Account is on the suppression list."

    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    if evidence is not None and evidence["raw"] == raw:
        return "unchanged", "Source record already imported."
    contact, contact_note = _contact(repository, payload)
    if preview:
        action = "Matches an existing account." if account_id else "Would create an account."
        return "staged", " ".join(part for part in (action, contact_note) if part)

    if account_id is None:
        kind = _cell(payload.get("organization_type"))[:60] or "Unknown"
        account_id = repository.create_account(name, domain, kind)
        if account_id is None:
            return "review", "An existing account identity conflicts with this source record."
    source_url = _cell(payload.get("source_url"))
    if not source_url.startswith(("https://", "http://")) or not organization_host(source_url):
        source_url = ""
    repository.save_imported_record(source, source_id, source_url, account_id, raw)
    if contact:
        repository.save_contact(account_id, contact)
    if contact_note:
        return "review", "Account evidence linked. " + contact_note
    return "imported", "Linked to the organization."
