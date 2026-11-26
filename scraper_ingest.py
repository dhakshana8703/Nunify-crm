"""Authenticated HTTP intake for external scrapers (R4-07)."""

import json
from contextlib import contextmanager
from math import isfinite

from flask import Blueprint, abort, flash, g, jsonify, redirect, request, url_for

from crm_core.source_intake import (
    INVALID_KEY, MAX_UPLOAD, RATE_LIMIT, IntakeInputError, authorize_request, choose_promotion,
    complete_run as finish_intake, open_run as start_intake, run_summary, submit_records as add_batch,
)
from scraper_intake_store import ScraperIntakeStore, initialize
from source_ops import _admin_db
from workflow_store import transaction, utcnow
from workflow_web import db


bp = Blueprint("scraper_ingest", __name__)
API_ENDPOINTS = frozenset({
    "scraper_ingest.open_run", "scraper_ingest.submit_records",
    "scraper_ingest.complete_run", "scraper_ingest.get_run",
})
REQUESTS_PER_MINUTE = 60


def get_promotion(connection, slug):
    return ScraperIntakeStore(connection).promotion(slug)


@contextmanager
def _intake_write(connection):
    try:
        with transaction(connection):
            yield ScraperIntakeStore(connection)
    except IntakeInputError as error:
        abort(error.status, str(error) or None)


@bp.post("/sources/<slug>/promotion")
def set_promotion(slug):
    with _intake_write(_admin_db()) as store:
        choose_promotion(store, slug, request.form.get("promotion"), g.crm_user["id"])
    flash("Scraper promotion setting updated.", "success")
    return redirect(url_for("source_ops.index"), code=303)


@bp.before_request
def authorize():
    if request.endpoint not in API_ENDPOINTS:
        return
    source = request.view_args["slug"]
    scheme, _, secret = request.headers.get("Authorization", "").partition(" ")
    with _intake_write(db()) as store:
        request_id, credential_hash, status = authorize_request(
            store, source, request.endpoint.rsplit(".", 1)[1], scheme, secret, utcnow(), REQUESTS_PER_MINUTE,
        )
    g.scraper_request_id = request_id
    if status == 401:
        return jsonify(error=INVALID_KEY), 401
    if status == 429:
        return jsonify(error=RATE_LIMIT), 429, {"Retry-After": "60"}
    g.scraper_key_hash = credential_hash


@bp.after_request
def record_status(response):
    request_id = g.get("scraper_request_id")
    if request_id is not None:
        with _intake_write(db()) as store:
            store.log_response(request_id, response.status_code)
    return response


def _reject_json_constant(_value):
    raise ValueError("Nonfinite JSON number")


def _json_float(value):
    result = float(value)
    if not isfinite(result):
        raise ValueError("Nonfinite JSON number")
    return result


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


@bp.post("/api/ingest/<slug>/runs")
def open_run(slug):
    body = _request_json()
    with _intake_write(db()) as store:
        receipt, status = start_intake(store, slug, g.scraper_key_hash, body, utcnow())
    return jsonify(receipt), status


@bp.post("/api/ingest/<slug>/runs/<int:run_id>/records")
def submit_records(slug, run_id):
    body = _request_json()
    with _intake_write(db()) as store:
        receipt, status = add_batch(store, slug, run_id, g.scraper_key_hash, body)
    return jsonify(receipt), status


@bp.post("/api/ingest/<slug>/runs/<int:run_id>/complete")
def complete_run(slug, run_id):
    body = _request_json(empty_allowed=True)
    with _intake_write(db()) as store:
        receipt, status = finish_intake(store, slug, run_id, g.scraper_key_hash, body, utcnow())
    return jsonify(receipt), status


@bp.get("/api/ingest/<slug>/runs/<int:run_id>")
def get_run(slug, run_id):
    try:
        return jsonify(run_summary(ScraperIntakeStore(db()), slug, run_id))
    except IntakeInputError as error:
        abort(error.status, str(error))
