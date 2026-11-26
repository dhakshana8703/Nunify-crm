"""Flask adapters for temporary rep coverage and durable AE handoffs."""

import json
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from crm_core.handoffs import (
    MANAGEMENT, POSITIVE_OUTCOMES, DeliveryFailure, HandoffConflict, HandoffMissing, HandoffRestricted,
    InvalidHandoff, deliver_pending_handoff, end_coverage, handoff_scope, parse_local_time, prepare_handoff,
    queue_handoff, reaching_contacts, requeue_failed_handoff, require_ae_visibility, require_handoff_requester,
    schedule_coverage, validate_coverage_window,
)
from handoff_store import OfflineAEProvider, SqlHandoffStore, initialize
from workflow_store import timestamp, utcnow
from workflow_web import account_required, db, now as request_now, staff_required

bp = Blueprint("sales_extensions", __name__)


def _display_local(value, viewer):
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(ZoneInfo(viewer["timezone"])).strftime("%b %d, %Y %H:%M %Z")


@contextmanager
def _errors():
    try:
        yield
    except InvalidHandoff as error:
        abort(400, description=str(error))
    except HandoffConflict as error:
        abort(409, description=str(error))
    except HandoffRestricted as error:
        abort(403, description=str(error) or None)
    except HandoffMissing:
        abort(404)


def _manage_user():
    return staff_required()


def _handoff_user():
    user = g.get("crm_user")
    if user is None:
        abort(401)
    return user


def account_context(connection, account_id, user):
    store = SqlHandoffStore(connection)
    at = timestamp(request_now())
    coverages = store.account_coverages(account_id, at)
    return {
        "manage": user["role"] in MANAGEMENT,
        "rep_options": store.users("sdr"),
        "ae_options": store.users("ae"),
        "contacts": reaching_contacts(store, account_id),
        "coverages": [dict(row, active=row["starts_at"] <= at, starts_local=_display_local(row["starts_at"], user),
                           ends_local=_display_local(row["ends_at"], user)) for row in coverages],
        "handoffs": store.account_handoffs(account_id),
        "positive_outcomes": POSITIVE_OUTCOMES,
    }


@bp.post("/accounts/<int:account_id>/coverages")
def create_coverage(account_id):
    user = _manage_user()
    at = request_now()
    store = SqlHandoffStore(db())
    with _errors():
        starts_at = parse_local_time(request.form.get("starts_at", ""), user)
        ends_at = parse_local_time(request.form.get("ends_at", ""), user)
        validate_coverage_window(starts_at, ends_at, at)
        with store.atomic():
            account = account_required(account_id, write=True)
            schedule_coverage(store, account, request.form.get("revision", type=int),
                              request.form.get("rep_id", type=int), starts_at, ends_at, user["id"], observed=at)
    flash("Coverage scheduled.")
    return redirect(url_for("account_detail", account_id=account_id), code=303)


@bp.post("/accounts/<int:account_id>/coverages/<int:coverage_id>/cancel")
def cancel_coverage(account_id, coverage_id):
    user = _manage_user()
    at = request_now()
    store = SqlHandoffStore(db())
    with _errors(), store.atomic():
        account = account_required(account_id, write=True)
        end_coverage(store, account, request.form.get("revision", type=int), coverage_id, user["id"], observed=at)
    flash("Coverage ended.")
    return redirect(url_for("account_detail", account_id=account_id), code=303)


@bp.post("/accounts/<int:account_id>/handoffs")
def create_handoff(account_id):
    user = _handoff_user()
    at = request_now()
    with _errors():
        require_handoff_requester(user)
        submission = prepare_handoff(
            request.form.get("person_id", type=int), request.form.get("ae_id", type=int),
            request.form.get("outcome", ""), request.form.get("context", "").strip(),
            request.form.get("next_step", "").strip(), request.form.get("meeting_at", ""), user,
        )
        store = SqlHandoffStore(db())
        with store.atomic():
            account = account_required(account_id, write=True)
            handoff_id, changed = queue_handoff(
                store, account, request.form.get("revision", type=int), submission, user["id"], observed=at,
            )
    if changed:
        flash("A handoff already exists for this contact. Review its saved next step.")
    return redirect(url_for("sales_extensions.handoff_detail", handoff_id=handoff_id), code=303)


@bp.get("/handoffs")
def handoffs():
    user = _handoff_user()
    filters = {"search": request.args.get("search", "").strip(), "status": request.args.get("status", ""),
               "sort": request.args.get("sort", "recent"), "size": request.args.get("size", 25, type=int)}
    if filters["size"] not in {25, 50, 100}:
        filters["size"] = 25
    with _errors():
        scope = handoff_scope(user)
    records, page, pages = SqlHandoffStore(db()).list_handoffs(
        scope, user["id"], request_now(), filters, request.args.get("page", 1, type=int),
    )
    return render_template("sales_handoffs.html", handoffs=records, filters=filters, page=page, pages=pages,
                           outcomes=POSITIVE_OUTCOMES)


@bp.get("/handoffs/<int:handoff_id>")
def handoff_detail(handoff_id):
    user = _handoff_user()
    store = SqlHandoffStore(db())
    handoff = store.handoff_detail(handoff_id)
    if handoff is None:
        abort(404)
    if user["role"] == "ae":
        with _errors():
            require_ae_visibility(handoff, user)
    else:
        account_required(handoff["account_id"])
    payload = json.loads(handoff["payload"])
    for activity in payload["conversation"]:
        activity["when_local"] = _display_local(activity["occurred_at"], user)
    return render_template("sales_handoff.html", handoff=handoff, payload=payload,
                           meeting_local=_display_local(handoff["meeting_at"], user),
                           managed_by_user=user["role"] in MANAGEMENT, outcomes=POSITIVE_OUTCOMES,
                           outreach_restricted=store.blocked(handoff["account_id"], handoff["person_id"]))


@bp.post("/handoffs/<int:handoff_id>/retry")
def retry_handoff(handoff_id):
    user = _manage_user()
    store = SqlHandoffStore(db())
    with _errors(), store.atomic():
        changed = requeue_failed_handoff(store, handoff_id, user["id"], observed=request_now())
    if changed:
        flash("Handoff queued for delivery.")
    return redirect(url_for("sales_extensions.handoff_detail", handoff_id=handoff_id), code=303)


def deliver_one(connection, provider, *, clock=utcnow, lease_seconds=60):
    return deliver_pending_handoff(SqlHandoffStore(connection), provider, clock=clock, lease_seconds=lease_seconds)
