"""Authenticated assignment, account activity, and rep queues."""

import calendar
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from collections import deque
from datetime import date, datetime, timedelta
from uuid import uuid4

from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)
from itsdangerous import BadSignature, URLSafeSerializer
from werkzeug.security import check_password_hash

from crm_store import connect
from cvent_contacts import evidence_for_account
from source_ops import account_sources
from workflow_store import (
    account_relation, account_rows, active_coverage, audit, local_date, suppression_reason,
    normalized_linkedin_profile, timestamp, transaction, utcnow,
)


bp = Blueprint("workflow", __name__)
STAFF = {"admin", "manager"}
ROLE_HOME = {
    "admin": "source_ops.index",
    "manager": "workflow.assignment",
    "sdr": "workflow.today",
    "ae": "sales_extensions.handoffs",
}
ASSIGNMENT_SOURCES = (
    ("all", "All sources"), ("cvent", "Cvent"), ("directory", "Directory of Associations"),
    ("both", "Both Cvent and Directory"), ("multi", "2+ sources"), ("other", "Other sources"),
)
ASSIGNMENT_FILTERS = ("filter", "source", "org_type", "owner", "stage", "show_suppressed",
                      "company", "search", "sort", "page_size", "page")
MAX_BULK_ASSIGNMENTS = 100
LABELS = {
    "call": "Call", "email": "Email", "linkedin": "LinkedIn", "note": "Note",
    "not_contacted": "Not contacted", "do_not_contact": "Do not contact",
    "apollo": "Apollo", "apollo_offline": "Offline enrichment", "cvent": "Cvent event",
    "cvent_event": "Cvent event", "directory": "Directory of Associations",
    "demo": "Demo", "manual": "Manual entry", "source": "Imported source",
}
OUTCOMES = {
    "call": {"no_answer", "voicemail", "connected", "wrong_number", "not_interested", "meeting_booked",
             "replied", "ghosted", "opted_out"},
    "email": {"sent", "replied", "bounced", "opted_out", "ghosted", "demo_requested", "pricing_requested"},
    "linkedin": {"connection_sent", "accepted", "message_sent", "replied", "opted_out",
                 "ghosted", "demo_requested", "pricing_requested"},
    "meeting": {"meeting_booked", "no_show"},
    "note": {"noted", "nurture"},
}
REPLIES = {"replied", "demo_requested", "pricing_requested"}
INBOUND = REPLIES | {"bounced", "opted_out", "accepted"}
STAGES_BEFORE_WORKING = {"discovered", "qualified", "enriched", "assigned", "nurture"}
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def now():
    return current_app.config.get("CRM_CLOCK", utcnow)()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(32)
    return session["csrf"]


def authenticate_request():
    g.crm_user = None
    unauthenticated = {None, "static", "source_ops.ingest", "campaigns.receive_event"}
    if request.endpoint in unauthenticated or request.endpoint in current_app.config.get("MACHINE_ENDPOINTS", ()):
        return
    row = db().execute("""
        SELECT id, name, role, timezone FROM users WHERE id = ? AND active = 1
    """, (session.get("user_id"),)).fetchone()
    g.crm_user = dict(row) if row else None
    if not g.crm_user and request.endpoint != "workflow.login":
        if request.path.startswith("/api/"):
            abort(401)
        return redirect(url_for("workflow.login"))
    if request.method == "POST":
        received = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(received.encode(), csrf_token().encode()):
            abort(400, "This form expired. Reload the page and try again.")


def staff_required():
    if g.crm_user["role"] not in STAFF:
        abort(403)
    return g.crm_user


def user_day():
    return local_date(g.crm_user, now())


def account_day(account, at=None):
    zone = account.get("owner_timezone") or g.crm_user["timezone"]
    return local_date({"timezone": zone}, at if at is not None else now())


def account_required(account_id, *, write=False):
    user = g.crm_user
    account = account_rows(db(), user_day().isoformat(), account_id)
    if account is None:
        abort(404)
    has_work = user["role"] in STAFF or (
        user["role"] == "sdr" and account_relation(db(), account, user, now()) in {"Owned", "Coverage"}
    )
    if not has_work:
        abort(403)
    if write and not account["owner_id"]:
        abort(409, "Assign this account before recording work.")
    return account


def show_suppressed():
    return request.args.get("show_suppressed") == "1"


def readable_accounts(*, include_suppressed=False):
    user = g.crm_user
    if not user or user["role"] == "ae":
        return []
    rows = account_rows(db(), user_day().isoformat())
    visible = []
    for account in rows:
        account["relation"] = account_relation(db(), account, user, now())
        if user["role"] not in STAFF and account["relation"] not in {"Owned", "Coverage"}:
            continue
        account["blocked"] = suppression_reason(db(), account["id"])
        if include_suppressed or not account["blocked"]:
            visible.append(account)
    return visible


def label(value):
    return LABELS.get(value, value.replace("_", " ").capitalize())


