"""Authenticated assignment, account activity, and rep queues."""

import hmac
import secrets
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)
from itsdangerous import BadSignature, URLSafeSerializer
from werkzeug.security import check_password_hash

from account_redirects import canonical_account_id
from account_store import SqlAccountAccess, active_session_user, user_for_login
from activity_store import SqlActivityStore
from assignment_store import SqlAssignmentStore
from crm_core.activity import (
    ActivityConflict, ActivitySubmission, InvalidActivity, OutreachBlocked, activity_direction, record_activity,
)
from crm_core.agenda import queue_items
from crm_core.assignment import (
    AllocationDenied, AssignmentBlocked, InvalidAssignment, StaleAssignment, apply_round_robin, assign_account,
    assign_selected, assignment_rows, bulk_selection, pool_accounts, require_active_owner, require_preview_owner,
    require_revision, round_robin_plan,
)
from crm_core.access import (
    AccountNotFound, AccountUnassigned, SalesQueueDenied, STAFF, require_account, require_sales_queue,
    uses_unassigned_pool, visible_accounts,
)
from crm_core.contacts import (
    CONTACT_STATUSES, ContactConflict, ContactMissing, ContactRestricted, InvalidContact, add_contact as add_contact_rule,
    can_handoff_contact, contact_for_view, contact_history, contact_rows, edit_contact as edit_contact_rule, email_locked,
    find_contact, require_status, update_status,
)
from crm_core.tasks import (
    InvalidTask, MissingWork, TaskConflict, TaskSubmission, account_date, acknowledge_reply as acknowledge_reply_rule,
    create_task as create_task_rule, find_reply, find_task, update_task as update_task_rule,
)
from crm_store import connect
from source_ops import account_sources
from work_store import SqlWorkStore
from workflow_store import account_rows, local_date, transaction, utcnow


bp = Blueprint("workflow", __name__)
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
LABELS = {
    "call": "Call", "email": "Email", "linkedin": "LinkedIn", "note": "Note",
    "not_contacted": "Not contacted", "do_not_contact": "Do not contact",
    "apollo": "Apollo", "apollo_offline": "Offline enrichment", "cvent": "Cvent event",
    "cvent_event": "Cvent event", "directory": "Directory of Associations",
    "demo": "Demo", "manual": "Manual entry", "source": "Imported source",
}


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
    g.crm_user = active_session_user(db(), session.get("user_id"))
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
    return account_date(account, g.crm_user["timezone"], at if at is not None else now())


def account_required(account_id, *, write=False):
    user = g.crm_user
    observed = now()
    try:
        return require_account(SqlAccountAccess(db()), user, account_id, local_date(user, observed).isoformat(),
                               observed, write=write)
    except AccountNotFound:
        abort(404)
    except PermissionError:
        abort(403)
    except AccountUnassigned as error:
        abort(409, str(error))


def show_suppressed():
    return request.args.get("show_suppressed") == "1"


def readable_accounts(*, include_suppressed=False):
    user = g.crm_user
    if not user:
        return []
    observed = now()
    return visible_accounts(SqlAccountAccess(db()), user, local_date(user, observed).isoformat(), observed,
                            include_suppressed=include_suppressed)


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


