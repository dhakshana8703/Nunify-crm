"""Review staged source evidence through a persistence port."""

import hashlib
import json
from typing import Protocol
from urllib.parse import unquote, urlsplit

from organization_identity import is_platform_domain, organization_host, organization_name_keys
from source_identity import cvent_event_id, cvent_url_matches_event, directory_listing_key


SOURCES = {"cvent": "Cvent", "directory": "Directory of Associations"}


class ReviewError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class ReviewRepository(Protocol):
    def review_row(self, run_id, row_number): ...
    def active_run(self, source): ...
    def has_newer_snapshot(self, source, run_id, source_id, payload_json): ...
    def existing_link(self, source, source_id): ...
    def account(self, account_id): ...
    def is_blocked(self, account_id): ...
    def save_link(self, source, source_id, account_id, details, payload): ...
    def save_decision(self, row, state, reason, evidence_url, account_id, actor_id, observed): ...
    def replay_decision(self, source, source_id): ...


def version(row):
    values = (row["run_id"], row["row_number"], row["payload_json"], row["result"], row["detail"], row["revision"])
    return hashlib.sha256(json.dumps(values).encode("utf-8")).hexdigest()


def identity(row, payload):
    details = payload["details"]
    if row["source_slug"] == "cvent":
        return details.get("organizer_company", ""), organization_host(payload.get("domain"))
    return details.get("name", ""), organization_host(details.get("host"))


def source_row_id(row, payload):
    if row["result"] == "invalid" or payload.get("error") or payload.get("duplicate") or payload.get("duplicate_id"):
        return ""
    source_url = payload.get("source_url", "")
    if row["source_slug"] == "directory":
        valid = directory_listing_key(source_url)
    else:
        valid = cvent_event_id(row["source_id"])
        if not cvent_url_matches_event(source_url, valid):
            return ""
    return valid if valid == row["source_id"] else ""


def _proof_host(evidence_url):
    try:
        url = urlsplit(evidence_url)
        if url.scheme == "https" and not url.username and not url.password and url.port in (None, 443):
            if "@" not in unquote(evidence_url):
                return organization_host(evidence_url)
    except ValueError:
        pass
    return ""


def reviewed_link(row, payload, account, evidence_url, corrected_name, *, blocked, linked):
    organizer, staged_host = identity(row, payload)
    account_host = organization_host(account["domain"])
    if not account_host or is_platform_domain(account_host) or _proof_host(evidence_url) != account_host:
        raise ReviewError(400, "Use a first-party HTTPS evidence link on the selected account's organization domain.")
    if blocked:
        raise ReviewError(409, "The selected account matches an outreach restriction.")
    if staged_host and staged_host != account_host:
        raise ReviewError(409, "The staged organization domain differs from the selected account domain.")
    account_name_keys = organization_name_keys(account["name"])
    if organization_name_keys(organizer).isdisjoint(account_name_keys):
        if payload.get("shared_host") or payload.get("shared_domain"):
            raise ReviewError(409, "Review the original evidence before linking a differently named organization on a shared domain.")
        if not corrected_name or organization_name_keys(corrected_name).isdisjoint(account_name_keys):
            raise ReviewError(400, "Enter the organization name shown on the first-party evidence when correcting a mismatch.")
    elif corrected_name and organization_name_keys(corrected_name).isdisjoint(account_name_keys):
        raise ReviewError(400, "The corrected organization name must match the selected account.")
    if linked:
        raise ReviewError(409, "There is already an account link for this source ID. The existing link is preserved.")
    details = dict(payload["details"], identity_evidence="Organization confirmed in source review",
                   identity_review_url=evidence_url)
    if row["source_slug"] == "cvent":
        details.update(organizer_company=corrected_name or organizer, organizer_domain=account_host)
    else:
        details.update(name=corrected_name or organizer, host=account_host)
    return details


def decide(store: ReviewRepository, run_id, row_number, state, reason, expected_version,
           evidence_url, corrected_name, account_id, actor_id, observed):
    if state not in {"held", "excluded", "linked"}:
        raise ReviewError(400, "Choose a review decision.")
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise ReviewError(400, "Enter a valid reason.")
    row = store.review_row(run_id, row_number)
    if row is None:
        raise ReviewError(404, "This row is unavailable for review.")
    if row["decision"] in {"excluded", "linked"} or expected_version != version(row):
        raise ReviewError(409, "This review changed. Reload the row before recording a decision.")
    source = row["source_slug"]
    if store.active_run(source):
        raise ReviewError(409, "Wait for the active source run to finish before recording a decision.")
    if not row["decision_key"].startswith("row:") and store.has_newer_snapshot(
        source, run_id, row["source_id"], row["payload_json"],
    ):
        raise ReviewError(409, "A newer source snapshot changed this record. Review the latest run.")
    if state == "linked":
        payload = json.loads(row["payload_json"])
        source_id = source_row_id(row, payload)
        if not source_id:
            raise ReviewError(409, "Correct the invalid or repeated source ID in a new source run before linking.")
        evidence_url, corrected_name = evidence_url.strip(), corrected_name.strip()
        if not evidence_url or len(evidence_url) > 500:
            raise ReviewError(400, "Enter a valid evidence url.")
        if len(corrected_name) > 200:
            raise ReviewError(400, "Enter a valid corrected name.")
        account = store.account(account_id)
        if not account:
            raise ReviewError(400, "Choose an existing account to link.")
        details = reviewed_link(row, payload, account, evidence_url, corrected_name,
                                blocked=store.is_blocked(account_id), linked=store.existing_link(source, source_id))
        store.save_link(source, source_id, account_id, details, payload)
    else:
        evidence_url, account_id = "", None
    store.save_decision(row, state, reason, evidence_url, account_id, actor_id, observed)


def replay_result(store: ReviewRepository, source, payload):
    """Retain manual exclusions and exact confirmed links through source replay."""
    source_id = payload.get("source_id")
    if (source not in SOURCES or not source_id or payload.get("error")
            or payload.get("duplicate") or payload.get("duplicate_id")):
        return None
    prior = store.replay_decision(source, source_id)
    if prior is None:
        return None
    if prior["state"] == "excluded":
        return "review", f"Excluded in source review. {prior['reason']}"
    if prior["linked_account_id"] == prior["account_id"] and json.loads(prior["payload_json"]) == payload:
        if store.is_blocked(prior["account_id"]):
            return "suppressed", "The confirmed account matches an outreach restriction."
        return "unchanged", "Previously linked in source review."
    return None
