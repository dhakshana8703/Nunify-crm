"""Saved account views, local campaign membership, and sequencer activity."""

import hashlib
import hmac
import os
from contextlib import contextmanager

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from campaign_store import CampaignStore, initialize
from crm_core.campaign import (
    CampaignError, campaign_candidates, enroll, matches_view, record_sequence_event, require_campaign_access,
    save_campaign, save_view,
)
from workflow_store import transaction
from workflow_web import account_required, db, list_params, now, readable_accounts, staff_required, submitted


bp = Blueprint("campaigns", __name__)


@contextmanager
def _campaign_write():
    connection = db()
    try:
        with transaction(connection):
            yield CampaignStore(connection)
    except CampaignError as error:
        abort(error.status, str(error) or None)


def _allowed():
    try:
        require_campaign_access(g.crm_user)
    except CampaignError as error:
        abort(error.status)


def _view(store, view_id):
    view = store.view(view_id)
    if view is None:
        abort(404)
    return view


def _campaign(store, campaign_id):
    campaign = store.campaign(campaign_id)
    if campaign is None:
        abort(404)
    return campaign


@bp.get("/campaigns")
def index():
    _allowed()
    store = CampaignStore(db())
    campaign_rows = store.campaigns()
    view_id = request.args.get("view_id", type=int)
    if view_id is not None:
        campaign_rows = [row for row in campaign_rows if row["view_id"] == view_id]
    context = list_params(campaign_rows, lambda row: f"{row['name']} {row['view_name'] or ''}", {
        "name": lambda row: row["name"].casefold(), "recent": lambda row: -row["id"],
    }, "recent")
    return render_template("campaigns_index.html", views=store.views(), **context)


@bp.post("/campaigns/views")
def create_view():
    user = staff_required()
    name = submitted("name", required=True, max_length=120)
    account_type = submitted("organization_type", max_length=80)
    platform = request.form.get("platform", "")
    with _campaign_write() as store:
        view_id = save_view(store, name, account_type, platform, user["id"], now())
    return redirect(url_for("campaigns.view_detail", view_id=view_id), code=303)


@bp.get("/campaigns/views/<int:view_id>")
def view_detail(view_id):
    staff_required()
    store = CampaignStore(db())
    view = _view(store, view_id)
    rows = [account for account in readable_accounts() if matches_view(store, account, view)]
    owner_filter = request.args.get("ownership", "")
    if owner_filter in {"assigned", "unassigned"}:
        rows = [row for row in rows if bool(row["owner_id"]) == (owner_filter == "assigned")]
    context = list_params(rows, lambda row: f"{row['name']} {row['domain']}", {
        "name": lambda row: row["name"].casefold(), "next_event": lambda row: row["next_event"] or "9999",
    }, "name")
    return render_template("campaigns_view.html", view=view, ownership=owner_filter, **context)


@bp.post("/campaigns")
def create_campaign():
    user = staff_required()
    name = submitted("name", required=True, max_length=120)
    view_id = request.form.get("view_id", type=int)
    with _campaign_write() as store:
        campaign_id = save_campaign(store, name, view_id, user["id"], now())
    return redirect(url_for("campaigns.detail", campaign_id=campaign_id), code=303)


@bp.get("/campaigns/<int:campaign_id>")
def detail(campaign_id):
    _allowed()
    store = CampaignStore(db())
    campaign = _campaign(store, campaign_id)
    view = _view(store, campaign["view_id"]) if campaign["view_id"] is not None else None
    scope = {account["id"]: account for account in readable_accounts()}
    members = [row for row in store.members(campaign_id) if row["account_id"] in scope]
    status = request.args.get("status", "")
    summaries = {key: sum(row[key] for row in members) for key in ("sent", "replies", "blocked")}
    summaries["members"] = len(members)
    candidates = campaign_candidates(store, campaign_id, scope, view)
    context = list_params([row for row in members if not status or row["status"] == status],
                          lambda row: f"{row['person_name']} {row['account_name']}", {
        "name": lambda row: row["person_name"].casefold(), "recent": lambda row: -row["id"],
    }, "recent")
    return render_template("campaigns_detail.html", campaign=campaign, candidates=candidates,
                           status=status, summaries=summaries, **context)


@bp.post("/campaigns/<int:campaign_id>/members")
def add_member(campaign_id):
    _allowed()
    person_id = request.form.get("person_id", type=int)
    with _campaign_write() as store:
        campaign = _campaign(store, campaign_id)
        person = store.person(person_id)
        if person is None:
            abort(400, "Choose an eligible contact.")
        account = account_required(person["account_id"], write=True)
        view = _view(store, campaign["view_id"]) if campaign["view_id"] is not None else None
        enroll(store, campaign, person, account, view, g.crm_user["id"], now())
    flash("Campaign membership saved locally.", "success")
    return redirect(url_for("campaigns.detail", campaign_id=campaign_id), code=303)


@bp.post("/api/sequencer/events")
def receive_event():
    secret = current_app.config.get("CRM_SEQ_WEBHOOK_SECRET") or os.environ.get("CRM_SEQ_WEBHOOK_SECRET")
    if not secret:
        abort(503, "The live sequencer connection is pending.")
    raw = request.get_data(cache=True)
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    provided = request.headers.get("X-Nunify-Signature", "").encode()
    if not hmac.compare_digest(expected.encode(), provided):
        abort(401, "The sequencer signature is invalid.")
    event = request.get_json(silent=True)
    if not isinstance(event, dict):
        abort(400, "Send a JSON event object.")
    with _campaign_write() as store:
        recorded = record_sequence_event(store, event, now())
    return (jsonify(status="recorded"), 201) if recorded else jsonify(status="already_recorded")
