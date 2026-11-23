"""Saved account views, local campaign membership, and sequencer activity."""

import hashlib
import hmac
import json
import os
from datetime import datetime

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from workflow_store import audit, is_blocked, timestamp, transaction
from workflow_web import account_required, db, list_params, now, readable_accounts, staff_required, submitted


bp = Blueprint("campaigns", __name__)
SYSTEM_EMAIL = "sequencer-integration@nunify.invalid"
EVENT_OUTCOMES = {"sent": "sent", "replied": "replied", "bounced": "bounced", "unsubscribed": "opted_out"}
EVENT_TYPES = {*EVENT_OUTCOMES, "opened", "clicked"}
PLATFORMS = {"", "cvent", "unknown"}
PLATFORM_SIGNAL = "Registration platform - "

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales_views (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    organization_type TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '' CHECK (platform IN ('', 'cvent', 'unknown')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    view_id INTEGER REFERENCES sales_views(id),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_members (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    person_id INTEGER NOT NULL REFERENCES people(id),
    added_by INTEGER NOT NULL REFERENCES users(id),
    added_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused')),
    UNIQUE(campaign_id, person_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_campaign ON campaign_members(person_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS sequence_events (
    id TEXT PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES campaign_members(id),
    event_type TEXT NOT NULL CHECK (event_type IN ('sent', 'opened', 'clicked', 'replied', 'bounced', 'unsubscribed')),
    payload_hash TEXT NOT NULL,
    activity_id INTEGER REFERENCES activities(id),
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sequence_events_member ON sequence_events(member_id, event_type);
"""


def initialize(connection):
    connection.executescript(SCHEMA)


def _crm_view(view_id):
    row = db().execute("SELECT * FROM sales_views WHERE id = ?", (view_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _campaign(campaign_id):
    campaign = db().execute("""
        SELECT c.*, v.name AS view_name FROM campaigns c LEFT JOIN sales_views v ON v.id = c.view_id
        WHERE c.id = ?
    """, (campaign_id,)).fetchone()
    if campaign is None:
        abort(404)
    return campaign


def _allowed():
    if g.crm_user["role"] not in {"admin", "manager", "sdr"}:
        abort(403)


def _matches_view(account, view):
    if view is None:
        return True
    if view["organization_type"] and account["organization_type"].casefold() != view["organization_type"].casefold():
        return False
    if not view["platform"]:
        return True
    has_cvent = db().execute("""
        SELECT 1 FROM source_records WHERE account_id = ? AND source = 'cvent' LIMIT 1
    """, (account["id"],)).fetchone()
    if has_cvent:
        return view["platform"] == "cvent"
    qualification = db().execute("SELECT signals FROM qualifications WHERE account_id = ?",
                                 (account["id"],)).fetchone()
    signals = json.loads(qualification["signals"]) if qualification else []
    platforms = {signal["label"].removeprefix(PLATFORM_SIGNAL).casefold() for signal in signals
                 if signal["label"].startswith(PLATFORM_SIGNAL)}
    return "cvent" in platforms if view["platform"] == "cvent" else not platforms


def _eligible(person, account):
    return (person["unlocked"] and person["email_verified"] and person["email"]
            and account["stage"] != "handed_off" and not is_blocked(db(), account["id"], person["id"]))


@bp.get("/campaigns")
def index():
    _allowed()
    views = db().execute("SELECT * FROM sales_views ORDER BY name").fetchall()
    campaign_rows = db().execute("""
        SELECT c.*, v.name AS view_name FROM campaigns c LEFT JOIN sales_views v ON v.id = c.view_id
    """).fetchall()
    if request.args.get("view_id", type=int) is not None:
        campaign_rows = [row for row in campaign_rows if row["view_id"] == request.args.get("view_id", type=int)]
    context = list_params(campaign_rows, lambda row: f"{row['name']} {row['view_name'] or ''}", {
        "name": lambda row: row["name"].casefold(), "recent": lambda row: -row["id"],
    }, "recent")
    return render_template("campaigns_index.html", views=views, **context)


@bp.post("/campaigns/views")
def create_view():
    user = staff_required()
    name = submitted("name", required=True, max_length=120)
    account_type = submitted("organization_type", max_length=80)
    platform = request.form.get("platform", "")
    if platform not in PLATFORMS:
        abort(400, "Choose an available platform filter.")
    with transaction(db()):
        cursor = db().execute("""
            INSERT INTO sales_views (name, organization_type, platform, created_by, created_at)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT (name) DO NOTHING
        """, (name, account_type, platform, user["id"], timestamp(now())))
        if cursor.rowcount != 1:
            abort(409, "A saved view with this name already exists.")
        audit(db(), user["id"], None, "sales_view_created", {"view_id": cursor.lastrowid}, now=now())
    return redirect(url_for("campaigns.view_detail", view_id=cursor.lastrowid), code=303)


@bp.get("/campaigns/views/<int:view_id>")
def view_detail(view_id):
    staff_required()
    view = _crm_view(view_id)
    rows = [account for account in readable_accounts() if _matches_view(account, view)]
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
    with transaction(db()):
        if view_id is not None:
            _crm_view(view_id)
        cursor = db().execute("""
            INSERT INTO campaigns (name, view_id, created_by, created_at)
            VALUES (?, ?, ?, ?) ON CONFLICT (name) DO NOTHING
        """, (name, view_id, user["id"], timestamp(now())))
        if cursor.rowcount != 1:
            abort(409, "A campaign with this name already exists.")
        audit(db(), user["id"], None, "campaign_created", {"campaign_id": cursor.lastrowid}, now=now())
    return redirect(url_for("campaigns.detail", campaign_id=cursor.lastrowid), code=303)


@bp.get("/campaigns/<int:campaign_id>")
def detail(campaign_id):
    _allowed()
    campaign = _campaign(campaign_id)
    view = _crm_view(campaign["view_id"]) if campaign["view_id"] is not None else None
    scope = {account["id"]: account for account in readable_accounts()}
    members = [row for row in db().execute("""
        SELECT m.*, p.name AS person_name, a.id AS account_id, a.name AS account_name,
               COUNT(CASE WHEN event.event_type = 'sent' THEN 1 END) AS sent,
               COUNT(CASE WHEN event.event_type = 'replied' THEN 1 END) AS replies,
               COUNT(CASE WHEN event.event_type IN ('bounced', 'unsubscribed') THEN 1 END) AS blocked
        FROM campaign_members m JOIN people p ON p.id = m.person_id JOIN accounts a ON a.id = p.account_id
        LEFT JOIN sequence_events event ON event.member_id = m.id
        WHERE m.campaign_id = ? GROUP BY m.id, p.name, a.id, a.name
    """, (campaign_id,)) if row["account_id"] in scope]
    status = request.args.get("status", "")
    summaries = {key: sum(row[key] for row in members) for key in ("sent", "replies", "blocked")}
    summaries["members"] = len(members)
    candidates = []
    for person in db().execute("""
        SELECT p.* FROM people p WHERE p.unlocked = 1 AND p.email_verified = 1 AND NOT EXISTS (
            SELECT 1 FROM campaign_members m WHERE m.person_id = p.id AND (m.campaign_id = ? OR m.status = 'active')
        ) ORDER BY p.name
    """, (campaign_id,)):
        account = scope.get(person["account_id"])
        if account and account["owner_id"] and _matches_view(account, view) and _eligible(person, account):
            candidates.append(dict(person, account_name=account["name"]))
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
    with transaction(db()):
        campaign = _campaign(campaign_id)
        person = db().execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
        if person is None:
            abort(400, "Choose an eligible contact.")
        account = account_required(person["account_id"], write=True)
        view = _crm_view(campaign["view_id"]) if campaign["view_id"] is not None else None
        if not _eligible(person, account) or not _matches_view(account, view):
            abort(403, "This contact is ineligible for the campaign.")
        inserted = db().execute("""
            INSERT INTO campaign_members(campaign_id, person_id, added_by, added_at)
            VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING
        """, (campaign_id, person_id, g.crm_user["id"], timestamp(now())))
        if inserted.rowcount == 1:
            audit(db(), g.crm_user["id"], account["id"], "campaign_member_added", {
                "campaign_id": campaign_id, "person_id": person_id,
            }, now=now())
        elif db().execute("""
            SELECT 1 FROM campaign_members WHERE campaign_id = ? AND person_id = ?
        """, (campaign_id, person_id)).fetchone() is None:
            abort(409, "This contact is active in another campaign.")
    flash("Campaign membership saved locally.", "success")
    return redirect(url_for("campaigns.detail", campaign_id=campaign_id), code=303)


def _system_actor(connection):
    connection.execute("""
        INSERT INTO users(name, email, role, timezone, password_hash, active)
        VALUES ('Sequencer integration', ?, 'sdr', 'UTC', '', 0) ON CONFLICT (email) DO NOTHING
    """, (SYSTEM_EMAIL,))
    actor = connection.execute("SELECT id, active FROM users WHERE email = ?", (SYSTEM_EMAIL,)).fetchone()
    if actor["active"]:
        abort(409, "The reserved integration user is active. An admin must resolve the account conflict.")
    return actor["id"]


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
    event_id, member_id, event_type = (event.get(key) for key in ("event_id", "member_id", "type"))
    note = event.get("note", "")
    if (not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 180
            or type(member_id) is not int or not 0 < member_id < 2**63
            or not isinstance(event_type, str) or event_type not in EVENT_TYPES
            or not isinstance(note, str) or len(note) > 4000):
        abort(400, "Event ID, member ID, or event type is invalid.")
    received_at = now()
    occurred_at = received_at
    if "occurred_at" in event:
        try:
            occurred_at = datetime.fromisoformat(event["occurred_at"])
            if occurred_at.utcoffset() is None:
                raise ValueError
            timestamp(occurred_at)
        except (TypeError, ValueError, OverflowError):
            abort(400, "Use an ISO 8601 event timestamp with a timezone.")
    content_hash = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
    with transaction(db()):
        previous = db().execute("SELECT payload_hash FROM sequence_events WHERE id = ?", (event_id,)).fetchone()
        if previous is not None:
            if previous["payload_hash"] != content_hash:
                abort(409, "That event ID has already been used for another event.")
            return jsonify(status="already_recorded")
        member = db().execute("""
            SELECT p.id, p.account_id, p.email FROM campaign_members m JOIN people p ON p.id = m.person_id
            WHERE m.id = ?
        """, (member_id,)).fetchone()
        if member is None:
            abort(404, "Campaign member not found.")
        outcome = EVENT_OUTCOMES.get(event_type)
        activity_id = None
        if outcome:
            actor_id = _system_actor(db())
            activity_id = db().execute("""
                INSERT INTO activities(account_id, person_id, actor_id, channel, outcome, direction, note, occurred_at)
                VALUES (?, ?, ?, 'email', ?, ?, ?, ?)
            """, (member["account_id"], member["id"], actor_id, outcome,
                  "outbound" if outcome == "sent" else "inbound", note, timestamp(occurred_at))).lastrowid
        if event_type in {"replied", "bounced", "unsubscribed"}:
            db().execute("UPDATE campaign_members SET status = 'paused' WHERE person_id = ?", (member["id"],))
        if event_type == "replied":
            db().execute("""
                UPDATE people SET status = 'replied' WHERE id = ? AND do_not_contact = 0
                  AND status NOT IN ('positive', 'handed_off')
            """, (member["id"],))
            db().execute("""
                UPDATE account_work SET stage = 'engaged' WHERE account_id = ?
                  AND stage IN ('discovered', 'qualified', 'enriched', 'assigned', 'working', 'nurture')
            """, (member["account_id"],))
        if event_type == "sent":
            db().execute("""
                UPDATE account_work SET stage = 'working' WHERE account_id = ?
                  AND stage IN ('discovered', 'qualified', 'enriched', 'assigned', 'nurture')
            """, (member["account_id"],))
        if event_type in {"bounced", "unsubscribed"}:
            db().execute("""
                UPDATE people SET status = ?, do_not_contact = 1 WHERE id = ?
                  AND (status != 'opted_out' OR ? = 'opted_out')
            """, (outcome, member["id"], outcome))
            if member["email"]:
                db().execute("""
                    UPDATE campaign_members SET status = 'paused'
                    WHERE person_id IN (SELECT id FROM people WHERE email = ?)
                """, (member["email"],))
                db().execute("""
                    INSERT OR IGNORE INTO suppression(kind, value, reason, created_by, created_at)
                    VALUES ('email', ?, ?, ?, ?)
                """, (member["email"], "Opted out" if outcome == "opted_out" else "Bounced",
                      actor_id, timestamp(received_at)))
        db().execute("""
            INSERT INTO sequence_events(id, member_id, event_type, payload_hash, activity_id, occurred_at, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (event_id, member_id, event_type, content_hash, activity_id,
              timestamp(occurred_at), timestamp(received_at)))
    return jsonify(status="recorded"), 201