def page_items(rows):
    size = request.args.get("page_size", 25, type=int)
    if size not in {25, 50, 100}:
        size = 25
    total = len(rows)
    pages = max(1, (total + size - 1) // size)
    page = min(pages, max(1, request.args.get("page", 1, type=int)))
    return rows[(page - 1) * size:page * size], {
        "page": page, "pages": pages, "size": size, "total": total,
    }


def list_params(rows, get_text, sort_keys, default_sort, *, search=None):
    if search is None:
        search = request.args.get("search", "").strip()
    sort = request.args.get("sort", default_sort)
    if sort not in sort_keys:
        sort = default_sort
    if search:
        rows = [row for row in rows if search.casefold() in get_text(row).casefold()]
    rows.sort(key=sort_keys[sort])
    items, pagination = page_items(rows)
    return {"items": items, "pagination": pagination, "search": search, "query": search, "sort": sort}


def account_list(rows, *, include_fit_score=False, search=None):
    def by_name(row):
        return row["name"].casefold()

    sort_keys = {
        "name": by_name,
        "next_event": lambda row: (row["next_event"] or "9999", by_name(row)),
        "stage": lambda row: (row["stage"], by_name(row)),
    }
    if include_fit_score:
        sort_keys["fit_score"] = lambda row: (row["fit_score"] is None, -(row["fit_score"] or 0), by_name(row))
    return list_params(
        rows, lambda row: f"{row['name']} {row['domain']} {row['owner_name'] or ''}",
        sort_keys, "name", search=search,
    )


def offset_months(day, count):
    year, month = divmod(day.year * 12 + day.month - 1 + count, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def activity_context(account):
    connection = db()
    account_id = account["id"]
    contacts = [dict(row) for row in connection.execute(
        "SELECT * FROM people WHERE account_id = ? ORDER BY name COLLATE NOCASE", (account_id,),
    )]
    for contact in contacts:
        contact["blocked"] = suppression_reason(connection, account_id, contact["id"])
        contact["origin_label"] = label(contact["origin"]) if contact["origin"] else "Source unavailable"
    has_suppressed_contacts = any(contact["blocked"] for contact in contacts)
    include_suppressed = show_suppressed()
    activities = connection.execute("""
        SELECT a.*, u.name AS actor_name, p.name AS contact_name
        FROM activities a JOIN users u ON u.id = a.actor_id LEFT JOIN people p ON p.id = a.person_id
        WHERE a.account_id = ? ORDER BY a.occurred_at DESC, a.id DESC
    """, (account_id,)).fetchall()
    tasks = connection.execute("""
        SELECT t.*, u.name AS owner_name, p.name AS contact_name
        FROM tasks t JOIN users u ON u.id = t.owner_id
        LEFT JOIN people p ON p.id = t.person_id
        WHERE t.account_id = ? ORDER BY t.due_on, t.id
    """, (account_id,)).fetchall()
    grant = active_coverage(connection, account, now())
    due_day = account_day(account)
    return {
        "work": dict(
            account, can_work=bool(account["owner_id"]), blocked=suppression_reason(connection, account_id),
            active_coverage=dict(grant) if grant else None,
        ),
        "contacts": [contact for contact in contacts if include_suppressed or not contact["blocked"]],
        "has_suppressed_contacts": has_suppressed_contacts, "show_suppressed": include_suppressed,
        "activities": activities, "tasks": tasks, "action_key": uuid4().hex,
        "task_action_key": uuid4().hex,
        "local_day": due_day.isoformat(),
        "due_timezone": account["owner_timezone"] or g.crm_user["timezone"],
    }


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = db().execute("SELECT * FROM users WHERE email = ? AND active = 1", (
            request.form.get("email", "").strip(),
        )).fetchone()
        if user is None or not check_password_hash(user["password_hash"], request.form.get("password", "")):
            flash("Check your email and password.", "error")
            return render_template("login.html"), 401
        session.clear()
        session["user_id"] = user["id"]
        return redirect(url_for(ROLE_HOME[user["role"]]), code=303)
    if g.crm_user:
        return redirect(url_for("home"))
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("workflow.login"), code=303)


@bp.get("/today")
def today():
    if g.crm_user["role"] == "ae":
        abort(403)
    bucket = request.args.get("bucket", "today")
    if bucket not in {"today", "tomorrow"}:
        bucket = "today"
    day = user_day()
    queue_day = day + timedelta(days=1) if bucket == "tomorrow" else day
    accounts = readable_accounts()
    rows = []
    for account in accounts:
        account_id = account["id"]
        task_day = account_day(account) + timedelta(days=1 if bucket == "tomorrow" else 0)
        queue_iso = task_day.isoformat()
        base = {"account_id": account_id, "account_name": account["name"], "task_id": None, "reply_id": None}
        for task in db().execute("""
            SELECT * FROM tasks WHERE account_id = ? AND status IN ('open', 'snoozed') AND due_on <= ?
        """, (account_id, queue_iso)):
            if bucket == "tomorrow" and task["due_on"] != queue_iso:
                continue
            if account["relation"] == "Coverage":
                grant = active_coverage(db(), account, now())
                owner_zone = {"timezone": account["owner_timezone"]}
                if not grant or local_date(owner_zone, date_time(grant["ends_at"])) < task_day:
                    continue
            overdue = task["due_on"] < queue_iso
            blocked = suppression_reason(db(), account_id, task["person_id"])
            if blocked:
                continue
            reason = "Overdue" if overdue else "Follow-up due"
            rows.append(dict(base, title=task["title"], due_on=task["due_on"], task_id=task["id"],
                             priority=1 if overdue else 2, reason=reason))
        if bucket == "tomorrow" or not account["owner_id"]:
            continue
        for reply in db().execute("""
            SELECT a.id, a.person_id, a.outcome, p.name FROM activities a LEFT JOIN people p ON p.id = a.person_id
            WHERE a.account_id = ? AND a.direction = 'inbound' AND a.acknowledged_at IS NULL
              AND a.outcome IN ('replied', 'demo_requested', 'pricing_requested')
              AND NOT EXISTS (
                  SELECT 1 FROM activities response
                  WHERE response.account_id = a.account_id AND response.person_id = a.person_id
                    AND response.direction = 'outbound'
                    AND (response.occurred_at, response.id) > (a.occurred_at, a.id)
              )
        """, (account_id,)):
            if reply["person_id"] and suppression_reason(db(), account_id, reply["person_id"]):
                continue
            title = f"{label(reply['outcome'])} from {reply['name']}" if reply["name"] else label(reply["outcome"])
            rows.append(dict(base, title=title, due_on=None, reply_id=reply["id"], priority=0, reason="Unanswered reply"))
        assigned_at = account["assigned_at"]
        if assigned_at and not db().execute(
            "SELECT 1 FROM activities WHERE account_id = ? AND occurred_at >= ? LIMIT 1",
            (account_id, assigned_at),
        ).fetchone():
            rows.append(dict(base, title="Review account evidence", due_on=None, priority=3, reason="Newly assigned"))
        start = offset_months(day, current_app.config["PLANNING_MONTHS_MIN"]).isoformat()
        end = offset_months(day, current_app.config["PLANNING_MONTHS_MAX"]).isoformat()
        if account["next_event"] and start <= account["next_event"] <= end and not suppression_reason(db(), account_id):
            rows.append(dict(base, title="Review upcoming event", due_on=account["next_event"],
                             priority=4, reason="Event planning window"))
    context = list_params(rows, lambda row: f"{row['title']} {row['account_name']} {row['reason']}", {
        "priority": lambda row: (row["priority"], row["due_on"] or "", row["account_name"]),
        "account": lambda row: (row["account_name"].casefold(), row["priority"]),
        "due": lambda row: (row["due_on"] or "9999", row["priority"]),
    }, "priority")
    return render_template("today.html", **context, local_day=day.isoformat(),
                           queue_day=queue_day.isoformat(), bucket=bucket, has_work_accounts=bool(accounts))


def date_time(value):
    return datetime.fromisoformat(value)


@bp.get("/my-accounts")
def my_accounts():
    if g.crm_user["role"] == "ae":
        abort(403)
    selected = request.args.get("filter", "all").casefold()
    pool = selected == "pool" and g.crm_user["role"] == "sdr"
    include_suppressed = show_suppressed() and not pool
    if pool:
        rows = [dict(row, relation="Unassigned") for row in account_rows(db(), user_day().isoformat())
                if not row["owner_id"] and not suppression_reason(db(), row["id"])]
    else:
        rows = readable_accounts(include_suppressed=include_suppressed)
    if selected in {"owned", "coverage"}:
        rows = [row for row in rows if row["relation"].casefold() == selected]
    context = account_list(rows)
    visible_sources = {} if pool else account_sources(db(), (row["id"] for row in context["items"]))
    return render_template("my_accounts.html", **context, selected=selected, filter=selected,
                           show_suppressed=include_suppressed, sources_by_account=visible_sources)


@bp.get("/people")
def people():
    if g.crm_user["role"] == "ae":
        abort(403)
    include_suppressed = show_suppressed()
    accounts = {account["id"]: account for account in readable_accounts(include_suppressed=include_suppressed)}
    visible = []
    for person in db().execute("""
        SELECT id, account_id, name, title, email, persona, status, origin, email_verified,
               unlocked, linkedin_url FROM people
        WHERE account_id IN (SELECT value FROM json_each(?))
    """, (json.dumps(list(accounts)),)):
        account = accounts[person["account_id"]]
        blocked = account["blocked"] or suppression_reason(db(), account["id"], person["id"])
        if include_suppressed or not blocked:
            visible.append(dict(person, account_name=account["name"], blocked=blocked,
                                origin_label=label(person["origin"]) if person["origin"] else "Source unavailable"))
    return render_template(
        "people.html", show_suppressed=include_suppressed, **list_params(
            visible, lambda row: f"{row['name']} {row['title']} {row['email']} {row['account_name']}",
            {
                "name": lambda row: (row["name"].casefold(), row["id"]),
                "account": lambda row: (row["account_name"].casefold(), row["name"].casefold(), row["id"]),
            }, "name",
        )
    )


@bp.get("/assignment")
def assignment():
    staff_required()
    selected = request.args.get("filter", "all").casefold()
    include_suppressed = show_suppressed()
    rows = readable_accounts(include_suppressed=include_suppressed)
    reps = _rep_workloads(db())
    owners = {str(rep["id"]): rep["name"] for rep in reps}
    owners.update({str(row["owner_id"]): row["owner_name"] for row in rows if row["owner_id"] and row["owner_name"]})
    owner_options = [("", "Any owner"), ("unassigned", "Unassigned"),
                     *sorted(owners.items(), key=lambda option: option[1].casefold())]
    owner = request.args.get("owner", "")
    if owner not in dict(owner_options):
        owner = ""
    stage_options = sorted({row["stage"] for row in rows})
    stage = request.args.get("stage", "")
    if stage not in stage_options:
        stage = ""
    organization_types = sorted({row["organization_type"] for row in rows if row["organization_type"]},
                                key=str.casefold)
    organization_type = request.args.get("org_type", "")
    if organization_type not in organization_types:
        organization_type = ""
    if selected in {"assigned", "unassigned"}:
        rows = [row for row in rows if bool(row["owner_id"]) == (selected == "assigned")]
    if selected == "qualified_unassigned":
        rows = [
            row for row in rows if not row["owner_id"] and row["stage"] == "qualified"
        ]
    source = request.args.get("source", "all")
    if source not in dict(ASSIGNMENT_SOURCES):
        source = "all"
    sources_by_account = account_sources(db())
    scores_by_account = dict(db().execute("SELECT account_id, score FROM qualifications").fetchall())
    filtered_rows = []
    for account in rows:
        evidence = sources_by_account.get(account["id"], ())
        sources = {item["slug"] for item in evidence}
        if owner and owner != (str(account["owner_id"]) if account["owner_id"] else "unassigned"):
            continue
        if stage and stage != account["stage"]:
            continue
        matches_source = {
            "all": True,
            "both": {"cvent", "directory"} <= sources,
            "multi": len(sources) > 1,
            "other": bool(sources - {"cvent", "directory"}),
        }.get(source, source in sources)
        if not matches_source or (organization_type and organization_type != account["organization_type"]):
            continue
        account["evidence_labels"] = [item["name"] for item in evidence]
        account["fit_score"] = scores_by_account.get(account["id"])
        filtered_rows.append(account)

    company_options = sorted(
        filtered_rows, key=lambda row: (row["name"].casefold(), row["domain"], row["id"]),
    )
    requested_company = request.args.get("company", type=int)
    company = next((row for row in company_options if row["id"] == requested_company), None)
    filters = _assignment_filters()
    if company:
        filters["company"] = str(company["id"])
        filters.pop("search", None)
    else:
        filters.pop("company", None)
    results = account_list([company] if company else filtered_rows, include_fit_score=True,
                           search="" if company else None)
    return render_template(
        "assignment.html", **results,
        company_options=company_options, selected_company=company["id"] if company else None,
        reps=reps, selected=selected, source=source, source_options=ASSIGNMENT_SOURCES,
        assignment_filters=filters, organization_types=organization_types,
        organization_type=organization_type, owner_options=owner_options, owner=owner,
        stage_options=stage_options, stage=stage, show_suppressed=include_suppressed,
    )


def _assignment_filters():
    return {key: request.args[key] for key in ASSIGNMENT_FILTERS if key in request.args}


def _rep_workloads(connection):
    observed_at = now()
    return connection.execute("""
        SELECT u.id, u.name,
               (SELECT COUNT(*) FROM account_work WHERE owner_id = u.id) AS account_count,
               (SELECT COUNT(*) FROM tasks WHERE owner_id = u.id AND status != 'done') AS open_tasks,
               (SELECT COUNT(*) FROM activities WHERE actor_id = u.id AND direction = 'outbound'
                 AND occurred_at BETWEEN ? AND ?) AS touches
        FROM users u WHERE u.role = 'sdr' AND u.active = 1 ORDER BY u.name COLLATE NOCASE, u.id
    """, (timestamp(observed_at - timedelta(days=7)), timestamp(observed_at))).fetchall()


def verify_revision(account):
    _check_revision(account, request.form.get("revision", type=int))


def _check_revision(account, expected):
    if expected != account["revision"]:
        abort(409, "The account changed while this form was open. Reload and review the current assignment.")


def _selected_sdr(connection):
    owner_id = request.form.get("owner_id", type=int)
    if not connection.execute(
        "SELECT id FROM users WHERE id = ? AND role = 'sdr' AND active = 1", (owner_id,),
    ).fetchone():
        abort(400, "Choose an active SDR.")
    return owner_id


def _apply_assignment(connection, account, actor_id, owner_id, changed_at, *, method="manual"):
    blocked = suppression_reason(connection, account["id"])
    if blocked:
        abort(409, f"Assignment is blocked for this account. {blocked}.")
    if owner_id == account["owner_id"]:
        return False
    account_id = account["id"]
    at = timestamp(changed_at)
    stage = "assigned" if account["stage"] == "discovered" else account["stage"]
    connection.execute("""
        INSERT INTO account_work(account_id, owner_id, stage, revision, assigned_at) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET owner_id = excluded.owner_id, stage = excluded.stage,
            revision = excluded.revision, assigned_at = excluded.assigned_at
    """, (account_id, owner_id, stage, account["revision"] + 1, at))
    connection.execute(
        "UPDATE tasks SET owner_id = ? WHERE account_id = ? AND status != 'done'", (owner_id, account_id),
    )
    connection.execute("""
        UPDATE coverages SET cancelled_at = ?
        WHERE account_id = ? AND cancelled_at IS NULL AND ends_at > ?
    """, (at, account_id, at))
    audit(connection, actor_id, account_id, "assignment", {
        "previous_owner_id": account["owner_id"], "owner_id": owner_id, "method": method,
    }, changed_at)
    return True


@bp.post("/assignment/<int:account_id>")
def assign(account_id):
    user = staff_required()
    connection = db()
    with transaction(connection):
        account = account_required(account_id)
        verify_revision(account)
        owner_id = _selected_sdr(connection)
        _apply_assignment(connection, account, user["id"], owner_id, now())
    flash(f"Assignment saved for {account['name']}.", "success")
    return redirect(url_for("workflow.assignment", **_assignment_filters()), code=303)


def _selected_revisions(values):
    if len(values) > MAX_BULK_ASSIGNMENTS:
        abort(400, "Select at most 100 accounts at a time.")
    revisions = {}
    for value in values:
        match = re.fullmatch(r"([1-9][0-9]{0,18}):([0-9]{1,19})", value)
        if not match:
            abort(400, "Reload the assignment list and select the accounts again.")
        account_id, revision = map(int, match.groups())
        if max(account_id, revision) > 2**63 - 1:
            abort(400, "Reload the assignment list and select the accounts again.")
        if revisions.setdefault(account_id, revision) != revision:
            abort(409, "A selected account changed. Reload the list and try again.")
    return revisions


def _bulk_selection():
    explicitly_selected = _selected_revisions(request.form.getlist("account"))
    page_selected = _selected_revisions(
        request.form.getlist("unassigned_on_page") if request.form.get("assign_page") == "yes" else [],
    )
    changes = {**page_selected, **explicitly_selected}
    if not changes or len(changes) > MAX_BULK_ASSIGNMENTS:
        abort(400, "Select between 1 and 100 accounts.")
    if any(changes[account_id] != revision for account_id, revision in page_selected.items()):
        abort(409, "A selected account changed. Reload the list and try again.")
    return changes, explicitly_selected


@bp.post("/assignment/bulk")
def assign_bulk():
    user = staff_required()
    changes, explicitly_selected = _bulk_selection()
    connection = db()
    with transaction(connection):
        owner_id = _selected_sdr(connection)
        accounts = []
        for account_id, revision in changes.items():
            account = account_required(account_id)
            _check_revision(account, revision)
            if account["owner_id"] is not None and account_id not in explicitly_selected:
                abort(409, "An account on this page is already assigned. Reload and review the current assignments.")
            if account["owner_id"] not in (None, owner_id) and request.form.get("allow_transfer") != "yes":
                abort(400, "Allow transfers to move individually selected accounts that have an owner.")
            accounts.append(account)
        changed_at = now()
        count = sum(_apply_assignment(connection, account, user["id"], owner_id, changed_at) for account in accounts)
    flash(
        f"Updated ownership for {count} {'account' if count == 1 else 'accounts'}."
        if count else "The selected accounts already have that owner.",
        "success",
    )
    return redirect(url_for("workflow.assignment", **_assignment_filters()), code=303)


def _round_robin_plan(connection, selection, rep_ids, cap):
    groups = [
        dict(row, accounts=[]) for row in _rep_workloads(connection) if row["id"] in rep_ids
    ]
    if len(groups) != len(rep_ids):
        abort(400, "Choose active SDRs for the allocation.")
    groups.sort(key=lambda group: group["account_count"])
    rotation = deque(group for group in groups if group["account_count"] < cap)
    unallocated = 0
    for account_id, revision in selection:
        account = account_required(account_id)
        _check_revision(account, revision)
        if account["owner_id"] is not None or suppression_reason(connection, account_id):
            abort(409, "Round-robin allocation requires unassigned accounts without outreach restrictions.")
        if not rotation:
            unallocated += 1
            continue
        group = rotation.popleft()
        group["accounts"].append(account)
        if group["account_count"] + len(group["accounts"]) < cap:
            rotation.append(group)
    snapshot = [
        [group["id"], group["account_count"], [account["id"] for account in group["accounts"]]]
        for group in groups
    ]
    return groups, unallocated, snapshot


@bp.post("/assignment/round-robin")
def assign_round_robin():
    user = staff_required()
    signer = URLSafeSerializer(current_app.secret_key, salt="round-robin-assignment-v1")
    confirmation = request.form.get("allocation")
    if confirmation:
        try:
            previous = signer.loads(confirmation)
        except BadSignature:
            abort(400, "Preview the round-robin allocation again before confirming.")
        if previous["actor_id"] != user["id"]:
            abort(403)
        selection, rep_ids, cap = previous["selection"], previous["rep_ids"], previous["cap"]
    else:
        selected, _ = _bulk_selection()
        selection = [[account_id, revision] for account_id, revision in selected.items()]
        rep_ids = request.form.getlist("rep_id", type=int)
        if not rep_ids or len(rep_ids) != len(request.form.getlist("rep_id")):
            abort(400, "Choose the SDRs for the round-robin allocation.")
        rep_ids = sorted(set(rep_ids))
        cap = request.form.get("cap", type=int)
        if cap is None or cap < 1:
            abort(400, "Enter a positive maximum total account count per SDR.")

    connection = db()
    with transaction(connection):
        groups, unallocated, snapshot = _round_robin_plan(connection, selection, rep_ids, cap)
        count = len(selection) - unallocated
        if confirmation:
            if snapshot != previous["snapshot"]:
                abort(409, "Rep workloads changed. Preview the round-robin allocation again.")
            changed_at = now()
            for group in groups:
                for account in group["accounts"]:
                    _apply_assignment(connection, account, user["id"], group["id"], changed_at, method="round_robin")

    if confirmation:
        flash(f"Distributed {count} {'account' if count == 1 else 'accounts'} across SDRs.", "success")
        return redirect(url_for("workflow.assignment", **_assignment_filters()), code=303)

    allocation = signer.dumps({
        "actor_id": user["id"], "selection": selection, "rep_ids": rep_ids, "cap": cap, "snapshot": snapshot,
    })
    return render_template(
        "assignment.html", allocation=allocation, allocation_groups=groups, allocation_count=count,
        allocation_unallocated=unallocated, allocation_cap=cap, assignment_filters=_assignment_filters(),
    )


def submitted(field, *, max_length, required=False):
    value = request.form.get(field, "").strip()
    if len(value) > max_length or (required and not value):
        abort(400, f"Enter a valid {field.replace('_', ' ')}.")
    return value


def task_date(value, *, relative_to=None):
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
        if value != parsed.isoformat() or parsed < (relative_to or user_day()):
            raise ValueError
        return value
    except ValueError:
        abort(400, "Choose today or a future date in your timezone.")


def selected_person(account_id):
    value = request.form.get("person_id", "")
    if not value:
        return None
    row = db().execute(
        "SELECT * FROM people WHERE id = ? AND account_id = ?", (value, account_id),
    ).fetchone()
    if not row:
        abort(400, "Choose a contact from this account.")
    return row


def apply_activity_state(account, person, outcome, direction):
    person_status = {
        "connected": "connected", "not_interested": "not_interested", "wrong_number": "wrong_person",
        "bounced": "bounced", "opted_out": "opted_out",
    }.get(outcome, "replied" if outcome in REPLIES else None)
    if person and person_status:
        if not person["do_not_contact"] or outcome in {"bounced", "opted_out"}:
            db().execute("""
                UPDATE people SET status = ?, do_not_contact = MAX(do_not_contact, ?) WHERE id = ?
            """, (person_status, int(outcome in {"bounced", "opted_out"}), person["id"]))
        if outcome == "opted_out" and person["email"]:
            db().execute("""
                INSERT OR IGNORE INTO suppression(kind, value, reason, created_by, created_at)
                VALUES ('email', ?, 'Opted out', ?, ?)
            """, (person["email"], g.crm_user["id"], timestamp(now())))
    stage = account["stage"]
    if outcome == "nurture" and stage not in {"positive", "handed_off", "won"}:
        stage = "nurture"
    elif (outcome in REPLIES or outcome == "connected") and stage in STAGES_BEFORE_WORKING | {"working"}:
        stage = "engaged"
    elif direction == "outbound" and stage in STAGES_BEFORE_WORKING:
        stage = "working"
    if stage != account["stage"]:
        db().execute("UPDATE account_work SET stage = ? WHERE account_id = ?", (stage, account["id"]))


@bp.post("/accounts/<int:account_id>/activities")
def log_activity(account_id):
    user = g.crm_user
    key = submitted("action_key", max_length=64, required=True)
    channel = submitted("channel", max_length=20, required=True)
    outcome = submitted("outcome", max_length=30, required=True)
    note = submitted("note", max_length=4000)
    due_on = submitted("due_on", max_length=10)
    due_tomorrow = request.form.get("due_tomorrow") == "1"
    next_step = submitted("next_step", max_length=180)
    if outcome not in OUTCOMES.get(channel, ()):
        abort(400, "Choose an outcome for this channel.")
    direction = "internal" if channel == "note" or outcome == "ghosted" else (
        "inbound" if outcome in INBOUND else "outbound"
    )
    due_request = {"relative": "tomorrow"} if due_tomorrow else due_on
    payload = [account_id, request.form.get("person_id", ""), channel, outcome, note, due_request, next_step]
    request_hash = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
    with transaction(db()):
        account = account_required(account_id, write=True)
        existing = db().execute(
            "SELECT request_hash FROM activities WHERE actor_id = ? AND request_key = ?", (user["id"], key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                abort(409, "This form was used for another activity. Open a fresh form.")
        else:
            verify_revision(account)
            saved_at = now()
            local_day = account_day(account, saved_at)
            if due_tomorrow:
                due_on = (local_day + timedelta(days=1)).isoformat()
            else:
                task_date(due_on, relative_to=local_day)
            if next_step and not due_on:
                abort(400, "Choose a due date for the next action.")
            person = selected_person(account_id)
            person_id = person["id"] if person else None
            if outcome in {"opted_out", "bounced", "demo_requested", "pricing_requested"} and person is None:
                abort(400, "Choose the contact for this outcome.")
            blocked = suppression_reason(db(), account_id, person_id)
            if blocked and (direction == "outbound" or due_on):
                abort(403, f"Outreach is blocked. {blocked}.")
            if outcome in {"opted_out", "bounced"} and due_on:
                abort(400, "Remove the outreach follow-up for this contact.")
            activity_id = db().execute("""
                INSERT INTO activities(account_id, person_id, actor_id, channel, outcome, direction, note,
                                       occurred_at, request_key, request_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (account_id, person_id, user["id"], channel, outcome, direction, note,
                  timestamp(saved_at), key, request_hash)).lastrowid
            if due_on:
                default_title = f"Follow up with {person['name']}" if person else "Follow up"
                db().execute("""
                    INSERT INTO tasks(account_id, person_id, owner_id, activity_id, title, due_on)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (account_id, person_id, account["owner_id"], activity_id,
                      next_step or default_title, due_on))
            apply_activity_state(account, person, outcome, direction)
    flash("Activity saved.", "success")
    return redirect(url_for("account_detail", account_id=account_id), code=303)


CONTACT_STATUSES = (
    "not_contacted", "connected", "replied", "not_interested", "wrong_person", "do_not_contact",
)


def linkedin_profile(value):
    profile = normalized_linkedin_profile(value)
    if profile is None:
        abort(400, "Enter a LinkedIn profile URL such as https://www.linkedin.com/in/name.")
    return profile


def contact_fields(existing=None):
    fields = {}
    for name, maximum in (("name", 180), ("title", 180), ("email", 254),
                          ("phone", 64), ("linkedin_url", 512), ("persona", 24)):
        if existing is not None and name not in request.form:
            fields[name] = existing[name]
        else:
            fields[name] = submitted(name, max_length=maximum, required=name == "name")
    if fields["email"] and (existing is None or fields["email"] != existing["email"]):
        if not EMAIL.fullmatch(fields["email"]):
            abort(400, "Enter a valid contact email.")
    if existing is None or fields["linkedin_url"] != existing["linkedin_url"]:
        fields["linkedin_url"] = linkedin_profile(fields["linkedin_url"])
        if len(fields["linkedin_url"]) > 512:
            abort(400, "Enter a shorter LinkedIn profile URL.")
    return fields


def contact_required(account_id, person_id, *, write=False):
    account = account_required(account_id, write=write)
    contact = db().execute("SELECT * FROM people WHERE account_id = ? AND id = ?", (account_id, person_id)).fetchone()
    if not contact:
        abort(404)
    blocked = suppression_reason(db(), account_id, person_id)
    if blocked and write:
        abort(403, "Contact changes are restricted by suppression.")
    if blocked and not show_suppressed():
        abort(404)
    return account, contact, blocked


def contact_email_locked(contact):
    return contact["origin"] != "manual" or bool(contact["unlocked"] or contact["email_verified"])


def verify_contact_revision(account, contact):
    verify_revision(account)
    if request.form.get("details_revision", type=int) != contact["details_revision"]:
        abort(409, "The contact has changed. Refresh and retry.")


def duplicate_contact_error(error):
    if error.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE:
        abort(409, "This email or LinkedIn profile already belongs to a contact on the account.")


@bp.get("/accounts/<int:account_id>/contacts/<int:person_id>")
def contact_detail(account_id, person_id):
    account, row, blocked = contact_required(account_id, person_id)
    contact = dict(row, origin_label=label(row["origin"]))
    activities, tasks, evidence = [], [], []
    if not blocked:
        activities = db().execute("""
            SELECT a.*, u.name AS actor_name FROM activities a JOIN users u ON u.id = a.actor_id
            WHERE a.account_id = ? AND a.person_id = ?
            ORDER BY a.occurred_at DESC, a.id DESC
        """, (account_id, person_id)).fetchall()
        tasks = db().execute("""
            SELECT t.*, u.name AS owner_name FROM tasks t JOIN users u ON u.id = t.owner_id
            WHERE t.account_id = ? AND t.person_id = ? AND t.status IN ('open', 'snoozed')
            ORDER BY t.due_on, t.id
        """, (account_id, person_id)).fetchall()
        evidence = [item for item in evidence_for_account(db(), account_id) if item["person_id"] == person_id]
    return render_template(
        "contact.html", account=account, contact=contact, blocked=blocked, activities=activities, tasks=tasks,
        evidence=evidence, email_locked=contact_email_locked(contact), contact_statuses=CONTACT_STATUSES,
        can_handoff=bool(account["owner_id"] and not blocked and db().execute(
            "SELECT 1 FROM users WHERE role = 'ae' AND active = 1 LIMIT 1",
        ).fetchone()),
        task_action_key=uuid4().hex, local_day=account_day(account).isoformat(),
    )


@bp.post("/accounts/<int:account_id>/contacts")
def add_contact(account_id):
    try:
        with transaction(db()):
            account = account_required(account_id, write=True)
            verify_revision(account)
            if suppression_reason(db(), account_id):
                abort(403, "Contact additions are restricted for this account.")
            fields = contact_fields()
            person_id = db().execute("""
                INSERT INTO people(account_id, name, title, email, phone, linkedin_url, persona)
                VALUES (:account_id, :name, :title, :email, :phone, :linkedin_url, :persona)
            """, dict(fields, account_id=account_id)).lastrowid
            if suppression_reason(db(), account_id, person_id):
                abort(403, "Contact additions are restricted by suppression.")
            audit(db(), g.crm_user["id"], account_id, "contact_add", {"person_id": person_id}, now())
    except sqlite3.IntegrityError as error:
        duplicate_contact_error(error)
        raise
    flash("Contact added.", "success")
    return redirect(url_for("account_detail", account_id=account_id), code=303)


@bp.post("/accounts/<int:account_id>/contacts/<int:person_id>")
def edit_contact(account_id, person_id):
    try:
        with transaction(db()):
            account, contact, _ = contact_required(account_id, person_id, write=True)
            verify_contact_revision(account, contact)
            fields = contact_fields(contact)
            if contact_email_locked(contact) and fields["email"] != contact["email"]:
                abort(409, "This contact's email is tied to saved verification or source evidence.")
            changed = [key for key, value in fields.items() if value != contact[key]]
            if contact["origin"] == "apollo" and not contact["unlocked"] and {"phone", "linkedin_url"}.intersection(changed):
                abort(409, "This Apollo candidate is locked.")
            if changed:
                db().execute("""
                    UPDATE people SET name = :name, title = :title, email = :email, phone = :phone,
                                      linkedin_url = :linkedin_url, persona = :persona, details_revision = details_revision + 1
                    WHERE account_id = :account_id AND id = :person_id
                """, dict(fields, account_id=account_id, person_id=person_id))
                if suppression_reason(db(), account_id, person_id):
                    abort(403, "Contact changes are restricted by suppression.")
                audit(db(), g.crm_user["id"], account_id, "contact_edit", {"person_id": person_id, "fields": changed}, now())
    except sqlite3.IntegrityError as error:
        duplicate_contact_error(error)
        raise
    flash("Contact details saved.", "success")
    return redirect(url_for("workflow.contact_detail", account_id=account_id, person_id=person_id), code=303)


@bp.post("/accounts/<int:account_id>/contacts/<int:person_id>/status")
def update_contact_status(account_id, person_id):
    status = request.form.get("status", "")
    if status not in CONTACT_STATUSES:
        abort(400, "Choose a contact status.")
    with transaction(db()):
        account, contact, _ = contact_required(account_id, person_id, write=True)
        verify_contact_revision(account, contact)
        if request.form.get("expected_status") != contact["status"]:
            abort(409, "The outreach status has changed. Refresh and retry.")
        if contact["status"] != status:
            blocked = status == "do_not_contact"
            db().execute("""
                UPDATE people SET status = ?, do_not_contact = ?, details_revision = details_revision + 1
                WHERE account_id = ? AND id = ?
            """, (status, int(blocked), account_id, person_id))
            if blocked and contact["email"]:
                db().execute("""
                    INSERT OR IGNORE INTO suppression(kind, value, reason, created_by, created_at)
                    VALUES ('email', ?, 'Marked do not contact', ?, ?)
                """, (contact["email"], g.crm_user["id"], timestamp(now())))
            audit(db(), g.crm_user["id"], account_id, "contact_status",
                  {"person_id": person_id, "previous_status": contact["status"], "status": status}, now())
    flash("Contact status saved.", "success")
    visibility = {"show_suppressed": 1} if status == "do_not_contact" else {}
    return redirect(url_for("workflow.contact_detail", account_id=account_id, person_id=person_id, **visibility), code=303)


@bp.post("/accounts/<int:account_id>/tasks")
def create_task(account_id):
    user = g.crm_user
    title = submitted("title", max_length=180, required=True)
    due_on = submitted("due_on", max_length=10, required=True)
    key = submitted("action_key", max_length=64, required=True)
    person_key = submitted("person_id", max_length=32)
    request_hash = hashlib.sha256(json.dumps([account_id, person_key, title, due_on]).encode()).hexdigest()
    with transaction(db()):
        account = account_required(account_id, write=True)
        earlier = db().execute("""
            SELECT person_id, request_hash FROM tasks WHERE created_by = ? AND request_key = ?
        """, (user["id"], key)).fetchone()
        if earlier is not None:
            if earlier["request_hash"] != request_hash:
                abort(409, "This form was used for another follow-up. Open a fresh form.")
            person_id = earlier["person_id"]
        else:
            verify_revision(account)
            task_date(due_on, relative_to=account_day(account))
            person = selected_person(account_id)
            person_id = person["id"] if person else None
            blocked = suppression_reason(db(), account_id, person_id)
            if blocked:
                abort(403, f"Outreach is blocked. {blocked}.")
            task_id = db().execute("""
                INSERT INTO tasks(account_id, person_id, owner_id, title, due_on, created_by, request_key, request_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (account_id, person_id, account["owner_id"], title, due_on, user["id"], key, request_hash)).lastrowid
            audit(db(), user["id"], account_id, "task_created", {
                "task_id": task_id, "person_id": person_id, "owner_id": account["owner_id"], "due_on": due_on,
            }, now())
    flash("Follow-up added.", "success")
    if request.form.get("return_to") == "contact" and person_id is not None:
        return redirect(url_for("workflow.contact_detail", account_id=account_id, person_id=person_id), code=303)
    return redirect(url_for("account_detail", account_id=account_id, _anchor="account-tasks-heading"), code=303)


@bp.post("/tasks/<int:task_id>")
def update_task(task_id):
    action = request.form.get("action")
    with transaction(db()):
        task = db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            abort(404)
        account = account_required(task["account_id"], write=True)
        if action == "complete":
            db().execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
        elif action == "snooze":
            blocked = suppression_reason(db(), task["account_id"], task["person_id"])
            if blocked:
                abort(403, f"Outreach is blocked. {blocked}.")
            today_for_owner = account_day(account)
            due_on = task_date(request.form.get("due_on") or (today_for_owner + timedelta(days=1)).isoformat(),
                               relative_to=today_for_owner)
            db().execute("UPDATE tasks SET status = 'snoozed', due_on = ? WHERE id = ? AND status != 'done'",
                         (due_on, task_id))
        else:
            abort(400, "Choose a task action.")
    return redirect(url_for("workflow.today"), code=303)


@bp.post("/replies/<int:activity_id>/acknowledge")
def acknowledge_reply(activity_id):
    with transaction(db()):
        activity = db().execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
        if not activity or activity["outcome"] not in REPLIES:
            abort(404)
        account_required(activity["account_id"], write=True)
        db().execute("""
            UPDATE activities SET acknowledged_at = ?, acknowledged_by = ?
            WHERE id = ? AND acknowledged_at IS NULL
        """, (timestamp(now()), g.crm_user["id"], activity_id))
    return redirect(url_for("workflow.today"), code=303)
