"""Plan authenticated external source intake without HTTP or SQL."""

import hashlib
import hmac
import json
from datetime import timedelta
from typing import Protocol

from crm_core.source_runs import MAX_COLUMNS, MAX_ROWS
from organization_identity import normalized_words, organization_host


MAX_UPLOAD = 2 * 1024 * 1024
MAX_BATCH_NUMBER = 1_000_000
INVALID_KEY = "Source key is missing or invalid."
RATE_LIMIT = "Source key request limit reached. Try again in a minute."


class IntakeInputError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class IntakeRepository(Protocol):
    def source_key(self, slug): ...
    def key_is_current(self, slug, credential_hash): ...
    def recent_requests(self, credential_hash, cutoff): ...
    def log_request(self, source, operation, credential_hash, observed, status): ...
    def promotion(self, slug): ...
    def store_promotion(self, slug, promotion, actor_id): ...
    def run(self, slug, run_id): ...
    def prior_run(self, slug, key): ...
    def create_run(self, slug, key, name, promotion, observed): ...
    def batch_counts(self, run_id): ...
    def staged_count(self, run_id): ...
    def source_state(self, source_run_id): ...
    def record(self, run_id, identity): ...
    def records_for_site(self, run_id, site_identity): ...
    def set_record_identity(self, run_id, prior_identity, identity, record): ...
    def write_record(self, run_id, prior_identity, identity, site_identity, batch_number, record_number, record): ...
    def prior_batch(self, run_id, key): ...
    def next_batch_number(self, run_id): ...
    def occupied_batch(self, run_id, batch_number): ...
    def store_receipt(self, run_id, batch_number, key, digest, receipt): ...
    def active_source_run(self, slug): ...
    def finish(self, run, state, observed): ...