def activity_context(account):
    store = SqlWorkStore(db())
    account_id = account["id"]
    include_suppressed = show_suppressed()
    contacts, has_suppressed = contact_rows(store, {account_id: account}, store.people_on_account(account_id),
                                            include_suppressed)
    for contact in contacts:
        contact["origin_label"] = label(contact["origin"]) if contact["origin"] else "Source unavailable"
    grant = store.coverage_for(account, now())
    due_day = account_day(account)
    return {
        "work": dict(
            account, can_work=bool(account["owner_id"]), blocked=store.restriction_for(account_id),
            active_coverage=dict(grant) if grant else None,
        ),
        "contacts": contacts, "has_suppressed_contacts": has_suppressed, "show_suppressed": include_suppressed,
        "activities": store.account_timeline(account_id), "tasks": store.account_tasks(account_id), "action_key": uuid4().hex,
        "task_action_key": uuid4().hex,
        "local_day": due_day.isoformat(),
        "due_timezone": account["owner_timezone"] or g.crm_user["timezone"],
    }


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = user_for_login(db(), request.form.get("email", "").strip())
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
    with _work_request():
        require_sales_queue(g.crm_user)
    bucket = request.args.get("bucket", "today")
    if bucket not in {"today", "tomorrow"}:
        bucket = "today"
    day = user_day()
    queue_day = day + timedelta(days=1) if bucket == "tomorrow" else day
    accounts = readable_accounts()
    rows = queue_items(SqlWorkStore(db()), accounts, bucket, day, g.crm_user["timezone"], now(),
                       current_app.config["PLANNING_MONTHS_MIN"], current_app.config["PLANNING_MONTHS_MAX"])
    for row in rows:
        if row["reply_id"] is not None:
            outcome = label(row["outcome"])
            row["title"] = f"{outcome} from {row['contact_name']}" if row["contact_name"] else outcome
    context = list_params(rows, lambda row: f"{row['title']} {row['account_name']} {row['reason']}", {
        "priority": lambda row: (row["priority"], row["due_on"] or "", row["account_name"]),
        "account": lambda row: (row["account_name"].casefold(), row["priority"]),
        "due": lambda row: (row["due_on"] or "9999", row["priority"]),
    }, "priority")
    return render_template("today.html", **context, local_day=day.isoformat(),
                           queue_day=queue_day.isoformat(), bucket=bucket, has_work_accounts=bool(accounts))


@bp.get("/my-accounts")
def my_accounts():
    with _work_request():
        require_sales_queue(g.crm_user)
    selected = request.args.get("filter", "all").casefold()
    pool = uses_unassigned_pool(g.crm_user, selected)
    include_suppressed = show_suppressed() and not pool
    if pool:
        rows = pool_accounts(account_rows(db(), user_day().isoformat()))
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
    with _work_request():
        require_sales_queue(g.crm_user)
    include_suppressed = show_suppressed()
    accounts = {account["id"]: account for account in readable_accounts(include_suppressed=include_suppressed)}
    store = SqlWorkStore(db())
    visible, _ = contact_rows(store, accounts, store.people_on_accounts(accounts), include_suppressed)
    for person in visible:
        person["origin_label"] = label(person["origin"]) if person["origin"] else "Source unavailable"
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
    reps = SqlAssignmentStore(db()).rep_workloads(now())
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
    source = request.args.get("source", "all")
    if source not in dict(ASSIGNMENT_SOURCES):
        source = "all"
    sources_by_account = account_sources(db())
    filtered_rows = assignment_rows(rows, sources_by_account, SqlWorkStore(db()).qualification_scores(),
                                    selected, source, owner, stage, organization_type)

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


def _selected_sdr(connection):
    owner_id = request.form.get("owner_id", type=int)
    require_active_owner(SqlAssignmentStore(connection), owner_id)
    return owner_id


@contextmanager
def _work_request():
    try:
        yield
    except (InvalidAssignment, InvalidContact, InvalidTask) as error:
        abort(400, str(error))
    except (AllocationDenied, ContactRestricted, OutreachBlocked, SalesQueueDenied) as error:
        abort(403, str(error) or None)
    except (AssignmentBlocked, ContactConflict, StaleAssignment, TaskConflict) as error:
        abort(409, str(error))
    except (ContactMissing, MissingWork):
        abort(404)


@bp.post("/assignment/<int:account_id>")
def assign(account_id):
    user = staff_required()
    connection = db()
    with _work_request(), transaction(connection):
        account = account_required(account_id)
        require_revision(account, request.form.get("revision", type=int))
        owner_id = _selected_sdr(connection)
        assign_account(SqlAssignmentStore(connection), account, user["id"], owner_id, now())
    flash(f"Assignment saved for {account['name']}.", "success")
    return redirect(url_for("workflow.assignment", **_assignment_filters()), code=303)


