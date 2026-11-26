"""Contact editing, restrictions and provenance rules."""

import re
from datetime import datetime
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit

from crm_core.assignment import require_revision


CONTACT_STATUSES = (
    "not_contacted", "connected", "replied", "not_interested", "wrong_person", "do_not_contact",
)
CONTACT_FIELDS = {"name": 180, "title": 180, "email": 254, "phone": 64, "linkedin_url": 512, "persona": 24}
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class InvalidContact(ValueError):
    pass


class ContactMissing(ValueError):
    pass


class ContactRestricted(ValueError):
    pass


class ContactConflict(ValueError):
    pass


class ContactStore(Protocol):
    def person_for(self, account_id: int, person_id: int): ...

    def restriction_for(self, account_id: int, person_id: int | None = None, *, candidate_email: str | None = None) -> str | None: ...

    def add_contact(self, account_id: int, fields: dict, actor_id: int, occurred_at: datetime) -> int: ...

    def edit_contact(self, account_id: int, person_id: int, fields: dict, changed: list, actor_id: int, occurred_at: datetime) -> None: ...

    def save_status(self, account_id: int, contact: dict, status: str, blocked: bool, actor_id: int,
                    occurred_at: datetime) -> None: ...


def normalized_linkedin_profile(value):
    if not value:
        return ""
    try:
        url = urlsplit(value if "://" in value else "https://" + value)
        host = url.hostname or ""
        path = unquote(url.path, errors="strict")
        profile = re.fullmatch(r"/in/([\w-]+)/?", path)
        if (url.scheme in {"http", "https"} and not url.username and not url.password
                and url.port in {None, 80 if url.scheme == "http" else 443}
                and re.fullmatch(r"(?:www\.|[a-z]{2}\.)?linkedin\.com", host) and profile):
            return "https://www.linkedin.com/in/" + quote(profile[1].casefold(), safe="-")
    except (ValueError, UnicodeError):
        pass
    return None


def _fields(values, existing=None):
    fields = {}
    for key, maximum in CONTACT_FIELDS.items():
        if existing is not None and key not in values:
            fields[key] = existing[key]
            continue
        value = values.get(key, "").strip()
        if len(value) > maximum or (key == "name" and not value):
            raise InvalidContact(f"Enter a valid {key.replace('_', ' ')}.")
        fields[key] = value
    if fields["email"] and (existing is None or fields["email"] != existing["email"]):
        if not EMAIL.fullmatch(fields["email"]):
            raise InvalidContact("Enter a valid contact email.")
    if existing is None or fields["linkedin_url"] != existing["linkedin_url"]:
        profile = normalized_linkedin_profile(fields["linkedin_url"])
        if profile is None:
            raise InvalidContact("Enter a LinkedIn profile URL such as https://www.linkedin.com/in/name.")
        if len(profile) > 512:
            raise InvalidContact("Enter a shorter LinkedIn profile URL.")
        fields["linkedin_url"] = profile
    return fields


def email_locked(contact):
    return contact["origin"] != "manual" or bool(contact["unlocked"] or contact["email_verified"])


def find_contact(store: ContactStore, account_id, person_id):
    contact = store.person_for(account_id, person_id)
    if not contact:
        raise ContactMissing()
    return contact


def contact_for_view(store: ContactStore, account_id, person_id, include_suppressed):
    contact = find_contact(store, account_id, person_id)
    blocked = store.restriction_for(account_id, person_id)
    if blocked and not include_suppressed:
        raise ContactMissing()
    return contact, blocked


def _verify_change(store, account, contact, revision, details_revision):
    if store.restriction_for(account["id"], contact["id"]):
        raise ContactRestricted("Contact changes are restricted by suppression.")
    require_revision(account, revision)
    if details_revision != contact["details_revision"]:
        raise ContactConflict("The contact has changed. Refresh and retry.")


def add_contact(store: ContactStore, account, actor_id, revision, values, occurred_at):
    require_revision(account, revision)
    account_id = account["id"]
    if store.restriction_for(account_id):
        raise ContactRestricted("Contact additions are restricted for this account.")
    fields = _fields(values)
    if store.restriction_for(account_id, candidate_email=fields["email"]):
        raise ContactRestricted("Contact additions are restricted by suppression.")
    return store.add_contact(account_id, fields, actor_id, occurred_at)


def edit_contact(store: ContactStore, account, contact, actor_id, revision, details_revision, values, occurred_at):
    _verify_change(store, account, contact, revision, details_revision)
    fields = _fields(values, contact)
    if email_locked(contact) and fields["email"] != contact["email"]:
        raise ContactConflict("This contact's email is tied to saved verification or source evidence.")
    changed = [key for key, value in fields.items() if value != contact[key]]
    if contact["origin"] == "apollo" and not contact["unlocked"] and {"phone", "linkedin_url"}.intersection(changed):
        raise ContactConflict("This Apollo candidate is locked.")
    if changed:
        if store.restriction_for(account["id"], contact["id"], candidate_email=fields["email"]):
            raise ContactRestricted("Contact changes are restricted by suppression.")
        store.edit_contact(account["id"], contact["id"], fields, changed, actor_id, occurred_at)


def require_status(status):
    if status not in CONTACT_STATUSES:
        raise InvalidContact("Choose a contact status.")


def update_status(store: ContactStore, account, contact, actor_id, revision, details_revision, expected, status, occurred_at):
    require_status(status)
    _verify_change(store, account, contact, revision, details_revision)
    if expected != contact["status"]:
        raise ContactConflict("The outreach status has changed. Refresh and retry.")
    if contact["status"] != status:
        store.save_status(account["id"], contact, status, status == "do_not_contact", actor_id, occurred_at)


def contact_rows(store: ContactStore, accounts, people, include_suppressed):
    visible = []
    has_suppressed = False
    for person in people:
        account = accounts[person["account_id"]]
        blocked = account.get("blocked") or store.restriction_for(account["id"], person["id"])
        has_suppressed = has_suppressed or bool(blocked)
        if include_suppressed or not blocked:
            visible.append(dict(person, account_name=account["name"], blocked=blocked))
    return visible, has_suppressed


def can_handoff_contact(account, blocked, active_ae_exists):
    return bool(account["owner_id"] and not blocked and active_ae_exists())


def contact_history(store, account_id, person_id, blocked):
    if blocked:
        return [], [], []
    return (store.contact_timeline(account_id, person_id), store.contact_tasks(account_id, person_id),
            store.contact_evidence(account_id, person_id))