def compact(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _cell(value):
    return "" if value is None else str(value).strip()


def _valid_text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _request_key(body):
    value = body.get("idempotency_key")
    if not _valid_text(value, 120):
        raise IntakeInputError("Provide an idempotency_key of up to 120 characters.")
    return value


def _sequence(value, label):
    if value is not None and (type(value) is not int or not 1 <= value <= MAX_BATCH_NUMBER):
        raise IntakeInputError(f"{label} must be an integer from 1 to {MAX_BATCH_NUMBER}.")
    return value


def choose_promotion(store: IntakeRepository, slug, promotion, actor_id):
    if promotion not in {"preview", "automatic"}:
        raise IntakeInputError("Choose whether to review or automatically promote the completed runs.")
    if store.source_key(slug) is None:
        raise IntakeInputError("", 404)
    store.store_promotion(slug, promotion, actor_id)


def authorize_request(store: IntakeRepository, source, operation, scheme, secret, observed, requests_per_minute):
    candidate = hashlib.sha256(secret.encode()).hexdigest()
    key = store.source_key(source)
    authorized = (key is not None and scheme.casefold() == "bearer" and secret
                  and hmac.compare_digest(key["key_hash"] or "", candidate))
    credential_hash = candidate if authorized else None
    limited = authorized and store.recent_requests(candidate, observed - timedelta(minutes=1)) >= requests_per_minute
    status = 401 if not authorized else 429 if limited else None
    request_id = store.log_request(source if key is not None else "(unknown source)", operation,
                                   credential_hash, observed, status)
    return request_id, credential_hash, status


def _run(store: IntakeRepository, slug, run_id):
    row = store.run(slug, run_id)
    if row is None:
        raise IntakeInputError("This run was not found for the source.", 404)
    return row


def _summary(store: IntakeRepository, run):
    batches = store.batch_counts(run["id"])
    source_run = store.source_state(run["source_run_id"])
    return {
        "run_id": run["id"], "source_run_id": run["source_run_id"],
        "state": source_run["state"] if source_run else "open", "promotion": run["promotion"],
        "received_batches": batches["count"], "highest_batch_number": batches["latest"],
        "staged_records": store.staged_count(run["id"]),
    }


def run_summary(store: IntakeRepository, slug, run_id):
    return _summary(store, _run(store, slug, run_id))


def open_run(store: IntakeRepository, slug, credential_hash, body, observed):
    key = _request_key(body)
    name = body.get("input_name", "External scraper")
    if not _valid_text(name, 120):
        raise IntakeInputError("Use a short input_name for the run.")
    name = name.strip()
    if not store.key_is_current(slug, credential_hash):
        return {"error": INVALID_KEY}, 401
    prior = store.prior_run(slug, key)
    if prior:
        if prior["input_name"] != name:
            return {"error": "This idempotency key was used for a different input name."}, 409
        return _summary(store, prior), 200
    run_id = store.create_run(slug, key, name, store.promotion(slug), observed)
    return run_summary(store, slug, run_id), 201


def _validate_record(record):
    if not isinstance(record, dict) or len(record) > MAX_COLUMNS:
        return "Provide a record object with up to 64 fields."
    if any(value is not None and not isinstance(value, (str, int, float, bool)) for value in record.values()):
        return "Record fields must be flat JSON values."
    try:
        for key, value in record.items():
            key.encode("utf-8")
            if isinstance(value, str):
                value.encode("utf-8")
    except UnicodeError:
        return "Use Unicode text in record fields."
    name = _cell(record.get("name"))
    domain = organization_host(_cell(record.get("domain") or record.get("website")))
    if not name or len(name) > 200 or not normalized_words(name) or not domain:
        return "Provide an organization name and a valid public website or domain."
    if len(_cell(record.get("source_id"))) > 200:
        return "Use a source_id of up to 200 characters."
    return ""


def stage_record(store: IntakeRepository, run_id, record, batch_number, record_number):
    source_id = _cell(record.get("source_id"))
    domain = organization_host(_cell(record.get("domain") or record.get("website")))
    site_identity = compact([domain, normalized_words(_cell(record.get("name")))])
    identity = f"id:{source_id}" if source_id else f"site:{site_identity}"
    existing = store.record(run_id, identity)
    if existing is None:
        same_site = store.records_for_site(run_id, site_identity)
        if len(same_site) == 1 and (not source_id or same_site[0]["identity"].startswith("site:")):
            existing = same_site[0]
    if existing and batch_number < existing["batch_number"]:
        if source_id and existing["identity"].startswith("site:"):
            latest = dict(json.loads(existing["payload_json"]), source_id=source_id)
            store.set_record_identity(run_id, existing["identity"], identity, latest)
        return False
    if existing and not source_id and existing["identity"].startswith("id:"):
        identity = existing["identity"]
        record = dict(record, source_id=identity.removeprefix("id:"))
    store.write_record(run_id, existing["identity"] if existing else None, identity,
                       site_identity, batch_number, record_number, record)
    return True


def submit_records(store: IntakeRepository, slug, run_id, credential_hash, body):
    key = _request_key(body)
    requested_number = _sequence(body.get("batch_number"), "batch_number")
    rows = body.get("records")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise IntakeInputError("Provide 1 to 1,000 records.")
    try:
        digest = hashlib.sha256(compact([requested_number, rows]).encode()).hexdigest()
    except (ValueError, RecursionError) as error:
        raise IntakeInputError("Send records in a valid JSON array.") from error
    if not store.key_is_current(slug, credential_hash):
        return {"error": INVALID_KEY}, 401
    run = _run(store, slug, run_id)
    prior = store.prior_batch(run_id, key)
    if prior:
        if prior["request_hash"] != digest:
            return {"error": "This idempotency key was used for different batch data."}, 409
        return json.loads(prior["receipt_json"]), 200
    if run["source_run_id"] is not None:
        return {"error": "This run is already closed."}, 409
    if requested_number is None:
        requested_number = store.next_batch_number(run_id)
    if requested_number > MAX_BATCH_NUMBER or store.occupied_batch(run_id, requested_number):
        return {"error": "This batch number is unavailable."}, 409
    errors = []
    accepted = superseded = 0
    for index, record in enumerate(rows, 1):
        issue = _validate_record(record)
        if issue:
            errors.append({"record": index, "error": issue})
        elif stage_record(store, run_id, record, requested_number, index):
            accepted += 1
        else:
            superseded += 1
    receipt = {"run_id": run_id, "batch_number": requested_number, "accepted": accepted,
               "superseded": superseded, "errors": errors}
    store.store_receipt(run_id, requested_number, key, digest, receipt)
    return receipt, 201


def complete_run(store: IntakeRepository, slug, run_id, credential_hash, body, observed):
    expected = _sequence(body.get("expected_batches"), "expected_batches")
    if not store.key_is_current(slug, credential_hash):
        return {"error": INVALID_KEY}, 401
    run = _run(store, slug, run_id)
    summary = _summary(store, run)
    if expected is not None and (summary["received_batches"], summary["highest_batch_number"]) != (expected, expected):
        return dict(summary, error="The run does not have every expected batch."), 409
    if summary["source_run_id"] is not None:
        return summary, 200
    if not summary["staged_records"]:
        return dict(summary, error="This run has no valid staged records."), 409
    automatic = run["promotion"] == "automatic"
    if automatic and store.active_source_run(slug):
        return dict(summary, error="A run for this source is already active."), 409
    state = "queued" if automatic else "ready"
    source_run_id = store.finish(run, state, observed)
    return dict(summary, state=state, source_run_id=source_run_id), 202
