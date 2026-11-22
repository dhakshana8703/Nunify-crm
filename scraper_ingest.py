"""Authenticated run and batch intake for external scrapers (R4-07)."""

import hashlib
import hmac
import json
from datetime import timedelta
from math import isfinite

from flask import Blueprint, abort, flash, g, jsonify, redirect, request, url_for

from source_ops import (
    MAX_COLUMNS, MAX_ROWS, MAX_UPLOAD, _active, _admin_db, _cell, _compact, _domain, _new_run, _normalized_name,
)
from workflow_store import audit, timestamp, transaction, utcnow
from workflow_web import db


bp = Blueprint("scraper_ingest", __name__)
API_ENDPOINTS = frozenset({
    "scraper_ingest.open_run", "scraper_ingest.submit_records",
    "scraper_ingest.complete_run", "scraper_ingest.get_run",
})
REQUESTS_PER_MINUTE = 60
MAX_BATCH_NUMBER = 1_000_000
SCHEMA = """
CREATE TABLE IF NOT EXISTS scraper_promotion_settings (
    source_slug TEXT PRIMARY KEY REFERENCES ingest_sources(slug),
    promotion TEXT NOT NULL CHECK (promotion IN ('preview', 'automatic'))
);
CREATE TABLE IF NOT EXISTS scraper_intake_runs (
    id INTEGER PRIMARY KEY,
    source_slug TEXT NOT NULL REFERENCES ingest_sources(slug),
    request_key TEXT NOT NULL,
    input_name TEXT NOT NULL,
    promotion TEXT NOT NULL CHECK (promotion IN ('preview', 'automatic')),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    source_run_id INTEGER UNIQUE REFERENCES source_runs(id),
    UNIQUE (source_slug, request_key)
);
CREATE TABLE IF NOT EXISTS scraper_intake_batches (
    run_id INTEGER NOT NULL REFERENCES scraper_intake_runs(id),
    batch_number INTEGER NOT NULL,
    request_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    PRIMARY KEY (run_id, batch_number),
    UNIQUE (run_id, request_key)
);
CREATE TABLE IF NOT EXISTS scraper_intake_records (
    run_id INTEGER NOT NULL REFERENCES scraper_intake_runs(id),
    identity TEXT NOT NULL,
    site_identity TEXT NOT NULL,
    batch_number INTEGER NOT NULL,
    record_number INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, identity)
);
CREATE INDEX IF NOT EXISTS scraper_identity_by_site ON scraper_intake_records(run_id, site_identity);
CREATE TABLE IF NOT EXISTS scraper_api_requests (
    id INTEGER PRIMARY KEY,
    source_slug TEXT NOT NULL,
    operation TEXT NOT NULL,
    credential_hash TEXT,
    occurred_at TEXT NOT NULL,
    status_code INTEGER
);
CREATE INDEX IF NOT EXISTS scraper_api_rate ON scraper_api_requests(credential_hash, occurred_at);
"""


def initialize(connection):
    connection.executescript(SCHEMA)


def get_promotion(connection, slug):
    row = connection.execute(
        "SELECT promotion FROM scraper_promotion_settings WHERE source_slug = ?", (slug,),
    ).fetchone()
    return row["promotion"] if row else "preview"


@bp.post("/sources/<slug>/promotion")
def set_promotion(slug):
    connection = _admin_db()
    promotion = request.form.get("promotion")
    if promotion not in {"preview", "automatic"}:
        abort(400, "Choose whether to review or automatically promote the completed runs.")
    with transaction(connection):
        if not connection.execute(
            "SELECT 1 FROM ingest_sources WHERE slug = ? AND adapter = 'external'", (slug,),
        ).fetchone():
            abort(404)
        connection.execute("""
            INSERT INTO scraper_promotion_settings(source_slug, promotion) VALUES (?, ?)
            ON CONFLICT(source_slug) DO UPDATE SET promotion = excluded.promotion
        """, (slug, promotion))
        audit(connection, g.crm_user["id"], None, "scraper_promotion_changed",
              {"source": slug, "promotion": promotion})
    flash("Scraper promotion setting updated.", "success")
    return redirect(url_for("source_ops.index"), code=303)


