"""Select and prioritize the CRM's daily work queue."""

import calendar
from datetime import date, datetime, timedelta
from typing import Protocol

from crm_core.activity import REPLIES
from crm_core.tasks import account_date


class AgendaStore(Protocol):
    def due_tasks(self, account_id, cutoff): ...

    def coverage_for(self, account, current_time): ...

    def restriction_for(self, account_id, person_id=None): ...

    def pending_replies(self, account_id, outcomes): ...

    def activity_since(self, account_id, assigned_at): ...


def _offset_months(day, count):
    year, month = divmod(day.year * 12 + day.month - 1 + count, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def queue_items(store: AgendaStore, accounts, bucket, day, actor_timezone, current_time, min_months, max_months):
    rows = []
    start = _offset_months(day, min_months).isoformat()
    end = _offset_months(day, max_months).isoformat()
    for account in accounts:
        account_id = account["id"]
        task_day = account_date(account, actor_timezone, current_time) + timedelta(days=int(bucket == "tomorrow"))
        queue_iso = task_day.isoformat()
        base = {"account_id": account_id, "account_name": account["name"], "task_id": None, "reply_id": None}
        for task in store.due_tasks(account_id, task_day):
            if bucket == "tomorrow" and task["due_on"] != queue_iso:
                continue
            if account["relation"] == "Coverage":
                grant = store.coverage_for(account, current_time)
                if not grant or account_date(account, actor_timezone, datetime.fromisoformat(grant["ends_at"])) < task_day:
                    continue
            if store.restriction_for(account_id, task["person_id"]):
                continue
            overdue = task["due_on"] < queue_iso
            rows.append(dict(base, title=task["title"], due_on=task["due_on"], task_id=task["id"],
                             priority=1 if overdue else 2, reason="Overdue" if overdue else "Follow-up due"))
        if bucket == "tomorrow" or not account["owner_id"]:
            continue
        for reply in store.pending_replies(account_id, REPLIES):
            if reply["person_id"] and store.restriction_for(account_id, reply["person_id"]):
                continue
            rows.append(dict(base, outcome=reply["outcome"], contact_name=reply["name"], due_on=None,
                             reply_id=reply["id"], priority=0, reason="Unanswered reply"))
        assigned_at = account["assigned_at"]
        if assigned_at and not store.activity_since(account_id, assigned_at):
            rows.append(dict(base, title="Review account evidence", due_on=None, priority=3, reason="Newly assigned"))
        if account["next_event"] and start <= account["next_event"] <= end and not store.restriction_for(account_id):
            rows.append(dict(base, title="Review upcoming event", due_on=account["next_event"],
                             priority=4, reason="Event planning window"))
    return rows
