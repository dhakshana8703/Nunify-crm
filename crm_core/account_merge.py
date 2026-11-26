"""Plan and authorize merges before a persistence adapter moves any rows."""

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

from crm_core.access import STAFF
from organization_identity import organization_name_keys, root_domain


def fingerprint(value):
    content = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


def _contact_conflicts(people):
    affected = []
    for field, label in (("email", "email"), ("linkedin_url", "LinkedIn profile")):
        seen = defaultdict(set)
        for person in people:
            value = person.get(field, "").strip().casefold()
            if value:
                seen[value].add(person["account_id"])
        if any(len(accounts) > 1 for accounts in seen.values()):
            affected.append(f"Contacts on different accounts share a {label}. Resolve their identities before merging.")
    return affected


def _coverage_conflicts(coverages, observed):
    timestamp = observed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    active = [row for row in coverages if not row["cancelled_at"] and row["ends_at"] > timestamp]
    for index, left in enumerate(active):
        for right in active[index + 1:]:
            if (left["account_id"] != right["account_id"] and left["rep_id"] != right["rep_id"]
                    and left["starts_at"] < right["ends_at"] and right["starts_at"] < left["ends_at"]):
                return ["Coverage windows overlap for different reps. Resolve coverage before merging."]
    return []


def _history_conflicts(history, observed, survivor_id):
    conflicts = _contact_conflicts(history["people"])
    owners = sorted({row["owner_id"] for row in history["account_work"] if row["owner_id"] is not None})
    if len(owners) > 1:
        conflicts.append(f"Account owners differ (user IDs {', '.join(map(str, owners))}). Resolve assignments before merging.")
    overrides = {row["override"] for row in history["qualifications"] if row["override"] is not None}
    if len(overrides) > 1:
        conflicts.append("The accounts have conflicting qualification overrides. Review qualification before merging.")
    conflicts.extend(_coverage_conflicts(history["coverages"], observed))
    jobs = history["credit_jobs"]
    if any(job["state"] == "processing" for job in jobs):
        conflicts.append("An enrichment request is in progress. Let it finish before merging.")
    open_jobs = Counter((job["action"], job["person_id"]) for job in jobs if job["state"] in {"queued", "failed"})
    if any(count > 1 for count in open_jobs.values()):
        conflicts.append("The accounts have overlapping queued or retryable enrichment requests.")
    if any(job["account_id"] != survivor_id and job["state"] in {"queued", "failed"} and job["attempts"]
           for job in jobs):
        conflicts.append("An enrichment request on a duplicate account is awaiting provider reconciliation.")
    if any(job["status"] == "delivering" or (job["account_id"] != survivor_id and job["status"] != "delivered")
           for job in history["handoffs"]):
        conflicts.append("An unfinished handoff must be resolved before its account can be merged.")
    if any(job["state"] != "done" for job in history["qualification_job_accounts"]):
        conflicts.append("An unfinished qualification run references these accounts. Complete it before merging.")
    return conflicts


def _corroborated(accounts):
    names = [organization_name_keys(account["name"]) for account in accounts]
    return bool(set.intersection(*names))


def _current_history(history):
    allocations = history["account_work"]
    qualification = min(history["qualifications"],
                        key=lambda row: (row["override"] is None, row["account_id"]), default=None)
    if not allocations:
        return None, qualification
    allocation = min(allocations, key=lambda row: (row["owner_id"] is None, row["account_id"]))
    return dict(allocation, revision=max(row["revision"] for row in allocations) + 1), qualification


def plan_merge(store, ids, *, observed=None, automatic=False):
    """Preview an explicit selection, or a same-root group for the one-time command."""
    observed = observed or datetime.now(timezone.utc)
    selected = sorted(set(ids))
    if len(selected) < 2:
        raise ValueError("Choose at least two different accounts.")
    accounts = store.accounts(selected)
    if [account["id"] for account in accounts] != selected:
        raise LookupError("One or more selected accounts are no longer active.")
    survivor, *duplicates = accounts
    history = store.history(selected)
    conflicts = _history_conflicts(history, observed, survivor["id"])
    if automatic and not _corroborated(accounts):
        conflicts.append("The shared root has different organization names. An Admin must review the identity.")
    affected = {
        table: sorted(row.get("id", row.get("job_id", row["account_id"]))
                      for row in rows if row["account_id"] != survivor["id"])
        for table, rows in history.items()
    }
    counts = {table: len(ids) for table, ids in affected.items()}
    version = fingerprint({"accounts": accounts, "history": history, "automatic": automatic, "conflicts": conflicts})
    allocation, qualification = _current_history(history)
    return dict(survivor=survivor, duplicates=duplicates, counts=counts, affected_records=affected, conflicts=conflicts,
                fingerprint=version, automatic=automatic, allocation=allocation, qualification=qualification)


def plan_root_merges(store, *, observed=None):
    groups = defaultdict(list)
    for account in store.accounts():
        root = root_domain(account["domain"])
        if root:
            groups[root].append(account["id"])
    return [plan_merge(store, ids, observed=observed, automatic=True) for _, ids in sorted(groups.items()) if len(ids) > 1]


def apply_merge(store, plan, *, actor_id, observed):
    """Recheck the signed or CLI plan under the caller's writer transaction."""
    actor = store.actor(actor_id)
    if not actor or not actor["active"] or actor["role"] not in STAFF:
        raise PermissionError("Only an active Admin or Manager can merge accounts.")
    ids = [plan["survivor"]["id"], *(row["id"] for row in plan["duplicates"])]
    try:
        current = plan_merge(store, ids, observed=observed, automatic=plan.get("automatic", False))
    except LookupError as error:
        raise ValueError("The accounts changed since preview. Open a new preview.") from error
    if current["fingerprint"] != plan["fingerprint"]:
        raise ValueError("The accounts or their history changed since preview. Open a new preview.")
    if current["conflicts"]:
        raise ValueError("Resolve the merge conflicts before applying the preview.")
    return store.merge(current, actor_id=actor_id, observed=observed)
