"""Decisions for staging, scheduling, requeueing and registering sources."""

import hashlib
import json
import re
from datetime import timedelta
from typing import Protocol

from source_schedule import calendar_schedule, next_calendar_run


MAX_ROWS = 1000
MAX_COLUMNS = 64
SLUG = re.compile(r"[a-z][a-z0-9-]{1,39}\Z")


class SourceRunConflict(ValueError):
    pass


class SourceRunStore(Protocol):
    def active(self, source): ...

    def stage(self, source, input_name, trigger, state="queued", **options): ...

    def stage_saved(self, source, file_name, policy, trigger): ...

    def save_policy(self, source, policy): ...

    def queue_again(self, run_id, promote, observed): ...

    def schedule(self, source, calendar, days, due_at): ...

    def connector_kind(self, source): ...

    def save_key(self, source, name, digest): ...

    def remove_key(self, source): ...

    def key_is_current(self, source, digest): ...

    def prior_request(self, source, key): ...

    def record(self, actor_id, action, details): ...


def require_source_admin(user):
    if user is None or user["role"] != "admin":
        raise PermissionError("Source operations require an administrator.")


def saved_connector(source, registered, attached):
    if source not in registered:
        raise LookupError(source)
    connector = attached.get(source)
    if connector is None:
        raise SourceRunConflict("This source connector is disconnected.")
    return connector


def connector_available(run, attached):
    return run["adapter"] in {"upload", "external"} or run["source_slug"] in attached


def listing_policy(defaults, minimum, categories):
    try:
        minimum = int(minimum)
        if not 1 <= minimum <= 100_000_000:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise ValueError("Set a membership minimum between 1 and 100 million.") from error
    policy = dict(defaults, min_members=minimum)
    for field, submitted in categories.items():
        if submitted is not None:
            terms = list(dict.fromkeys(term.strip() for term in submitted.split(",") if term.strip()))
            if len(terms) > 80 or any(len(term) > 80 for term in terms):
                raise ValueError("Provide up to 80 category terms of up to 80 characters.")
            policy[field] = terms
    if not policy["types"]:
        raise ValueError("Provide at least one listing type.")
    return policy


def stage_upload(store: SourceRunStore, filename, rows, actor_id):
    run_id = store.stage("upload", filename, "upload", "ready", rows=rows)
    store.record(actor_id, "source_staged", {"run_id": run_id, "rows": len(rows)})
    return run_id


def queue_saved_export(store: SourceRunStore, source, file_name, policy, actor_id, *, save_policy=False):
    active = store.active(source)
    if active:
        return active["id"], "A source run is already queued or in progress."
    if save_policy:
        store.save_policy(source, policy)
    run_id = store.stage_saved(source, file_name, policy, "manual")
    store.record(actor_id, "saved_export_queued", {"run_id": run_id, "source": source, "policy": policy})
    return run_id, None


def queue_run_again(store: SourceRunStore, run, attached, actor_id, observed):
    if not connector_available(run, attached):
        raise SourceRunConflict("This source connector is disconnected.")
    if run["state"] not in {"ready", "failed"}:
        return "This run has already been queued or processed."
    if store.active(run["source_slug"]):
        return "Another run for this source is queued or in progress."
    store.queue_again(run["id"], run["state"] == "ready", observed)
    store.record(actor_id, "source_run_queued", {"run_id": run["id"], "prior_state": run["state"]})
    return None


def save_schedule(store: SourceRunStore, source, actor_id, observed, *, cadence, days, zone, clock, day):
    settings = None
    due_at = None
    if cadence in {None, "interval"}:
        if days is None or not 0 <= days <= 365:
            raise ValueError("Enter an interval between 1 and 365 days, or 0 to pause the schedule.")
        if days:
            due_at = observed + timedelta(days=days)
    else:
        days = None
        if cadence != "paused":
            settings = calendar_schedule(cadence, zone, clock, day)
            due_at = next_calendar_run(settings, observed)
    scheduled = store.schedule(source, settings, days or None, due_at)
    store.record(actor_id, "source_schedule_changed", {
        "source": source, "calendar": settings, "interval_days": days or None, "next_run_at": scheduled,
    })


def register_key(store: SourceRunStore, source, name, digest, actor_id):
    if not (name and len(name) <= 100 and SLUG.fullmatch(source)):
        raise ValueError("Enter a name and a 2-40 character source ID using lowercase letters, numbers, and hyphens.")
    kind = store.connector_kind(source)
    if kind is not None and kind != "external":
        raise SourceRunConflict("This source ID is reserved for an installed connector.")
    store.save_key(source, name, digest)
    store.record(actor_id, "source_key_rotated", {"source": source})


def revoke_key(store: SourceRunStore, source, actor_id):
    if store.remove_key(source):
        store.record(actor_id, "source_key_revoked", {"source": source})


def legacy_request(body):
    rows = body.get("records") if isinstance(body, dict) else None
    key = body.get("idempotency_key") if isinstance(body, dict) else None
    if not (isinstance(key, str) and 1 <= len(key) <= 120 and isinstance(rows, list) and 1 <= len(rows) <= MAX_ROWS
            and all(isinstance(row, dict) and len(row) <= MAX_COLUMNS for row in rows)):
        raise ValueError("Provide an idempotency_key and up to 1000 record objects.")
    try:
        encoded = json.dumps(rows, ensure_ascii=True, sort_keys=True, default=str).encode()
        digest = hashlib.sha256(encoded).hexdigest()
    except (ValueError, RecursionError) as error:
        raise ValueError("Invalid record shape.") from error
    return key, rows, digest


def submit_legacy_batch(store: SourceRunStore, source, key_hash, key, digest, rows):
    if not store.key_is_current(source, key_hash):
        raise PermissionError("Source key is missing or invalid.")
    prior = store.prior_request(source, key)
    if prior:
        if prior["request_hash"] != digest:
            raise SourceRunConflict("This idempotency key was used for different data.")
        return prior["id"], prior["state"], False
    if store.active(source):
        raise SourceRunConflict("A run for this source is already active.")
    run_id = store.stage(source, "External API", "api", rows=enumerate(rows, 1), key=key, digest=digest)
    return run_id, "queued", True
