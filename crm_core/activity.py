"""Record sales activity and its next action through a persistence port."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from crm_core.assignment import require_revision


OUTCOMES = {
    "call": {"no_answer", "voicemail", "connected", "wrong_number", "not_interested", "meeting_booked",
             "replied", "ghosted", "opted_out"},
    "email": {"sent", "replied", "bounced", "opted_out", "ghosted", "demo_requested", "pricing_requested"},
    "linkedin": {"connection_sent", "accepted", "message_sent", "replied", "opted_out",
                 "ghosted", "demo_requested", "pricing_requested"},
    "meeting": {"meeting_booked", "no_show"},
    "note": {"noted", "nurture"},
}
REPLIES = {"replied", "demo_requested", "pricing_requested"}
INBOUND = REPLIES | {"bounced", "opted_out", "accepted"}
STAGES_BEFORE_WORKING = {"discovered", "qualified", "enriched", "assigned", "nurture"}
STOP_OUTREACH = {"bounced", "opted_out"}
CONTACT_REQUIRED = STOP_OUTREACH | {"demo_requested", "pricing_requested"}
PERSON_STATUSES = {
    "connected": "connected", "not_interested": "not_interested", "wrong_number": "wrong_person",
    "bounced": "bounced", "opted_out": "opted_out",
}


class InvalidActivity(ValueError):
    pass


class ActivityConflict(ValueError):
    pass


class OutreachBlocked(ValueError):
    pass


@dataclass(frozen=True)
class ActivitySubmission:
    actor_id: int
    actor_timezone: str
    key: str
    revision: int | None
    channel: str
    outcome: str
    note: str = ""
    person_key: str = ""
    due_on: str = ""
    due_tomorrow: bool = False
    next_step: str = ""


@dataclass(frozen=True)
class ActivityChange:
    submission: ActivitySubmission
    request_hash: str
    account_id: int
    person_id: int | None
    saved_at: datetime
    direction: str
    stage: str | None
    person_status: str | None
    do_not_contact: bool
    opt_out_email: str | None
    due_on: str | None
    task_title: str | None
    task_owner_id: int


class ActivityStore(Protocol):
    def previous_activity(self, actor_id: int, key: str): ...

    def person_for(self, account_id: int, person_id: int): ...

    def restriction_for(self, account_id: int, person_id: int | None) -> str | None: ...

    def save(self, change: ActivityChange) -> None: ...


def activity_direction(channel, outcome):
    if outcome not in OUTCOMES.get(channel, ()):
        raise InvalidActivity("Choose an outcome for this channel.")
    if channel == "note" or outcome == "ghosted":
        return "internal"
    return "inbound" if outcome in INBOUND else "outbound"


def _due_date(submission, account, saved_at):
    local_day = saved_at.astimezone(ZoneInfo(account["owner_timezone"] or submission.actor_timezone)).date()
    if submission.due_tomorrow:
        return (local_day + timedelta(days=1)).isoformat()
    if submission.due_on:
        try:
            parsed = date.fromisoformat(submission.due_on)
            if submission.due_on == parsed.isoformat() and parsed >= local_day:
                return submission.due_on
        except ValueError:
            pass
        raise InvalidActivity("Choose today or a future date in your timezone.")
    if submission.next_step:
        raise InvalidActivity("Choose a due date for the next action.")
    return None


def contact_for_submission(store, account_id, person_key):
    if not person_key:
        return None
    message = "Choose a contact from this account."
    try:
        person_id = int(person_key)
    except ValueError as error:
        raise InvalidActivity(message) from error
    if not -(2**63) <= person_id < 2**63:
        raise InvalidActivity(message)
    person = store.person_for(account_id, person_id)
    if not person:
        raise InvalidActivity(message)
    return person


def _new_stage(stage, outcome, direction):
    if outcome == "nurture" and stage not in {"positive", "handed_off", "won"}:
        return "nurture"
    if (outcome in REPLIES or outcome == "connected") and stage in STAGES_BEFORE_WORKING | {"working"}:
        return "engaged"
    if direction == "outbound" and stage in STAGES_BEFORE_WORKING:
        return "working"
    return stage


def record_activity(store: ActivityStore, account, submission: ActivitySubmission, saved_at):
    direction = activity_direction(submission.channel, submission.outcome)
    account_id = account["id"]
    outcome = submission.outcome
    due_request = {"relative": "tomorrow"} if submission.due_tomorrow else submission.due_on
    fields = [account_id, submission.person_key, submission.channel, outcome, submission.note,
              due_request, submission.next_step]
    request_hash = hashlib.sha256(json.dumps(fields).encode()).hexdigest()
    existing = store.previous_activity(submission.actor_id, submission.key)
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise ActivityConflict("This form was used for another activity. Open a fresh form.")
        return False

    require_revision(account, submission.revision)
    due_on = _due_date(submission, account, saved_at)
    person = contact_for_submission(store, account_id, submission.person_key)
    if outcome in CONTACT_REQUIRED and person is None:
        raise InvalidActivity("Choose the contact for this outcome.")
    person_id = person["id"] if person else None
    reason = store.restriction_for(account_id, person_id)
    if reason and (direction == "outbound" or due_on):
        raise OutreachBlocked(f"Outreach is blocked. {reason}.")
    if due_on and outcome in STOP_OUTREACH:
        raise InvalidActivity("Remove the outreach follow-up for this contact.")

    person_status = None
    opt_out_email = None
    if person:
        if not person["do_not_contact"] or outcome in STOP_OUTREACH:
            person_status = PERSON_STATUSES.get(outcome, "replied" if outcome in REPLIES else None)
        if outcome == "opted_out":
            opt_out_email = person["email"] or None
    stage = _new_stage(account["stage"], outcome, direction)
    task_title = None
    if due_on:
        task_title = submission.next_step or (f"Follow up with {person['name']}" if person else "Follow up")
    store.save(ActivityChange(
        submission=submission, request_hash=request_hash, account_id=account_id, person_id=person_id,
        saved_at=saved_at, direction=direction, stage=stage if stage != account["stage"] else None,
        person_status=person_status, do_not_contact=outcome in STOP_OUTREACH, opt_out_email=opt_out_email,
        due_on=due_on, task_title=task_title, task_owner_id=account["owner_id"],
    ))
    return True
