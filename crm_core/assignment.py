"""Choose account owners and allocation plans independently of transport and storage."""

import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class StaleAssignment(ValueError):
    pass


class AssignmentBlocked(ValueError):
    pass


class InvalidAssignment(ValueError):
    pass


class AllocationDenied(ValueError):
    pass


MAX_BULK_ASSIGNMENTS = 100
PRIMARY_SOURCES = {"cvent", "directory"}


@dataclass(frozen=True)
class AssignmentChange:
    account_id: int
    previous_owner_id: int | None
    owner_id: int
    actor_id: int
    stage: str
    revision: int
    changed_at: datetime
    method: str


class AssignmentStore(Protocol):
    def restriction_for(self, account_id: int) -> str | None: ...

    def save(self, change: AssignmentChange) -> None: ...


def require_revision(account, expected):
    if expected != account["revision"]:
        raise StaleAssignment("The account changed while this form was open. Reload and review the current assignment.")


def assign_account(store: AssignmentStore, account, actor_id, owner_id, changed_at, *, method="manual"):
    reason = store.restriction_for(account["id"])
    if reason:
        raise AssignmentBlocked(f"Assignment is blocked for this account. {reason}.")
    if owner_id == account["owner_id"]:
        return False

    store.save(AssignmentChange(
        account_id=account["id"],
        previous_owner_id=account["owner_id"],
        owner_id=owner_id,
        actor_id=actor_id,
        stage="assigned" if account["stage"] == "discovered" else account["stage"],
        revision=account["revision"] + 1,
        changed_at=changed_at,
        method=method,
    ))
    return True


def require_active_owner(store, owner_id):
    if not store.active_sdr(owner_id):
        raise InvalidAssignment("Choose an active SDR.")


def _selected_revisions(values):
    if len(values) > MAX_BULK_ASSIGNMENTS:
        raise InvalidAssignment("Select at most 100 accounts at a time.")
    revisions = {}
    for value in values:
        match = re.fullmatch(r"([1-9][0-9]{0,18}):([0-9]{1,19})", value)
        if not match:
            raise InvalidAssignment("Reload the assignment list and select the accounts again.")
        account_id, revision = map(int, match.groups())
        if max(account_id, revision) > 2**63 - 1:
            raise InvalidAssignment("Reload the assignment list and select the accounts again.")
        if revisions.setdefault(account_id, revision) != revision:
            raise StaleAssignment("A selected account changed. Reload the list and try again.")
    return revisions


def bulk_selection(individual, page):
    explicitly_selected = _selected_revisions(individual)
    page_selected = _selected_revisions(page)
    changes = {**page_selected, **explicitly_selected}
    if not changes or len(changes) > MAX_BULK_ASSIGNMENTS:
        raise InvalidAssignment("Select between 1 and 100 accounts.")
    if any(changes[account_id] != revision for account_id, revision in page_selected.items()):
        raise StaleAssignment("A selected account changed. Reload the list and try again.")
    return changes, explicitly_selected


def assign_selected(store: AssignmentStore, selection, explicit, allow_transfer, owner_id, actor_id, changed_at):
    accounts = []
    for account, revision in selection:
        require_revision(account, revision)
        if account["owner_id"] is not None and account["id"] not in explicit:
            raise StaleAssignment("An account on this page is already assigned. Reload and review the current assignments.")
        if account["owner_id"] not in (None, owner_id) and not allow_transfer:
            raise InvalidAssignment("Allow transfers to move individually selected accounts that have an owner.")
        accounts.append(account)
    return sum(assign_account(store, account, actor_id, owner_id, changed_at) for account in accounts)


def round_robin_plan(reps, selection, rep_ids, cap, restriction_for):
    if cap is None or cap < 1:
        raise InvalidAssignment("Enter a positive maximum total account count per SDR.")
    groups = [dict(row, accounts=[]) for row in reps if row["id"] in rep_ids]
    if len(groups) != len(rep_ids):
        raise InvalidAssignment("Choose active SDRs for the allocation.")
    groups.sort(key=lambda group: group["account_count"])
    rotation = deque(group for group in groups if group["account_count"] < cap)
    unallocated = 0
    for account, revision in selection:
        require_revision(account, revision)
        if account["owner_id"] is not None or restriction_for(account["id"]):
            raise AssignmentBlocked("Round-robin allocation requires unassigned accounts without outreach restrictions.")
        if not rotation:
            unallocated += 1
            continue
        group = rotation.popleft()
        group["accounts"].append(account)
        if group["account_count"] + len(group["accounts"]) < cap:
            rotation.append(group)
    snapshot = [
        [group["id"], group["account_count"], [account["id"] for account in group["accounts"]]]
        for group in groups
    ]
    return groups, unallocated, snapshot


def require_preview_owner(preview, actor_id):
    if preview["actor_id"] != actor_id:
        raise AllocationDenied()


def apply_round_robin(store: AssignmentStore, groups, snapshot, expected, actor_id, changed_at):
    if snapshot != expected:
        raise StaleAssignment("Rep workloads changed. Preview the round-robin allocation again.")
    for group in groups:
        for account in group["accounts"]:
            assign_account(store, account, actor_id, group["id"], changed_at, method="round_robin")


def pool_accounts(accounts):
    return [dict(row, relation="Unassigned") for row in accounts if not row["owner_id"] and not row["suppressed"]]


def assignment_rows(accounts, sources_by_account, scores_by_account, selected, source, owner, stage, organization_type):
    visible = []
    for account in accounts:
        assigned = bool(account["owner_id"])
        if selected == "assigned" and not assigned or selected == "unassigned" and assigned:
            continue
        if selected == "qualified_unassigned" and (assigned or account["stage"] != "qualified"):
            continue
        if owner and owner != (str(account["owner_id"]) if assigned else "unassigned"):
            continue
        if stage and stage != account["stage"]:
            continue
        if organization_type and organization_type != account["organization_type"]:
            continue
        evidence = sources_by_account.get(account["id"], ())
        sources = {item["slug"] for item in evidence}
        matches_source = {
            "all": True, "both": PRIMARY_SOURCES <= sources,
            "multi": len(sources) > 1, "other": bool(sources - PRIMARY_SOURCES),
        }.get(source, source in sources)
        if matches_source:
            visible.append(dict(account, evidence_labels=[item["name"] for item in evidence],
                                fit_score=scores_by_account.get(account["id"])))
    return visible