@bp.before_request
def authorize():
    if request.endpoint not in API_ENDPOINTS:
        return
    source = request.view_args["slug"]
    scheme, _, secret = request.headers.get("Authorization", "").partition(" ")
    candidate = hashlib.sha256(secret.encode()).hexdigest()
    connection = db()
    with transaction(connection):
        key = connection.execute(
            "SELECT key_hash FROM ingest_sources WHERE slug = ? AND adapter = 'external'", (source,),
        ).fetchone()
        authorized = (key is not None and scheme.casefold() == "bearer" and secret
                      and hmac.compare_digest(key["key_hash"] or "", candidate))
        observed = utcnow()
        credential_hash = candidate if authorized else None
        limited = authorized and connection.execute("""
            SELECT COUNT(*) FROM scraper_api_requests WHERE credential_hash = ? AND occurred_at > ?
                AND status_code IS NOT 429
        """, (candidate, timestamp(observed - timedelta(minutes=1)))).fetchone()[0] >= REQUESTS_PER_MINUTE
        status = 401 if not authorized else 429 if limited else None
        g.scraper_request_id = connection.execute("""
            INSERT INTO scraper_api_requests(source_slug, operation, credential_hash, occurred_at, status_code)
            VALUES (?, ?, ?, ?, ?)
        """, (source if key is not None else "(unknown source)", request.endpoint.rsplit(".", 1)[1],
              credential_hash, timestamp(observed), status)).lastrowid
    if not authorized:
        return jsonify(error="Source key is missing or invalid."), 401
    if limited:
        return jsonify(error="Source key request limit reached. Try again in a minute."), 429, {"Retry-After": "60"}
    g.scraper_key_hash = candidate


@bp.after_request
def record_status(response):
    request_id = g.get("scraper_request_id")
    if request_id is not None:
        connection = db()
        with transaction(connection):
            connection.execute("UPDATE scraper_api_requests SET status_code = ? WHERE id = ?",
                               (response.status_code, request_id))
    return response


def _key_is_current(connection, slug):
    return connection.execute(
        "SELECT 1 FROM ingest_sources WHERE slug = ? AND key_hash = ?", (slug, g.scraper_key_hash),
    ).fetchone() is not None


def _reject_json_constant(_value):
    raise ValueError("Nonfinite JSON number")


def _json_float(value):
    result = float(value)
    if not isfinite(result):
        raise ValueError("Nonfinite JSON number")
    return result


def _valid_text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _request_json(*, empty_allowed=False):
    raw = request.stream.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        abort(413, "The request is larger than 2 MiB.")
    if not raw and empty_allowed:
        return {}
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant, parse_float=_json_float)
    except (ValueError, UnicodeError, RecursionError):
        abort(400, "Send a valid JSON object.")
    if not isinstance(value, dict):
        abort(400, "Send a valid JSON object.")
    return value


def _request_key(body):
    value = body.get("idempotency_key")
    if not _valid_text(value, 120):
        abort(400, "Provide an idempotency_key of up to 120 characters.")
    return value


def _sequence(value, label):
    if value is not None and (type(value) is not int or not 1 <= value <= MAX_BATCH_NUMBER):
        abort(400, f"{label} must be an integer from 1 to {MAX_BATCH_NUMBER}.")
    return value


def _run(connection, slug, run_id):
    row = connection.execute(
        "SELECT * FROM scraper_intake_runs WHERE id = ? AND source_slug = ?", (run_id, slug),
    ).fetchone()
    if row is None:
        abort(404, "This run was not found for the source.")
    return row


