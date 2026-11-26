"""Campaign membership and sequencer event decisions."""

import hashlib
import json
from datetime import datetime, timezone
from typing import NamedTuple, Protocol


EVENT_OUTCOMES = {"sent": "sent", "replied": "replied", "bounced": "bounced", "unsubscribed": "opted_out"}
EVENT_TYPES = {*EVENT_OUTCOMES, "opened", "clicked"}
PLATFORMS = {"", "cvent", "unknown"}
PLATFORM_SIGNAL = "Registration platform - "
WORKABLE_STAGES = {"discovered", "qualified", "enriched", "assigned", "nurture"}


class CampaignError(Exception):
    def __init__(self, status, message=""):
        super().__init__(message)
        self.status = status


class EventPlan(NamedTuple):
    outcome: str | None
    direction: str | None
    pause: bool
    person_status: str | None
    stage: str | None
    suppression: str | None


class CampaignRepository(Protocol):
    def view(self, view_id): ...
    def create_view(self, name, account_type, platform, actor_id, occurred_at): ...
    def create_campaign(self, name, view_id, actor_id, occurred_at): ...
    def has_cvent_evidence(self, account_id): ...
    def qualification_signals(self, account_id): ...
    def is_blocked(self, account_id, person_id): ...
    def available_people(self, campaign_id): ...
    def insert_member(self, campaign_id, person_id, actor_id, occurred_at): ...
    def membership_exists(self, campaign_id, person_id): ...
    def event_hash(self, event_id): ...
    def event_member(self, member_id): ...
    def system_actor(self): ...
    def save_event(self, event_id, member_id, event_type, note, content_hash,
                   occurred_at, received_at, member, actor_id, plan): ...
    def audit(self, actor_id, account_id, action, details, occurred_at): ...


def require_campaign_access(user):
    if user["role"] not in {"admin", "manager", "sdr"}:
        raise CampaignError(403)


def save_view(store: CampaignRepository, name, account_type, platform, actor_id, occurred_at):
    if platform not in PLATFORMS:
        raise CampaignError(400, "Choose an available platform filter.")
    view_id = store.create_view(name, account_type, platform, actor_id, occurred_at)
    if view_id is None:
        raise CampaignError(409, "A saved view with this name already exists.")
    store.audit(actor_id, None, "sales_view_created", {"view_id": view_id}, occurred_at)
    return view_id


def save_campaign(store: CampaignRepository, name, view_id, actor_id, occurred_at):
    if view_id is not None and store.view(view_id) is None:
        raise CampaignError(404)
    campaign_id = store.create_campaign(name, view_id, actor_id, occurred_at)
    if campaign_id is None:
        raise CampaignError(409, "A campaign with this name already exists.")
    store.audit(actor_id, None, "campaign_created", {"campaign_id": campaign_id}, occurred_at)
    return campaign_id


def matches_view(store: CampaignRepository, account, view):
    if view is None:
        return True
    if view["organization_type"] and account["organization_type"].casefold() != view["organization_type"].casefold():
        return False
    if not view["platform"]:
        return True
    if store.has_cvent_evidence(account["id"]):
        return view["platform"] == "cvent"
    platforms = {signal["label"].removeprefix(PLATFORM_SIGNAL).casefold()
                 for signal in store.qualification_signals(account["id"])
                 if signal["label"].startswith(PLATFORM_SIGNAL)}
    return "cvent" in platforms if view["platform"] == "cvent" else not platforms


def eligible(person, account, *, blocked):
    return bool(person["unlocked"] and person["email_verified"] and person["email"]
                and account["stage"] != "handed_off" and not blocked)


def campaign_candidates(store: CampaignRepository, campaign_id, scope, view):
    candidates = []
    for person in store.available_people(campaign_id):
        account = scope.get(person["account_id"])
        if (account and account["owner_id"] and matches_view(store, account, view)
                and eligible(person, account, blocked=store.is_blocked(account["id"], person["id"]))):
            candidates.append(dict(person, account_name=account["name"]))
    return candidates


def enroll(store: CampaignRepository, campaign, person, account, view, actor_id, occurred_at):
    if not eligible(person, account, blocked=store.is_blocked(account["id"], person["id"])):
        raise CampaignError(403, "This contact is ineligible for the campaign.")
    if not matches_view(store, account, view):
        raise CampaignError(403, "This contact is ineligible for the campaign.")
    if store.insert_member(campaign["id"], person["id"], actor_id, occurred_at):
        store.audit(actor_id, account["id"], "campaign_member_added",
                    {"campaign_id": campaign["id"], "person_id": person["id"]}, occurred_at)
    elif not store.membership_exists(campaign["id"], person["id"]):
        raise CampaignError(409, "This contact is active in another campaign.")


def plan_sequence_event(event_type, person_status, account_stage, do_not_contact):
    outcome = EVENT_OUTCOMES.get(event_type)
    if event_type == "replied":
        status = "replied" if not do_not_contact and person_status not in {"positive", "handed_off"} else None
        stage = "engaged" if account_stage in WORKABLE_STAGES or account_stage == "working" else None
        return EventPlan(outcome, "inbound", True, status, stage, None)
    if event_type == "sent":
        stage = "working" if account_stage in WORKABLE_STAGES else None
        return EventPlan(outcome, "outbound", False, None, stage, None)
    if event_type in {"bounced", "unsubscribed"}:
        status = outcome if person_status != "opted_out" or outcome == "opted_out" else None
        reason = "opt_out" if outcome == "opted_out" else "bounced"
        return EventPlan(outcome, "inbound", True, status, None, reason)
    return EventPlan(None, None, False, None, None, None)


def record_sequence_event(store: CampaignRepository, event, received_at):
    event_id, member_id, event_type = (event.get(key) for key in ("event_id", "member_id", "type"))
    note = event.get("note", "")
    if (not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 180
            or type(member_id) is not int or not 0 < member_id < 2**63
            or not isinstance(event_type, str) or event_type not in EVENT_TYPES
            or not isinstance(note, str) or len(note) > 4000):
        raise CampaignError(400, "Event ID, member ID, or event type is invalid.")
    occurred_at = received_at
    if "occurred_at" in event:
        try:
            occurred_at = datetime.fromisoformat(event["occurred_at"])
            if occurred_at.utcoffset() is None:
                raise ValueError
            occurred_at.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError) as error:
            raise CampaignError(400, "Use an ISO 8601 event timestamp with a timezone.") from error
    content_hash = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
    previous = store.event_hash(event_id)
    if previous is not None:
        if previous != content_hash:
            raise CampaignError(409, "That event ID has already been used for another event.")
        return False
    member = store.event_member(member_id)
    if member is None:
        raise CampaignError(404, "Campaign member not found.")
    plan = plan_sequence_event(event_type, member["status"], member["stage"], member["do_not_contact"])
    actor_id = None
    if plan.outcome:
        actor = store.system_actor()
        if actor["active"]:
            raise CampaignError(409, "The reserved integration user is active. An admin must resolve the account conflict.")
        actor_id = actor["id"]
    store.save_event(event_id, member_id, event_type, note, content_hash,
                     occurred_at, received_at, member, actor_id, plan)
    return True