def _bulk_selection():
    return bulk_selection(
        request.form.getlist("account"),
        request.form.getlist("unassigned_on_page") if request.form.get("assign_page") == "yes" else [],
    )


@bp.post("/assignment/bulk")
def assign_bulk():
    user = staff_required()
    connection = db()
    with _work_request():
        changes, explicit = _bulk_selection()
        with transaction(connection):
            selection = ((account_required(account_id), rev) for account_id, rev in changes.items())
            count = assign_selected(SqlAssignmentStore(connection), selection, explicit,
                                    request.form.get("allow_transfer") == "yes", _selected_sdr(connection), user["id"], now())
    flash(
        f"Updated ownership for {count} {'account' if count == 1 else 'accounts'}."
        if count else "The selected accounts already have that owner.",
        "success",
    )
    return redirect(url_for("workflow.assignment", **_assignment_filters()), code=303)


@bp.post("/assignment/round-robin")
def assign_round_robin():
    user = staff_required()
    signer = URLSafeSerializer(current_app.secret_key, salt="round-robin-assignment-v1")
    confirmation = request.form.get("allocation")
    with _work_request():
        if confirmation:
            try:
                previous = signer.loads(confirmation)
            except BadSignature:
                abort(400, "Preview the round-robin allocation again before confirming.")
            require_preview_owner(previous, user["id"])
            selection, rep_ids, cap = previous["selection"], previous["rep_ids"], previous["cap"]
        else:
            selected, _ = _bulk_selection()
            selection = [[account_id, revision] for account_id, revision in selected.items()]
            rep_ids = request.form.getlist("rep_id", type=int)
            if not rep_ids or len(rep_ids) != len(request.form.getlist("rep_id")):
                abort(400, "Choose the SDRs for the round-robin allocation.")
            rep_ids = sorted(set(rep_ids))
            cap = request.form.get("cap", type=int)

        connection = db()
        store = SqlAssignmentStore(connection)
        with transaction(connection):
            accounts = ((account_required(account_id), rev) for account_id, rev in selection)
            groups, unallocated, snapshot = round_robin_plan(store.rep_workloads(now()), accounts, rep_ids, cap,
                                                             store.restriction_for)
            count = len(selection) - unallocated
            if confirmation:
                apply_round_robin(store, groups, snapshot, previous["snapshot"], user["id"], now())

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


@bp.post("/accounts/<int:account_id>/activities")
def log_activity(account_id):
    user = g.crm_user
    data = ActivitySubmission(
        actor_id=user["id"], actor_timezone=user["timezone"],
        key=submitted("action_key", max_length=64, required=True), revision=request.form.get("revision", type=int),
        channel=submitted("channel", max_length=20, required=True),
        outcome=submitted("outcome", max_length=30, required=True), note=submitted("note", max_length=4000),
        person_key=request.form.get("person_id", ""), due_on=submitted("due_on", max_length=10),
        due_tomorrow=request.form.get("due_tomorrow") == "1", next_step=submitted("next_step", max_length=180),
    )
    try:
        activity_direction(data.channel, data.outcome)
        connection = db()
        with transaction(connection):
            account = account_required(account_id, write=True)
            record_activity(SqlActivityStore(connection), account, data, now())
    except InvalidActivity as error:
        abort(400, str(error))
    except OutreachBlocked as error:
        abort(403, str(error))
    except (ActivityConflict, StaleAssignment) as error:
        abort(409, str(error))
    flash("Activity saved.", "success")
    return redirect(url_for("account_detail", account_id=account_id), code=303)