def _summary(connection, run):
    batches = connection.execute("""
        SELECT COUNT(*) AS count, COALESCE(MAX(batch_number), 0) AS latest
        FROM scraper_intake_batches WHERE run_id = ?
    """, (run["id"],)).fetchone()
    records = connection.execute(
        "SELECT COUNT(*) FROM scraper_intake_records WHERE run_id = ?", (run["id"],),
    ).fetchone()[0]
    source_run = connection.execute(
        "SELECT state FROM source_runs WHERE id = ?", (run["source_run_id"],),
    ).fetchone()
    return {
        "run_id": run["id"], "source_run_id": run["source_run_id"],
        "state": source_run["state"] if source_run else "open", "promotion": run["promotion"],
        "received_batches": batches["count"], "highest_batch_number": batches["latest"], "staged_records": records,
    }


@bp.post("/api/ingest/<slug>/runs")
def open_run(slug):
    body = _request_json()
    key = _request_key(body)
    name = body.get("input_name", "External scraper")
    if not _valid_text(name, 120):
        abort(400, "Use a short input_name for the run.")
    name = name.strip()
    connection = db()
    with transaction(connection):
        if not _key_is_current(connection, slug):
            return jsonify(error="Source key is missing or invalid."), 401
        prior = connection.execute(
            "SELECT * FROM scraper_intake_runs WHERE source_slug = ? AND request_key = ?", (slug, key),
        ).fetchone()
        if prior:
            if prior["input_name"] != name:
                return jsonify(error="This idempotency key was used for a different input name."), 409
            return jsonify(_summary(connection, prior))
        run_id = connection.execute("""
            INSERT INTO scraper_intake_runs(source_slug, request_key, input_name, promotion, opened_at)
            VALUES (?, ?, ?, ?, ?)
        """, (slug, key, name, get_promotion(connection, slug), timestamp())).lastrowid
        return jsonify(_summary(connection, _run(connection, slug, run_id))), 201


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
    domain = _domain(_cell(record.get("domain") or record.get("website")))
    if not name or len(name) > 200 or not _normalized_name(name) or not domain:
        return "Provide an organization name and a valid public website or domain."
    if len(_cell(record.get("source_id"))) > 200:
        return "Use a source_id of up to 200 characters."
    return ""


