"""Reconcile saved Cvent and Apollo contact evidence through CRM ports."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from crm_core.contacts import normalized_linkedin_profile
from crm_core.suppression import BLOCKED_CONTACT_STATUSES as BLOCKED_STATUSES
from crm_core.suppression import contact_has_restriction, saved_address_is_blocked as _blocked
from organization_identity import is_platform_domain as _platform
from organization_identity import matches_domain, organization_host
from organization_identity import organization_host as _host
from organization_identity import organization_name_keys as _names
from organization_identity import text_value as _text


ORIGIN = "cvent"
EMAIL_LOCAL = re.compile(r"[a-z0-9]+(?:[.!#$%&'*+/=?^_`{|}~-][a-z0-9]+)*", re.ASCII)
ROLE_WORDS = {
    "admin", "administrator", "billing", "board", "conference", "conferences", "contact", "customerservice",
    "donations", "donotreply", "enquiries", "events", "event", "help", "hello", "info", "inquiries", "mail",
    "marketing", "media", "membership", "noreply", "office", "planning", "postmaster", "reception",
    "registration", "registrations", "sales", "staff", "support", "team",
}
GENERIC_NAME_WORDS = ROLE_WORDS | {
    "association", "department", "first", "last", "name", "planner", "services", "unknown",
}
FREE_MAIL = {
    "aol.com", "gmail.com", "hotmail.com", "icloud.com", "live.com", "mail.com", "outlook.com",
    "proton.me", "protonmail.com", "yahoo.com",
}
@dataclass(frozen=True)
class SavedContact:
    name: str
    email: str
    account_id: int
    source_record_id: int


class SavedContactStore(Protocol):
    def active_admin(self, actor_id): ...

    def prepare_cvent(self): ...

    def linked_events(self): ...

    def aliases(self, account_ids): ...

    def rules(self): ...

    def contact_restrictions(self): ...

    def person_by_email(self, account_id, email): ...

    def link_exists(self, person_id, source_record_id): ...

    def create_cvent_contact(self, candidate): ...

    def link_event(self, person_id, source_record_id): ...

    def record_summary(self, actor_id, event, counts): ...


class SavedLeadStore(Protocol):
    def active_admin(self, actor_id): ...

    def prepare_apollo(self): ...

    def existing_people(self): ...

    def accounts(self): ...

    def aliases(self, account_ids): ...

    def saved_organizers(self): ...

    def rules(self): ...

    def mapped_leads(self): ...

    def record_summary(self, actor_id, event, counts): ...

    def create_apollo_lead(self, lead): ...

    def refresh_apollo_contact(self, person_id, fields): ...

    def record_unlock_time(self, apollo_id, unlocked_at): ...


def _event_id(value):
    if not isinstance(value, str):
        return ""
    try:
        return str(UUID(value.strip()))
    except ValueError:
        return ""


def _contact_name(value):
    if not isinstance(value, str) or len(value) > 120 or any(char in value for char in "\r\n\t"):
        return ""
    words = value.split()
    if not 2 <= len(words) <= 6:
        return ""
    if any(
        not any(char.isalpha() for char in word)
        or any(not (char.isalpha() or char in ".-'’") for char in word)
        or word.strip(".").casefold() in GENERIC_NAME_WORDS
        for word in words
    ):
        return ""
    return " ".join(words)


def _work_email(value):
    if not isinstance(value, str) or len(value) > 254:
        return ""
    email = value.strip().casefold()
    local, sep, host = email.partition("@")
    if not sep or len(local) > 64 or not EMAIL_LOCAL.fullmatch(local):
        return ""
    if organization_host(host) != host.removeprefix("www.") or not host.rpartition(".")[2].isalpha() or any(
        matches_domain(host, provider) for provider in FREE_MAIL
    ):
        return ""
    words = [re.sub(r"\d+$", "", part) for part in re.split(r"[._+-]", local)]
    if any(word in ROLE_WORDS for word in words) or "".join(words) in ROLE_WORDS:
        return ""
    return email


def _blocked_people(people):
    return {row["email"].casefold() for row in people if row["email"] and contact_has_restriction(row)}


def _candidates(rows, links, rules, blocked_people, aliases, counts):
    event_rows = defaultdict(list)
    for row in rows:
        event_id = _event_id(row.get("event_uuid")) if isinstance(row, dict) else ""
        if not event_id:
            counts["invalid_event_rows"] += 1
        elif event_id not in links:
            counts["unlinked_event_rows"] += 1
        else:
            event_rows[event_id].append(row)

    groups = defaultdict(list)
    for event_id, saved_rows in event_rows.items():
        if len(saved_rows) != 1:
            counts["duplicate_linked_event_rows"] += len(saved_rows)
            continue
        row = saved_rows[0]
        link = links[event_id]
        if row.get("cvent_url") != link["source_url"]:
            counts["mismatched_source_rows"] += 1
            continue
        name = _contact_name(row.get("contact_name"))
        observed_email = row.get("contact_email")
        email = _work_email(observed_email)
        if not name or not email:
            counts["ambiguous_contact_rows"] += 1
            continue
        account_host = organization_host(link["domain"])
        evidence = dict(link)
        organizer_host = organization_host(evidence.get("organizer_domain")) if evidence.get("identity_evidence") else ""
        hosts = {account_host, organizer_host} - {""}
        if _platform(organizer_host):
            hosts.discard(organizer_host)
        if not any(matches_domain(email.rpartition("@")[2], host) for host in hosts):
            counts["mismatched_contact_domain_rows"] += 1
            continue
        account_id = link["account_id"]
        if any(_blocked(rules, blocked_people, account_id, host, observed_email, aliases=aliases.get(account_id, ()))
               for host in hosts):
            counts["suppressed_contact_rows"] += 1
            continue
        groups[account_id, email].append(SavedContact(name, email, account_id, link["id"]))

    for group in groups.values():
        if len({candidate.name.casefold() for candidate in group}) != 1:
            counts["conflicting_contact_rows"] += len(group)
        else:
            yield group


def import_cvent_contacts(store: SavedContactStore, rows, *, apply=False, actor_id=None):
    counts = dict.fromkeys((
        "invalid_event_rows", "unlinked_event_rows", "duplicate_linked_event_rows",
        "mismatched_source_rows", "ambiguous_contact_rows", "mismatched_contact_domain_rows",
        "suppressed_contact_rows", "conflicting_contact_rows", "eligible_people",
        "new_people", "existing_people", "new_evidence", "existing_evidence",
    ), 0)
    counts["source_rows"] = len(rows)
    account_ids = set()
    if apply:
        if not store.active_admin(actor_id):
            raise PermissionError("An active administrator is required to apply a contact import.")
        store.prepare_cvent()
    links = {row["source_id"]: row for row in store.linked_events()}
    aliases = store.aliases({row["account_id"] for row in links.values()})
    candidates = _candidates(rows, links, store.rules(), _blocked_people(store.contact_restrictions()), aliases, counts)
    for group in candidates:
        candidate = group[0]
        account_ids.add(candidate.account_id)
        counts["eligible_people"] += 1
        person = store.person_by_email(candidate.account_id, candidate.email)
        person_id = person["id"] if person else None
        if person_id is None:
            counts["new_people"] += 1
            if apply:
                person_id = store.create_cvent_contact(candidate)
        else:
            counts["existing_people"] += 1
        for evidence in group:
            if person_id is not None and store.link_exists(person_id, evidence.source_record_id):
                counts["existing_evidence"] += 1
                continue
            counts["new_evidence"] += 1
            if apply:
                store.link_event(person_id, evidence.source_record_id)
    counts["eligible_accounts"] = len(account_ids)
    if apply and (counts["new_people"] or counts["new_evidence"]):
        store.record_summary(actor_id, "cvent_contacts_imported", counts)
    return counts


def _verified_emails(proofs):
    verified = set()
    for row in proofs:
        profile = row["profile"]
        email = _work_email(row["email"])
        if (email and isinstance(profile, dict) and profile.get("email_status") == "verified"
                and profile.get("id") == row["apollo_id"] and _work_email(profile.get("email")) == email):
            verified.add((row["apollo_id"], email))
    return verified


def _eligible(store, candidates, verified, blocked_people, counts):
    domains = defaultdict(set)
    account_hosts = defaultdict(set)
    for account in store.accounts():
        host = _host(account["domain"])
        if host:
            domains[host].add(account["id"])
            account_hosts[account["id"]].add(host)
    organizers = defaultdict(set)
    for account_id, details in store.saved_organizers():
        host = _host(details.get("organizer_domain"))
        if host and details.get("identity_evidence"):
            organizers[account_id, host].update(_names(details.get("organizer_company")))
            domains[host].add(account_id)

    company_hosts, host_companies = defaultdict(set), defaultdict(set)
    for row in candidates:
        host = _host(row["domain"])
        if host and row["company_id"]:
            company_hosts[row["company_id"]].add(host)
            host_companies[host].add(row["company_id"])
    rules = store.rules()
    aliases = store.aliases(account_hosts)

    for row in candidates:
        apollo_id, name, host = _text(row["apollo_id"]), _text(row["name"]), _host(row["domain"])
        company_id = row["company_id"]
        unlocked = row["is_unlocked"]
        if (not apollo_id or apollo_id != row["apollo_id"] or len(apollo_id) > 120 or not apollo_id.isprintable()
                or not isinstance(company_id, int) or company_id <= 0
                or not name or len(name) > 180 or not name.isprintable()
                or not any(char.isalpha() for char in name) or not host or unlocked not in (0, 1)):
            counts["invalid"] += 1
            continue
        if _platform(host):
            counts["suppressed"] += 1
            continue
        account_ids = domains.get(host, ())
        if (len(account_ids) != 1 or len(company_hosts[company_id]) != 1
                or len(host_companies[host]) != 1):
            counts["review_identity"] += 1
            continue
        account_id = next(iter(account_ids))
        if organizers[account_id, host].isdisjoint(_names(row["company_name"])):
            counts["review_identity"] += 1
            continue
        if any(_blocked(rules, blocked_people, account_id, account_host, _text(row["email"]),
                        aliases=aliases.get(account_id, ()))
               for account_host in {host, *account_hosts[account_id]}):
            counts["suppressed"] += 1
            continue

        email = _work_email(row["email"]) if unlocked else ""
        if email and _host(email.rpartition("@")[2]) != host:
            email = ""
        linkedin = normalized_linkedin_profile(_text(row["linkedin_url"])) if unlocked else ""
        yield {
            "apollo_id": apollo_id, "legacy_company_id": company_id, "company_domain": host,
            "account_id": account_id, "name": name, "title": _text(row["title"])[:180],
            "persona": _text(row["persona"])[:24], "email": email, "linkedin_url": linkedin or "",
            "unlocked": unlocked, "email_verified": int(bool(email and (apollo_id, email) in verified)),
            "discovered_at": row["discovered_at"], "unlocked_at": row["unlocked_at"] if unlocked else None,
        }


def _channels(lead):
    return [
        (lead["account_id"], field, lead[field].casefold())
        for field in ("email", "linkedin_url") if lead[field]
    ]


def import_saved_leads(store: SavedLeadStore, candidates, proofs, *, apply=False, actor_id=None):
    verified = _verified_emails(proofs)
    counts = dict.fromkeys((
        "invalid", "suppressed", "review_identity", "review_contact", "new_people", "updated_people",
        "existing_people", "linked_accounts", "importable_unlocked", "saved_emails", "verified_emails",
        "saved_linkedin",
    ), 0)
    counts["source_rows"] = len(candidates)
    counts["source_unlocked"] = sum(row["is_unlocked"] == 1 for row in candidates)
    accounts = set()
    if apply:
        if not store.active_admin(actor_id):
            raise PermissionError("An active administrator is required to apply a lead import.")
        store.prepare_apollo()
    existing = store.mapped_leads()
    people = list(store.existing_people())
    people_channels = {channel: row["id"] for row in people for channel in _channels(row)}
    leads = list(_eligible(store, candidates, verified, _blocked_people(people), counts))
    claims = Counter(channel for lead in leads for channel in _channels(lead))
    for lead in leads:
        linked = existing.get(lead["apollo_id"])
        person_id = linked["person_id"] if linked else None
        if linked and any(
            linked[field] != lead[field] for field in ("account_id", "legacy_company_id", "company_domain")
        ):
            counts["review_identity"] += 1
            continue
        if linked and contact_has_restriction(linked):
            counts["suppressed"] += 1
            continue
        if any(claims[channel] > 1 or people_channels.get(channel) not in (None, person_id)
               for channel in _channels(lead)):
            counts["review_contact"] += 1
            continue
        if linked and any(
            linked[field] and lead[field] and linked[field].casefold() != lead[field].casefold()
            for field in ("email", "linkedin_url")
        ):
            counts["review_contact"] += 1
            continue

        accounts.add(lead["account_id"])
        for metric, field in (
            ("importable_unlocked", "unlocked"), ("saved_emails", "email"),
            ("verified_emails", "email_verified"), ("saved_linkedin", "linkedin_url"),
        ):
            counts[metric] += bool(lead[field])
        if linked:
            updated = _refresh_link(store, linked, lead, apply=apply)
            counts["updated_people" if updated else "existing_people"] += 1
        else:
            counts["new_people"] += 1
            if apply:
                store.create_apollo_lead(lead)
    counts["linked_accounts"] = len(accounts)
    if apply and (counts["new_people"] or counts["updated_people"]):
        store.record_summary(actor_id, "cvent_apollo_leads_imported", counts)
    return counts


def _refresh_link(store, linked, lead, *, apply):
    fields = {
        "email": linked["email"] or lead["email"],
        "linkedin_url": linked["linkedin_url"] or lead["linkedin_url"],
        "unlocked": max(linked["unlocked"], lead["unlocked"]),
        "email_verified": max(linked["email_verified"], lead["email_verified"]),
    }
    changed = any(linked[field] != value for field, value in fields.items())
    unlocked_at = linked["unlocked_at"] or lead["unlocked_at"]
    if apply and changed:
        store.refresh_apollo_contact(linked["person_id"], fields)
    if apply and unlocked_at != linked["unlocked_at"]:
        store.record_unlock_time(lead["apollo_id"], unlocked_at)
    return changed or unlocked_at != linked["unlocked_at"]