@bp.get("/accounts/<int:account_id>/contacts/<int:person_id>")
def contact_detail(account_id, person_id):
    active_id = canonical_account_id(db(), account_id)
    account = account_required(active_id)
    store = SqlWorkStore(db())
    with _work_request():
        row, blocked = contact_for_view(store, active_id, person_id, show_suppressed())
    if active_id != account_id:
        options = {"show_suppressed": 1} if show_suppressed() else {}
        return redirect(url_for("workflow.contact_detail", account_id=active_id, person_id=person_id, **options))
    contact = dict(row, origin_label=label(row["origin"]))
    activities, tasks, evidence = contact_history(store, account_id, person_id, blocked)
    return render_template(
        "contact.html", account=account, contact=contact, blocked=blocked, activities=activities, tasks=tasks,
        evidence=evidence, email_locked=email_locked(contact), contact_statuses=CONTACT_STATUSES,
        can_handoff=can_handoff_contact(account, blocked, store.active_ae_exists),
        task_action_key=uuid4().hex, local_day=account_day(account).isoformat(),
    )


@bp.post("/accounts/<int:account_id>/contacts")
def add_contact(account_id):
    connection = db()
    with _work_request(), transaction(connection):
        account = account_required(account_id, write=True)
        add_contact_rule(SqlWorkStore(connection), account, g.crm_user["id"], request.form.get("revision", type=int),
                         request.form.to_dict(), now())
    flash("Contact added.", "success")
    return redirect(url_for("account_detail", account_id=account_id), code=303)


@bp.post("/accounts/<int:account_id>/contacts/<int:person_id>")
def edit_contact(account_id, person_id):
    connection = db()
    store = SqlWorkStore(connection)
    with _work_request(), transaction(connection):
        account = account_required(account_id, write=True)
        contact = find_contact(store, account_id, person_id)
        edit_contact_rule(store, account, contact, g.crm_user["id"], request.form.get("revision", type=int),
                          request.form.get("details_revision", type=int), request.form.to_dict(), now())
    flash("Contact details saved.", "success")
    return redirect(url_for("workflow.contact_detail", account_id=account_id, person_id=person_id), code=303)


@bp.post("/accounts/<int:account_id>/contacts/<int:person_id>/status")
def update_contact_status(account_id, person_id):
    status = request.form.get("status", "")
    with _work_request():
        require_status(status)
        connection = db()
        store = SqlWorkStore(connection)
        with transaction(connection):
            account = account_required(account_id, write=True)
            contact = find_contact(store, account_id, person_id)
            update_status(store, account, contact, g.crm_user["id"], request.form.get("revision", type=int),
                          request.form.get("details_revision", type=int), request.form.get("expected_status"), status, now())
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
    data = TaskSubmission(user["id"], user["timezone"], request.form.get("revision", type=int), key, person_key,
                          title, due_on)
    connection = db()
    with _work_request(), transaction(connection):
        account = account_required(account_id, write=True)
        person_id = create_task_rule(SqlWorkStore(connection), account, data, now())
    flash("Follow-up added.", "success")
    if request.form.get("return_to") == "contact" and person_id is not None:
        return redirect(url_for("workflow.contact_detail", account_id=account_id, person_id=person_id), code=303)
    return redirect(url_for("account_detail", account_id=account_id, _anchor="account-tasks-heading"), code=303)


@bp.post("/tasks/<int:task_id>")
def update_task(task_id):
    action = request.form.get("action")
    connection = db()
    store = SqlWorkStore(connection)
    with _work_request(), transaction(connection):
        task = find_task(store, task_id)
        account = account_required(task["account_id"], write=True)
        update_task_rule(store, task, account, action, request.form.get("due_on"), g.crm_user["timezone"], now())
    return redirect(url_for("workflow.today"), code=303)


@bp.post("/replies/<int:activity_id>/acknowledge")
def acknowledge_reply(activity_id):
    connection = db()
    store = SqlWorkStore(connection)
    with _work_request(), transaction(connection):
        activity = find_reply(store, activity_id)
        account_required(activity["account_id"], write=True)
        acknowledge_reply_rule(store, activity, g.crm_user["id"], now())
    return redirect(url_for("workflow.today"), code=303)
