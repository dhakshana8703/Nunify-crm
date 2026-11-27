"""Match and reconcile a reviewed Cvent charge statement."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from crm_core.credit_ledger import CREDIT_ACTIONS, OPENING_ADJUSTMENT, parse_credits


REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}")
IMMUTABLE_FIELDS = ("source", "reference", "action", "credits", "at", "on_day", "user_id", "person_id")


def credit_amount_text(amount):
    return format(Decimal(str(amount)).normalize(), "f")


def charge_reference(value):
    if not isinstance(value, str) or not REFERENCE.fullmatch(value):
        raise ValueError("Use a stable source reference of up to 120 letters, digits or separators.")
    return value


def charge_instant(value):
    if not isinstance(value, str):
        raise ValueError("Use an ISO date and time with an explicit UTC offset.")
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() is None:
        raise ValueError("Use an ISO date and time with an explicit UTC offset.")
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class HistoricalCharge:
    reference: str
    action: str
    credits: str
    at: str
    legacy_user_id: int | None
    cvent_company_id: int | None
    apollo_person_id: str | None

    @classmethod
    def verified(cls, reference, action, credits, at, legacy_user_id=None, cvent_company_id=None, apollo_person_id=None):
        amount = parse_credits(credits)
        if action not in CREDIT_ACTIONS or amount <= 0:
            raise ValueError("Each confirmed charge needs a supported Apollo action and a positive amount.")
        return cls(charge_reference(reference), action, credit_amount_text(amount), charge_instant(at),
                   legacy_user_id, cvent_company_id, apollo_person_id)


@dataclass(frozen=True)
class HistoricalStatement:
    reference: str
    total_credits: str
    as_of: str

    @classmethod
    def verified(cls, reference, total_credits, as_of):
        amount = parse_credits(total_credits)
        if amount < 0:
            raise ValueError("The verified statement total must be zero or positive.")
        return cls(charge_reference(reference), credit_amount_text(amount), charge_instant(as_of))


class CreditHistoryStore(Protocol):
    def history_import(self): ...
    def historical_postings(self): ...
    def mapped_user(self, email): ...
    def mapped_company(self, legacy_company_id): ...
    def mapped_person(self, apollo_id): ...
    def post(self, posting): ...
    def record_summary(self, actor_id, proof, observed): ...


def _fingerprint(details):
    data = json.dumps(details, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def _unique_charges(charges, cutoff):
    unique = {}
    for charge in charges:
        if charge.at > cutoff:
            raise ValueError("A confirmed charge is after the approved statement cutoff.")
        if unique.setdefault(charge.reference, charge) != charge:
            raise ValueError("The charge export contains conflicting rows with the same reference.")
    return sorted(unique.values(), key=lambda charge: charge.reference)


def _mapped_posting(store, charge, approved_users):
    user = store.mapped_user(approved_users.get(charge.legacy_user_id))
    account = store.mapped_company(charge.cvent_company_id)
    person = store.mapped_person(charge.apollo_person_id)
    if person and (
        (account and person["account_id"] != account)
        or (charge.cvent_company_id and not account and person["legacy_company_id"] != charge.cvent_company_id)
    ):
        raise ValueError(f"Charge {charge.reference} has conflicting company and Apollo person evidence.")
    instant = datetime.fromisoformat(charge.at)
    local_day = instant.astimezone(ZoneInfo(user["timezone"])).date() if user else instant.date()
    return {
        "source": "historical", "reference": charge.reference, "action": charge.action, "credits": charge.credits,
        "at": charge.at, "on_day": local_day.isoformat(), "user_id": user["id"] if user else None,
        "account_id": person["account_id"] if person else account, "person_id": person["id"] if person else None,
    }


def _opening_posting(statement, adjustment):
    return {
        "source": "adjustment", "reference": statement.reference, "action": OPENING_ADJUSTMENT,
        "credits": credit_amount_text(adjustment), "at": statement.as_of, "on_day": statement.as_of[:10],
        "user_id": None, "account_id": None, "person_id": None,
    }


def _postings_digest(postings):
    # A later account merge can repoint the account ID on an existing posting.
    ordered = sorted(postings, key=lambda row: (row["source"], row["reference"]))
    return _fingerprint([[row[field] for field in IMMUTABLE_FIELDS] for row in ordered])


def _plan(store: CreditHistoryStore, charges, statement, approved_users):
    unique = _unique_charges(charges, statement.as_of)
    # Ignore mappings for users outside this statement and exact repeated export rows.
    used_users = {charge.legacy_user_id for charge in unique}
    input_fingerprint = _fingerprint({
        "charges": [asdict(charge) for charge in unique], "statement": asdict(statement),
        "approved_users": {key: email for key, email in approved_users.items() if key in used_users},
    })
    confirmed = sum((Decimal(charge.credits) for charge in unique), Decimal(0))
    difference = parse_credits(Decimal(statement.total_credits) - confirmed)
    archived = store.history_import()
    existing = store.historical_postings()
    if archived:
        if archived["input_fingerprint"] != input_fingerprint:
            raise ValueError("The Cvent historical statement is already recorded with different evidence.")
        if _postings_digest(existing) != archived["postings_digest"]:
            raise ValueError("The recorded historical postings differ from their reconciliation audit.")
        postings = existing
        proof = archived
    else:
        if existing:
            raise ValueError("There are historical postings without a verified opening statement audit.")
        postings = [_mapped_posting(store, charge, approved_users) for charge in unique]
        if difference:
            postings.append(_opening_posting(statement, difference))
        proof = {
            "input_fingerprint": input_fingerprint,
            "fingerprint": _fingerprint({"input": input_fingerprint, "postings": postings}),
            "postings_digest": _postings_digest(postings), "statement_reference": statement.reference,
            "statement_as_of": statement.as_of, "statement_credits": statement.total_credits,
            "confirmed_credits": credit_amount_text(confirmed), "opening_adjustment": credit_amount_text(difference),
        }
    report = {
        "fingerprint": proof["fingerprint"], "statement_reference": proof["statement_reference"],
        "statement_as_of": proof["statement_as_of"], "statement_credits": proof["statement_credits"],
        "confirmed_credits": proof["confirmed_credits"], "opening_adjustment": proof["opening_adjustment"],
        "new_charges": 0 if archived else len(unique), "already_imported": len(unique) if archived else 0,
        "opening_already_recorded": archived is not None, "postings": postings,
    }
    return report, proof


def preview_credit_history(store, charges, statement, approved_users):
    return _plan(store, charges, statement, approved_users)[0]


def apply_credit_history(store, charges, statement, approved_users, *, expected, actor_id, observed):
    report, proof = _plan(store, charges, statement, approved_users)
    if expected != report["fingerprint"]:
        raise ValueError("The credit import preview changed. Review it again before applying.")
    if report["opening_already_recorded"]:
        return dict(report, written=0)
    for posting in report["postings"]:
        store.post(posting)
    store.record_summary(actor_id, proof, observed)
    return dict(report, written=len(report["postings"]))
