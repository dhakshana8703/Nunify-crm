"""Visibility and business dates for confirmed Apollo credit reporting."""

from datetime import date
from decimal import Decimal
from typing import Protocol

from crm_core.access import STAFF
from crm_core.growth import local_day


CREDIT_VIEW_ROLES = STAFF | {"sdr"}


class CreditReportingStore(Protocol):
    def active_user(self, user_id): ...

    def user_total(self, user_id, day=None): ...

    def team_totals(self): ...


def credit_overview(store: CreditReportingStore, viewer):
    if not viewer or viewer["role"] not in CREDIT_VIEW_ROLES:
        return None
    if viewer["role"] == "sdr":
        return {"scope": "own", "total": store.user_total(viewer["id"])}
    totals = store.team_totals()
    return {
        "scope": "team", "total": sum((row["credits"] for row in totals), Decimal(0)),
        "users": [row for row in totals if row["user_id"] is not None],
        "unattributed": next((row["credits"] for row in totals if row["user_id"] is None), Decimal(0)),
    }


def daily_credits(store: CreditReportingStore, viewer, requested_id, requested_date, observed):
    if viewer["role"] not in CREDIT_VIEW_ROLES:
        raise PermissionError("This role cannot view Apollo credits.")
    if requested_id is None:
        user_id = viewer["id"]
    elif requested_id.isascii() and requested_id.isdecimal() and len(requested_id) <= 19:
        user_id = int(requested_id)
        if not 0 < user_id < 2**63:
            raise ValueError("Enter a valid user ID.")
    else:
        raise ValueError("Enter a valid user ID.")
    if user_id != viewer["id"] and viewer["role"] == "sdr":
        raise PermissionError("You can only view your own credits.")
    target = store.active_user(user_id)
    if target is None:
        raise LookupError("Active user was not found.")
    if user_id != viewer["id"] and viewer["role"] == "manager" and target["role"] != "sdr":
        raise PermissionError("Managers can view their own and SDR credits.")
    if requested_date is None:
        day = local_day(target, observed).isoformat()
    else:
        try:
            parsed = date.fromisoformat(requested_date)
        except ValueError:
            raise ValueError("Enter a date in YYYY-MM-DD format.") from None
        day = parsed.isoformat()
        if requested_date != day:
            raise ValueError("Enter a date in YYYY-MM-DD format.")
    return {"user_id": user_id, "date": day, "credits": store.user_total(user_id, day)}
