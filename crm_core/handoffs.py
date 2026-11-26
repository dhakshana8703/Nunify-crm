"""Coverage and AE handoffs through a storage and delivery contract."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from crm_core.access import STAFF as MANAGEMENT

POSITIVE_OUTCOMES = {
    "demo_requested": "Demo requested",
    "pricing_requested": "Pricing requested",
    "meeting_booked": "Meeting booked",
}


class InvalidHandoff(ValueError):
    pass


class HandoffConflict(ValueError):
    pass


class HandoffRestricted(ValueError):
    pass


class HandoffMissing(ValueError):
    pass


class DeliveryFailure(Exception):
    """A delivery failure whose message can be shared with the manager."""


class DeliveryProvider(Protocol):
    def deliver(self, key: str, payload: dict) -> str: ...


class HandoffStore(Protocol):
    def atomic(self): ...
    def active_user(self, user_id, role): ...
    def coverage_overlaps(self, account_id, owner_id, starts_at, ends_at): ...
    def add_coverage(self, **details): ...
    def end_coverage(self, coverage_id, account_id, owner_id, observed): ...
    def existing_handoff(self, account_id, person_id): ...
    def contact(self, account_id, person_id): ...
    def account_contacts(self, account_id): ...
    def conversation(self, account_id, person_id): ...
    def blocked(self, account_id, person_id): ...
    def add_handoff(self, account_id, actor_id, request, payload, key, observed): ...
    def mark_positive(self, account_id, person_id, stage): ...
    def handoff_status(self, handoff_id): ...
    def requeue_handoff(self, handoff_id): ...
    def pending_handoff(self, at): ...
    def fail_before_delivery(self, handoff_id, reason): ...
    def claim_handoff(self, handoff_id, lease_id, lease_until): ...
    def complete_delivery(self, handoff_id, lease_id, status, receipt, error, at): ...
    def transition_delivery(self, account_id, person_id, previous, target): ...
    def audit(self, actor_id, account_id, action, details, observed): ...


@dataclass(frozen=True)
class HandoffRequest:
    person_id: int | None
    ae_id: int | None
    outcome: str
    context: str
    next_step: str
    meeting_at: str | None


def _utc_text(at):
    return at.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_local_time(value, viewer, *, required=True):
    if not value and not required:
        return None
    try:
        local = datetime.fromisoformat(value)
        if local.tzinfo is not None or "T" not in value:
            raise ValueError
    except ValueError:
        raise InvalidHandoff("Enter a valid local date and time.") from None
    zone = ZoneInfo(viewer["timezone"])
    earlier = local.replace(tzinfo=zone, fold=0)
    later = local.replace(tzinfo=zone, fold=1)
    if earlier.utcoffset() != later.utcoffset():
        raise InvalidHandoff("This local time changes with daylight saving. Select another time.")
    return _utc_text(earlier)


def require_revision(account, revision):
    if revision != account["revision"]:
        raise HandoffConflict("This account changed while the form was open. Reload and try again.")


def validate_coverage_window(starts_at, ends_at, observed):
    if ends_at <= starts_at or ends_at <= _utc_text(observed):
        raise InvalidHandoff("Coverage must end after its start and be scheduled into the future.")


def schedule_coverage(store: HandoffStore, account, revision, rep_id, starts_at, ends_at, actor_id, *, observed):
    validate_coverage_window(starts_at, ends_at, observed)
    require_revision(account, revision)
    owner_id, account_id = account["owner_id"], account["id"]
    if owner_id is None or owner_id == rep_id:
        raise InvalidHandoff("Choose an assigned account and a different rep to cover it.")
    if not store.active_user(rep_id, "sdr"):
        raise InvalidHandoff("Choose an active SDR for coverage.")
    if store.coverage_overlaps(account_id, owner_id, starts_at, ends_at):
        raise HandoffConflict("This account already has coverage during that window.")
    details = {"owner_id": owner_id, "rep_id": rep_id, "starts_at": starts_at, "ends_at": ends_at}
    coverage_id = store.add_coverage(account_id=account_id, actor_id=actor_id, **details)
    store.audit(actor_id, account_id, "coverage_created", {"coverage_id": coverage_id, **details}, observed)
    return coverage_id


def end_coverage(store: HandoffStore, account, revision, coverage_id, actor_id, *, observed):
    require_revision(account, revision)
    if not store.end_coverage(coverage_id, account["id"], account["owner_id"], observed):
        raise HandoffConflict("Coverage is no longer active or scheduled for this account.")
    store.audit(actor_id, account["id"], "coverage_cancelled", {"coverage_id": coverage_id}, observed)


def handoff_scope(user):
    if user["role"] not in MANAGEMENT | {"sdr", "ae"}:
        raise HandoffRestricted()
    return user["role"]


def require_handoff_requester(user):
    if user["role"] not in MANAGEMENT | {"sdr"}:
        raise HandoffRestricted()


def require_ae_visibility(handoff, user):
    if handoff["ae_id"] != user["id"] or handoff["status"] != "delivered":
        raise HandoffRestricted()


def reaching_contacts(store: HandoffStore, account_id):
    contacts = store.account_contacts(account_id)
    if store.blocked(account_id, None):
        return []
    return [contact for contact in contacts if not store.blocked(account_id, contact["id"])]


def prepare_handoff(person_id, ae_id, outcome, context, next_step, meeting, viewer):
    if outcome not in POSITIVE_OUTCOMES or not (1 <= len(context) <= 4000 and 1 <= len(next_step) <= 2000):
        raise InvalidHandoff("Enter a qualifying outcome, conversation context, and the agreed next step.")
    meeting_at = parse_local_time(meeting, viewer, required=outcome == "meeting_booked")
    return HandoffRequest(person_id, ae_id, outcome, context, next_step, meeting_at)


def queue_handoff(store: HandoffStore, account, revision, submission: HandoffRequest, actor_id, *, observed):
    account_id = account["id"]
    person_id, ae_id = submission.person_id, submission.ae_id
    existing = store.existing_handoff(account_id, person_id)
    if existing is not None:
        changed = any(existing[key] != getattr(submission, key)
                      for key in ("ae_id", "outcome", "context", "next_step", "meeting_at"))
        return existing["id"], changed
    require_revision(account, revision)
    person = store.contact(account_id, person_id)
    if person is None or not store.active_user(ae_id, "ae"):
        raise InvalidHandoff("Choose a contact from this account and an active receiving AE.")
    if submission.outcome == "meeting_booked" and person["persona"].strip().upper() not in {"P1", "P2"}:
        raise InvalidHandoff("A booked meeting qualifies for handoff with a P1 or P2 contact.")
    if store.blocked(account_id, person_id):
        raise HandoffRestricted("Outreach restrictions prevent handing off this contact.")
    payload = {
        "account": {"id": account_id, "name": account["name"]}, "contact": dict(person),
        "outcome": submission.outcome, "ae_id": ae_id, "meeting_at": submission.meeting_at,
        "context": submission.context, "next_step": submission.next_step,
        "conversation": [dict(row) for row in store.conversation(account_id, person_id)],
    }
    handoff_id = store.add_handoff(account_id, actor_id, submission, payload, uuid4().hex, observed)
    store.mark_positive(account_id, person_id, "positive" if account["stage"] != "handed_off" else None)
    store.audit(actor_id, account_id, "handoff_created", {
        "handoff_id": handoff_id, "person_id": person_id, "ae_id": ae_id, "outcome": submission.outcome,
    }, observed)
    return handoff_id, False


def requeue_failed_handoff(store: HandoffStore, handoff_id, actor_id, *, observed):
    handoff = store.handoff_status(handoff_id)
    if handoff is None:
        raise HandoffMissing()
    if handoff["status"] != "failed":
        return False
    store.requeue_handoff(handoff_id)
    store.audit(actor_id, handoff["account_id"], "handoff_requeued", {"handoff_id": handoff_id}, observed)
    return True


def deliver_pending_handoff(store: HandoffStore, provider: DeliveryProvider, *, clock, lease_seconds=60):
    """Claim a handoff and settle delivery under the same lease."""
    lease_id = uuid4().hex
    claim_at = clock()
    with store.atomic():
        handoff = store.pending_handoff(claim_at)
        if handoff is None:
            return None
        handoff_id = handoff["id"]
        if store.blocked(handoff["account_id"], handoff["person_id"]):
            store.fail_before_delivery(handoff_id, "Outreach became restricted. Review the account before retrying.")
            return {"id": handoff_id, "status": "failed"}
        store.claim_handoff(handoff_id, lease_id, claim_at + timedelta(seconds=lease_seconds))

    receipt = None
    error = None
    try:
        payload = handoff["payload"]
        receipt = provider.deliver(handoff["idempotency_key"], json.loads(payload) if isinstance(payload, str) else payload)
        if not isinstance(receipt, str) or not receipt.strip():
            raise DeliveryFailure("Delivery provider did not return a receipt.")
    except DeliveryFailure as exc:
        error = str(exc)[:500]
    except Exception:
        error = "Delivery failed. Check the configured integration and retry."

    status = "failed" if error is not None else "delivered"
    with store.atomic():
        changed = store.complete_delivery(handoff_id, lease_id, status, receipt if error is None else None,
                                          error, clock() if error is None else None)
        if not changed:
            return {"id": handoff_id, "status": "lease_lost"}
        if status == "delivered":
            store.transition_delivery(handoff["account_id"], handoff["person_id"], "positive", "handed_off")
    return {"id": handoff_id, "status": status}