def _stage_record(connection, run_id, record, batch_number, record_number):
    source_id = _cell(record.get("source_id"))
    domain = _domain(_cell(record.get("domain") or record.get("website")))
    site_identity = _compact([domain, _normalized_name(_cell(record.get("name")))])
    identity = f"id:{source_id}" if source_id else f"site:{site_identity}"
    existing = connection.execute("""
        SELECT * FROM scraper_intake_records WHERE run_id = ? AND identity = ?
    """, (run_id, identity)).fetchone()
    if existing is None:
        same_site = connection.execute("""
            SELECT * FROM scraper_intake_records WHERE run_id = ? AND site_identity = ?
            ORDER BY identity LIMIT 2
        """, (run_id, site_identity)).fetchall()
        if len(same_site) == 1 and (not source_id or same_site[0]["identity"].startswith("site:")):
            existing = same_site[0]
    if existing:
        if batch_number < existing["batch_number"]:
            if source_id and existing["identity"].startswith("site:"):
                latest = dict(json.loads(existing["payload_json"]), source_id=source_id)
                connection.execute("""
                    UPDATE scraper_intake_records SET identity = ?, payload_json = ?
                    WHERE run_id = ? AND identity = ?
                """, (identity, _compact(latest), run_id, existing["identity"]))
            return False
        if not source_id and existing["identity"].startswith("id:"):
            identity = existing["identity"]
            record = dict(record, source_id=identity.removeprefix("id:"))
        connection.execute("""
            UPDATE scraper_intake_records SET identity = ?, site_identity = ?, batch_number = ?,
                record_number = ?, payload_json = ? WHERE run_id = ? AND identity = ?
        """, (identity, site_identity, batch_number, record_number, _compact(record), run_id, existing["identity"]))
    else:
        connection.execute("""
            INSERT INTO scraper_intake_records(run_id, identity, site_identity, batch_number, record_number, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (run_id, identity, site_identity, batch_number, record_number, _compact(record)))
    return True


@bp.post("/api/ingest/<slug>/runs/<int:run_id>/records")
def submit_records(slug, run_id):
    body = _request_json()
    key = _request_key(body)
    requested_number = _sequence(body.get("batch_number"), "batch_number")
    rows = body.get("records")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        abort(400, "Provide 1 to 1,000 records.")
    try:
        digest = hashlib.sha256(_compact([requested_number, rows]).encode()).hexdigest()
    except (ValueError, RecursionError):
        abort(400, "Send records in a valid JSON array.")
    connection = db()
    with transaction(connection):
        if not _key_is_current(connection, slug):
            return jsonify(error="Source key is missing or invalid."), 401
        run = _run(connection, slug, run_id)
        prior = connection.execute("""
            SELECT * FROM scraper_intake_batches WHERE run_id = ? AND request_key = ?
        """, (run_id, key)).fetchone()
        if prior:
            if prior["request_hash"] != digest:
                return jsonify(error="This idempotency key was used for different batch data."), 409
            return jsonify(json.loads(prior["receipt_json"]))
        if run["source_run_id"] is not None:
            return jsonify(error="This run is already closed."), 409
        if requested_number is None:
            requested_number = connection.execute("""
                SELECT COALESCE(MAX(batch_number), 0) + 1 FROM scraper_intake_batches WHERE run_id = ?
            """, (run_id,)).fetchone()[0]
        if requested_number > MAX_BATCH_NUMBER or connection.execute("""
            SELECT 1 FROM scraper_intake_batches WHERE run_id = ? AND batch_number = ?
        """, (run_id, requested_number)).fetchone():
            return jsonify(error="This batch number is unavailable."), 409
        errors = []
        accepted = superseded = 0
        for index, record in enumerate(rows, 1):
            issue = _validate_record(record)
            if issue:
                errors.append({"record": index, "error": issue})
            elif _stage_record(connection, run_id, record, requested_number, index):
                accepted += 1
            else:
                superseded += 1
        receipt = {"run_id": run_id, "batch_number": requested_number, "accepted": accepted,
                   "superseded": superseded, "errors": errors}
        connection.execute("""
            INSERT INTO scraper_intake_batches(run_id, batch_number, request_key, request_hash, receipt_json)
            VALUES (?, ?, ?, ?, ?)
        """, (run_id, requested_number, key, digest, _compact(receipt)))
        return jsonify(receipt), 201


@bp.post("/api/ingest/<slug>/runs/<int:run_id>/complete")
def complete_run(slug, run_id):
    body = _request_json(empty_allowed=True)
    expected = _sequence(body.get("expected_batches"), "expected_batches")
    connection = db()
    with transaction(connection):
        if not _key_is_current(connection, slug):
            return jsonify(error="Source key is missing or invalid."), 401
        run = _run(connection, slug, run_id)
        summary = _summary(connection, run)
        if expected is not None and (summary["received_batches"], summary["highest_batch_number"]) != (expected, expected):
            return jsonify(error="The run does not have every expected batch.", **summary), 409
        if summary["source_run_id"] is not None:
            return jsonify(summary)
        if not summary["staged_records"]:
            return jsonify(error="This run has no valid staged records.", **summary), 409
        mode = run["promotion"]
        if mode == "automatic" and _active(connection, slug):
            return jsonify(error="A run for this source is already active.", **summary), 409
        state = "queued" if mode == "automatic" else "ready"
        source_run_id = _new_run(connection, slug, run["input_name"], "api", state)
        connection.execute("""
            INSERT INTO source_run_rows(run_id, row_number, payload_json)
            SELECT ?, ROW_NUMBER() OVER (ORDER BY batch_number, record_number, identity), payload_json
            FROM scraper_intake_records WHERE run_id = ?
        """, (source_run_id, run_id))
        connection.execute("""
            UPDATE scraper_intake_runs SET closed_at = ?, source_run_id = ? WHERE id = ?
        """, (timestamp(), source_run_id, run_id))
        return jsonify(dict(summary, state=state, source_run_id=source_run_id)), 202


@bp.get("/api/ingest/<slug>/runs/<int:run_id>")
def get_run(slug, run_id):
    connection = db()
    return jsonify(_summary(connection, _run(connection, slug, run_id)))
