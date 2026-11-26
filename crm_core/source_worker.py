"""Claim and process staged source runs through storage and reader ports."""

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Protocol

from crm_core.ingest import SourceRepository, evaluate_intake, evaluate_saved
from source_schedule import next_calendar_run


LEASE_DURATION = timedelta(minutes=5)
SCHEDULED_RETRY_DELAYS = (60, 120, 240)
SAVED_EXPORT_BATCH_SIZE = 500
REVIEW_RESULTS = {"invalid", "review", "suppressed"}


@dataclass(frozen=True)
class SourceReader:
    input_name: str
    default_policy: dict
    read: Callable[[], list]


class WorkerRepository(Protocol):
    intake: SourceRepository

    def transaction(self): ...
    def expired_runs(self, slugs, observed): ...
    def due_sources(self, observed): ...
    def active_run(self, slug): ...
    def saved_policy(self, slug, default_policy): ...
    def queue_saved(self, slug, input_name, policy): ...
    def schedule_at(self, slug, next_at): ...
    def next_run(self, slugs, observed): ...
    def claim(self, run_id, observed, lease_until): ...
    def owns(self, run): ...
    def health_event(self, source, run_id, kind, message, observed): ...
    def retry(self, run_id, retry_after, message): ...
    def finish(self, run_id, state, observed, *, error=None): ...
    def saved_run(self, run_id): ...
    def has_staged(self, run_id): ...
    def stage(self, run_id, rows): ...
    def rows(self, run_id, *, after=None, limit=None): ...
    def save_results(self, results): ...
    def has_review_results(self, run_id): ...
    def replay(self, source, payload): ...
    def remember_progress(self, run_id, cursor, lease_until): ...
    def count_rows(self, run_id): ...
    def recent_row_counts(self, run): ...


def fail_run(store: WorkerRepository, run, message, *, observed, retryable):
    retry_count = run["retry_count"]
    if run["trigger"] == "scheduled" and retryable and retry_count < len(SCHEDULED_RETRY_DELAYS):
        retry_after = observed + timedelta(seconds=SCHEDULED_RETRY_DELAYS[retry_count])
        store.retry(run["id"], retry_after, message)
    else:
        store.finish(run["id"], "failed", observed, error=message)
        store.health_event(run["source_slug"], run["id"], "failed", message, observed)


def _claim(store: WorkerRepository, readers, observed):
    slugs = tuple(readers)
    with store.transaction():
        for run in store.expired_runs(slugs, observed):
            fail_run(store, run, "Worker stopped before finishing. Retry this run.", observed=observed, retryable=True)
        for source in store.due_sources(observed):
            slug = source["slug"]
            reader = readers.get(slug)
            if reader is None:
                continue
            active = store.active_run(slug)
            if active:
                store.health_event(slug, active["id"], "skipped",
                                   "Scheduled run skipped because another run is still active.", observed)
            else:
                store.queue_saved(slug, reader.input_name, store.saved_policy(slug, reader.default_policy))
            next_at = (next_calendar_run(source, observed) if source["cadence"]
                       else observed + timedelta(days=source["schedule_days"]))
            store.schedule_at(slug, next_at)
        run = store.next_run(slugs, observed)
        if run is None:
            return None
        store.claim(run["id"], observed, observed + LEASE_DURATION)
        return dict(run, attempts=run["attempts"] + 1)


def _observe_yield(store: WorkerRepository, run, clock):
    totals = store.recent_row_counts(run)
    count = store.count_rows(run["id"])
    if totals and count < 0.30 * (sum(totals) / len(totals)):
        store.health_event(run["source_slug"], run["id"], "low_yield",
                           f"The run collected {count} rows, below 30% of its recent average.", clock())


def _process_saved(store: WorkerRepository, run, config, reader, clock, evaluator, batch_size):
    run_id = run["id"]
    promote = config["phase"] == "promote"
    policy = json.loads(config["policy_json"])
    if not store.has_staged(run_id):
        if promote:
            raise ValueError("The preview has no staged rows. Start a new preview.")
        records = reader.read()
        if not records:
            raise ValueError("The saved export did not contain any records.")
        with store.transaction():
            if not store.owns(run):
                return
            store.stage(run_id, enumerate(records, 1))
    cursor = config["last_row"]
    while True:
        with store.transaction():
            if not store.owns(run):
                return
            rows = store.rows(run_id, after=cursor, limit=batch_size)
            if not rows:
                if promote:
                    state = "partial" if store.has_review_results(run_id) else "complete"
                else:
                    state = "ready"
                    _observe_yield(store, run, clock)
                store.finish(run_id, state, clock())
                return
            changes = []
            for row in rows:
                payload = json.loads(row["payload_json"])
                decision = store.replay(run["adapter"], payload)
                result, detail = decision or evaluator(store.intake, run["adapter"], payload, policy, promote=promote)
                changes.append((result, detail, run_id, row["row_number"]))
            store.save_results(changes)
            cursor = rows[-1]["row_number"]
            store.remember_progress(run_id, cursor, clock() + LEASE_DURATION)


def _process_intake(store: WorkerRepository, run, clock):
    with store.transaction():
        if not store.owns(run):
            return
        rows = store.rows(run["id"])
        if not rows:
            raise ValueError("The source did not return any rows.")
        changes = []
        for row in rows:
            result, detail = evaluate_intake(store.intake, run["source_slug"], json.loads(row["payload_json"]))
            changes.append((result, detail, run["id"], row["row_number"]))
        store.save_results(changes)
        state = "partial" if any(result in REVIEW_RESULTS for result, *_ in changes) else "complete"
        store.finish(run["id"], state, clock())


def process_next(store: WorkerRepository, readers, clock, *, saved_evaluator=evaluate_saved,
                 batch_size=SAVED_EXPORT_BATCH_SIZE, storage_errors=()):
    run = _claim(store, readers, clock())
    if run is None:
        return None
    retryable = (OSError, RuntimeError, *storage_errors)
    try:
        reader = readers.get(run["source_slug"])
        if reader is None:
            _process_intake(store, run, clock)
        else:
            config = store.saved_run(run["id"])
            if config is None:
                raise ValueError("This saved-file run predates full export staging. Start a new preview in Sources.")
            _process_saved(store, run, config, reader, clock, saved_evaluator, batch_size)
    except (ValueError, KeyError, *retryable) as error:
        message = str(error) if isinstance(error, ValueError) else f"{type(error).__name__} while reading source data."
        with store.transaction():
            if store.owns(run):
                fail_run(store, run, message[:240], observed=clock(), retryable=isinstance(error, retryable))
    return run["id"]
