"""Plan reviewed Cvent ownership imports using stable source identity."""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Protocol

from crm_core.account_merge import fingerprint
from crm_core.assignment import AssignmentStore, StaleAssignment, assign_account


@dataclass(frozen=True, order=True)
class LegacyAllocation:
    company_id: int
    legacy_user_id: int
    legacy_assigned_at: str


class LegacyAllocationStore(AssignmentStore, Protocol):
    def legacy_company(self, company_id: int) -> dict | None: ...

    def sdr_id(self, email: str) -> int | None: ...

    def record_import(self, actor_id, allocations, version, observed): ...


def _status(account, owner_id, mapped, owners, restriction):
    if account is None:
        return "unlinked_company"
    if len(owners) > 1:
        return "conflict"
    if restriction:
        return "suppressed"
    if not mapped:
        return "unmapped_user"
    if owner_id is None:
        return "inactive_sdr"
    if account["owner_id"] == owner_id:
        return "already_current"
    if account["owner_id"] is not None:
        return "already_owned"
    return "ready"


def preview_allocations(store: LegacyAllocationStore, allocations, approved_users):
    rows = sorted(set(allocations))
    companies = {company_id: store.legacy_company(company_id) for company_id in {row.company_id for row in rows}}
    owners = {user_id: store.sdr_id(email) for user_id, email in approved_users.items()}
    intents = defaultdict(set)
    for row in rows:
        account = companies[row.company_id]
        if account is not None:
            owner_id = owners.get(row.legacy_user_id)
            intent = ("crm", owner_id) if owner_id is not None else ("legacy", row.legacy_user_id)
            intents[account["id"]].add(intent)

    planned = []
    for row in rows:
        account = companies[row.company_id]
        account_id = account["id"] if account is not None else None
        restriction = store.restriction_for(account_id) if account_id is not None else None
        owner_id = owners.get(row.legacy_user_id)
        planned.append({
            **asdict(row), "account_id": account_id, "account_name": account["name"] if account else None,
            "owner_id": owner_id, "current_owner_id": account["owner_id"] if account else None,
            "stage": account["stage"] if account else None, "revision": account["revision"] if account else None,
            "restriction": restriction,
            "status": _status(account, owner_id, row.legacy_user_id in approved_users, intents[account_id], restriction),
        })
    counts = dict(Counter(row["status"] for row in planned))
    return {"rows": planned, "counts": counts, "fingerprint": fingerprint([planned, approved_users])}


def apply_reviewed_allocations(store: LegacyAllocationStore, allocations, approved_users, *, expected, actor, observed):
    """Verify current eligibility before reusing the existing assignment transition."""
    if not actor or actor["role"] != "admin" or not actor["active"]:
        raise PermissionError("An active Admin sign-in is required to apply Cvent allocations.")
    plan = preview_allocations(store, allocations, approved_users)
    if plan["fingerprint"] != expected:
        raise StaleAssignment("The allocation plan changed. Run a new preview before applying.")

    applied_accounts = set()
    applied_rows = []
    for row in plan["rows"]:
        if row["status"] != "ready":
            continue
        account_id = row["account_id"]
        if account_id not in applied_accounts:
            account = {
                "id": account_id, "owner_id": row["current_owner_id"], "stage": row["stage"],
                "revision": row["revision"],
            }
            assign_account(store, account, actor["id"], row["owner_id"], observed, method="legacy_cvent")
            applied_accounts.add(account_id)
        applied_rows.append({key: row[key] for key in
                             ("account_id", "owner_id", "company_id", "legacy_user_id", "legacy_assigned_at")})

    if applied_rows:
        store.record_import(actor["id"], applied_rows, plan["fingerprint"], observed)
    return dict(plan, applied=len(applied_accounts))
