"""Review staged source evidence in Flask."""

import json

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from crm_core.source_review import SOURCES, ReviewError, decide as apply_decision, identity, source_row_id, version
from source_review_store import ReviewStore, initialize, replay_result
from workflow_store import transaction
from workflow_web import db, now, staff_required


bp = Blueprint("source_review", __name__)


@bp.get("/source-review")
def index():
    staff_required()
    store = ReviewStore(db())
    source = request.args.get("source", "")
    if source not in SOURCES:
        source = ""
    scope = request.args.get("state", "open")
    if scope not in {"all", "open", "held", "excluded", "linked"}:
        scope = "open"
    result = request.args.get("result", "")
    if result not in {"review", "invalid"}:
        result = ""
    search = request.args.get("search", "").strip().casefold()[:200]
    run_id = request.args.get("run_id", type=int)
    sort = request.args.get("sort", "newest")
    if sort not in {"newest", "oldest", "name"}:
        sort = "newest"
    size = request.args.get("page_size", 25, type=int)
    size = size if size in {25, 50, 100} else 25
    items, pagination = store.page(source, scope, result, search, run_id, sort, size,
                                   request.args.get("page", 1, type=int))
    return render_template("source_review.html", items=items, sources=SOURCES, selected=source, scope=scope,
                           result=result, search=search, sort=sort, run_id=run_id, pagination=pagination)


@bp.get("/source-review/<int:run_id>/<int:row_number>")
def detail(run_id, row_number):
    staff_required()
    store = ReviewStore(db())
    row = store.review_row(run_id, row_number)
    if row is None:
        abort(404, "This row is unavailable for review.")
    payload = json.loads(row["payload_json"])
    source_id = source_row_id(row, payload)
    existing = store.existing_link(row["source_slug"], source_id) if source_id else None
    organizer, host = identity(row, payload)
    search = request.args.get("account_search", "").strip()[:200]
    return render_template("source_review_detail.html", row=row, payload=payload, source_name=SOURCES[row["source_slug"]],
                           organizer=organizer, host=host, search=search, accounts=store.candidates(search, host, organizer),
                           version=version(row), existing=existing, can_link=bool(source_id) and not existing)


@bp.post("/source-review/<int:run_id>/<int:row_number>")
def decide(run_id, row_number):
    user = staff_required()
    state = request.form.get("decision")
    connection = db()
    try:
        with transaction(connection):
            apply_decision(ReviewStore(connection), run_id, row_number, state, request.form.get("reason", ""),
                           request.form.get("version"), request.form.get("evidence_url", ""),
                           request.form.get("corrected_name", ""), request.form.get("account_id", type=int),
                           user["id"], now())
    except ReviewError as error:
        abort(error.status, str(error))
    flash({"held": "Kept in review.", "excluded": "Excluded from source promotion.",
           "linked": "Source evidence linked to the existing account."}[state], "success")
    return redirect(url_for("source_review.detail", run_id=run_id, row_number=row_number), code=303)
