"""Standalone follow-up and reply decisions, independent of HTTP and SQL."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from crm_core.activity import REPLIES, InvalidActivity, OutreachBlocked, contact_for_submission
from crm_core.assignment import require_revision


class InvalidTask(ValueError):
    pass


class TaskConflict(ValueError):
    pass


class MissingWork(ValueError):
    pass


@dataclass(frozen=True)
class TaskSubmission:
    actor_id: int
    actor_timezone: str
    revision: int | None
    key: str
    person_key: str
    title: str
    due_on: str


class TaskStore(Protocol):
    def previous_task(self, actor_id: int, key: str): ...

    def person_for(self, account_id: int, person_id: int): ...

    def restriction_for(self, account_id: int, person_id: int | None) -> str | None: ...

    def save_task(self, task: dict, occurred_at: datetime) -> None: ...

    def complete_task(self, task_id: int) -> None: ...

    def snooze_task(self, task_id: int, due_on: str) -> None: ...


def account_date(account, actor_timezone, occurred_at):
    return occurred_at.astimezone(ZoneInfo(account.get("owner_timezone") or actor_timezone)).date()


def valid_due_on(value, relative_to):
    try:
        parsed = date.fromisoformat(value)
        if value == parsed.isoformat() and parsed >= relative_to:
            return value
    except ValueError:
        pass
    raise InvalidTask("Choose today or a future date in your timezone.")


def create_task(store: TaskStore, account, submission: TaskSubmission, occurred_at):
    account_id = account["id"]
    request_hash = hashlib.sha256(json.dumps([
        account_id, submission.person_key, submission.title, submission.due_on,
    ]).encode()).hexdigest()
    earlier = store.previous_task(submission.actor_id, submission.key)
    if earlier is not None:
        if earlier["request_hash"] != request_hash:
            raise TaskConflict("This form was used for another follow-up. Open a fresh form.")
        return earlier["person_id"]

    require_revision(account, submission.revision)
    valid_due_on(submission.due_on, account_date(account, submission.actor_timezone, occurred_at))
    try:
        person = contact_for_submission(store, account_id, submission.person_key)
    except InvalidActivity as error:
        raise InvalidTask(str(error)) from error
    person_id = person["id"] if person else None
    reason = store.restriction_for(account_id, person_id)
    if reason:
        raise OutreachBlocked(f"Outreach is blocked. {reason}.")
    store.save_task({
        "account_id": account_id, "person_id": person_id, "owner_id": account["owner_id"],
        "title": submission.title, "due_on": submission.due_on, "actor_id": submission.actor_id,
        "key": submission.key, "request_hash": request_hash,
    }, occurred_at)
    return person_id


def find_task(store, task_id):
    task = store.task(task_id)
    if task is None:
        raise MissingWork()
    return task


def update_task(store: TaskStore, task, account, action, due_on, actor_timezone, occurred_at):
    if action == "complete":
        store.complete_task(task["id"])
        return
    if action != "snooze":
        raise InvalidTask("Choose a task action.")
    reason = store.restriction_for(account["id"], task["person_id"])
    if reason:
        raise OutreachBlocked(f"Outreach is blocked. {reason}.")
    today = account_date(account, actor_timezone, occurred_at)
    due_on = valid_due_on(due_on or (today + timedelta(days=1)).isoformat(), today)
    if task["status"] != "done":
        store.snooze_task(task["id"], due_on)


def find_reply(store, activity_id):
    activity = store.activity(activity_id)
    if activity is None or activity["outcome"] not in REPLIES:
        raise MissingWork()
    return activity


def acknowledge_reply(store, activity, actor_id, occurred_at):
    if activity["acknowledged_at"] is None:
        store.acknowledge_reply(activity["id"], actor_id, occurred_at)
