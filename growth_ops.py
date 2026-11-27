"""Flask adapters for qualification and credit-controlled enrichment."""

import csv
import io
import json
from contextlib import contextmanager
from html.parser import HTMLParser

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from crm_core.growth import (
    DEFAULT_RULES, QUALIFICATION_BATCH_SIZE, QUALIFICATION_SCOPES, GrowthConflict, GrowthInvalid, GrowthMissing, GrowthRestricted,
    account_credit_access, add_suppression_rules, audited_source_report, change_daily_quota, change_qualification_rules,
    enrich_next, fit_satisfies, local_day, manual_qualification, queue_enrichment, queue_qualification_job,
    remove_suppression_rule, request_enrichment, require_admin, retry_enrichment, retry_qualification_job, run_qualification_batch,
    score_qualification, select_credit_job,
)
from crm_core.suppression_metadata import REASON_LABELS
from growth_store import OfflineApollo, SqlGrowthStore, initialize
from worker_status import last_pass
from workflow_store import utcnow
from workflow_web import account_required, db, list_params, now, staff_required, submitted

bp = Blueprint("growth_ops", __name__)
SUPPRESSION_SOURCE_LABELS = {
    "manual": "Manual entry", "csv": "CSV", "outreach": "CRM outreach",
    "sequencer": "Sequencer", "seed": "Starter rule", "legacy": "Legacy rule",
}


@contextmanager
def _errors():
    try:
        yield
    except GrowthInvalid as error:
        abort(400, str(error))
    except GrowthConflict as error:
        abort(409, str(error))
    except GrowthRestricted as error:
        abort(403, description=str(error) or None)
    except GrowthMissing:
        abort(404)


class PageEvidence(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headings = []
        self.links = []
        self.heading = None

    def handle_starttag(self, tag, attrs):
        if tag in {"title", "h1", "h2", "h3"}:
            self.heading = tag
        if tag == "a":
            link = dict(attrs).get("href", "")
            if link:
                self.links.append(link)

    def handle_endtag(self, tag):
        if tag == self.heading:
            self.heading = None

    def handle_data(self, data):
        if self.heading and data.strip():
            self.headings.append(data.strip())


def qualify(
    connection, account, *, observed, day=None, config, page=None, page_url=None,
    planning=(4, 9), update_stage=True,
):
    parsed = PageEvidence()
    if page:
        parsed.feed(page)
    return score_qualification(SqlGrowthStore(connection), account, observed=observed, day=day, config=config,
                               headings=parsed.headings, links=parsed.links, page_url=page_url,
                               planning=planning, update_stage=update_stage)


def rules(connection):
    return SqlGrowthStore(connection).rules()


def qualify_batch(connection, *, clock=utcnow):
    def scorer(account, **options):
        return qualify(connection, account, **options)

    return run_qualification_batch(SqlGrowthStore(connection), scorer, clock=clock, limit=QUALIFICATION_BATCH_SIZE)


def meets_fit(connection, account_id, config):
    return fit_satisfies(SqlGrowthStore(connection).qualification(account_id), config)


def credit_usage(connection, user_id, day):
    return SqlGrowthStore(connection).credit_usage(user_id, day)


def auto_usage(connection, day):
    return SqlGrowthStore(connection).auto_usage(day)


def queue_job(connection, account, person_id, user, *, observed, auto=False):
    return queue_enrichment(SqlGrowthStore(connection), account, person_id, user, observed=observed, auto=auto)


def claimable_job(connection, observed):
    return select_credit_job(SqlGrowthStore(connection), observed)


def enrich_one(connection, provider, *, clock=utcnow):
    return enrich_next(SqlGrowthStore(connection), provider, clock=clock)


def apollo_web_status():
    if not current_app.config.get("APOLLO_WEB_ENABLED"):
        return None
    return {"ready": current_app.config.get("APOLLO_PROVIDER") is not None,
            "paid": current_app.config.get("APOLLO_ALLOW_PAID", False)}


def web_enrichment_provider(person_id):
    status = apollo_web_status()
    if status is None:
        return None
    if person_id and not status["paid"]:
        abort(403, "Paid Apollo email unlock is disabled.")
    if not status["ready"]:
        abort(503, "Apollo needs to be configured for this CRM.")
    return current_app.config["APOLLO_PROVIDER"]


def account_context(connection, account_id, user):
    store = SqlGrowthStore(connection)
    qualification = store.qualification(account_id)
    config = store.rules()
    apollo = apollo_web_status()
    return {
        "qualification": dict(qualification, signals=json.loads(qualification["signals"])) if qualification else None,
        "jobs": store.account_jobs(account_id),
        "apollo": apollo,
        "previous_search_id": store.empty_search_job_id(account_id) if apollo and apollo["ready"] else None,
        **account_credit_access(store, user, qualification, config, observed=now() if user["role"] == "sdr" else None),
    }


@bp.get("/operations")
def index():
    viewer = staff_required()
    connection = db()
    store = SqlGrowthStore(connection)
    observed = now()
    jobs = store.jobs_for_display()
    selected = request.args.get("filter", "all")
    rows = jobs if selected == "all" else [row for row in jobs if row["state"] == selected]
    context = list_params(rows, lambda row: f"{row['name']} {row['actor']} {row['state']}", {
        "recent": lambda row: -row["id"], "account": lambda row: row["name"].casefold(),
    }, "recent")
    return render_template(
        "operations.html", **context, selected=selected, workload=store.workload(observed),
        apollo=apollo_web_status(),
        last_worker_pass=last_pass(connection),
        coverage=store.coverage(local_day(viewer, observed).isoformat()), config=store.rules(),
        funnel=store.source_funnel(), qualification_jobs=store.qualification_jobs(),
        qualification_scopes=QUALIFICATION_SCOPES, qualification_active=bool(store.active_qualification_job()),
    )


@bp.post("/operations/qualification-jobs")
def queue_qualification():
    user = staff_required()
    scope = request.form.get("scope", "all")
    planning = (current_app.config["PLANNING_MONTHS_MIN"], current_app.config["PLANNING_MONTHS_MAX"])
    store = SqlGrowthStore(db())
    with _errors(), store.atomic():
        job_id = queue_qualification_job(store, user, scope, now(), planning)
    flash("Qualification queued." if job_id else "A qualification job is already active.",
          "success" if job_id else "error")
    return redirect(url_for("growth_ops.index"), code=303)


@bp.post("/operations/qualification-jobs/<int:job_id>/retry")
def retry_qualification(job_id):
    user = staff_required()
    store = SqlGrowthStore(db())
    with _errors(), store.atomic():
        retry_qualification_job(store, user, job_id, now())
    return redirect(url_for("growth_ops.index"), code=303)


def source_funnel(connection):
    return SqlGrowthStore(connection).source_funnel()


@bp.post("/operations/source-report")
def export_source_funnel():
    user = admin()
    store = SqlGrowthStore(db())
    with store.atomic():
        rows = audited_source_report(store, user, now())
    body = io.StringIO()
    writer = csv.writer(body)
    writer.writerow(("Source", "Discovered", "Scored", "Assigned", "Worked", "Positive", "Delivered", "Credits"))
    for row in rows:
        writer.writerow(row.values())
    return Response(body.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=nunify-source-funnel.csv",
    })


@bp.post("/operations/users/<int:user_id>/quota")
def set_quota(user_id):
    user = admin()
    quota = request.form.get("quota", type=int)
    store = SqlGrowthStore(db())
    with _errors(), store.atomic():
        change_daily_quota(store, user, user_id, quota, now())
    return redirect(url_for("growth_ops.index"), code=303)


@bp.post("/accounts/<int:account_id>/qualify")
def qualify_account(account_id):
    user = staff_required()
    upload = request.files.get("evidence")
    page_url = submitted("evidence_url", max_length=1000)
    page = None
    if upload and upload.filename:
        raw = upload.read(1_000_001)
        if len(raw) > 1_000_000 or not page_url.startswith(("https://", "http://")):
            abort(400, "Provide an HTTP evidence URL and a saved HTML file up to 1 MB.")
        page = raw.decode("utf-8", errors="replace")
    connection = db()
    store = SqlGrowthStore(connection)

    def scorer(account, **options):
        return qualify(connection, account, page=page, page_url=page_url, **options)

    with store.atomic():
        account = account_required(account_id)
        warning = manual_qualification(store, account, user, scorer, clock=now)
    if warning:
        flash(warning, "error")
    flash("Qualification saved.", "success")
    return redirect(url_for("account_detail", account_id=account_id), code=303)


@bp.post("/accounts/<int:account_id>/enrich")
def enrich(account_id):
    user = g.crm_user
    person_id = request.form.get("person_id", type=int)
    if "person_id" in request.form and (person_id is None or person_id <= 0):
        abort(400, "Choose a contact for email unlock.")
    previous_search_id = request.form.get("previous_search_id", type=int)
    if "previous_search_id" in request.form and (person_id or not previous_search_id or previous_search_id < 0):
        abort(400, "Choose a previous empty search to run again.")
    store = SqlGrowthStore(db())
    with _errors(), store.atomic():
        account = account_required(account_id, write=True)
        provider = web_enrichment_provider(person_id)
        if previous_search_id is not None and provider is None:
            abort(403, "Apollo must be configured to repeat a search.")
        observed = now()
        queued = request_enrichment(store, account, person_id, user, observed=observed,
                                    previous_search_id=previous_search_id)
        job_id = store.requested_credit_job_id(account_id, person_id, user["id"], observed) if provider else None
    if job_id is not None:
        enrich_next(store, provider, clock=now, job_id=job_id)
    else:
        flash("Enrichment queued." if queued else "This request is already recorded.", "success")
    return redirect(url_for("account_detail", account_id=account_id), code=303)


@bp.post("/operations/credits/<int:job_id>/retry")
def retry(job_id):
    user = staff_required()
    store = SqlGrowthStore(db())
    with _errors(), store.atomic():
        retry_enrichment(store, user, job_id, now())
        provider = web_enrichment_provider(store.credit_job(job_id)["person_id"])
    if provider is not None:
        enrich_next(store, provider, clock=now, job_id=job_id)
    return redirect(url_for("growth_ops.index"), code=303)


def admin():
    user = staff_required()
    with _errors():
        require_admin(user)
    return user


@bp.post("/operations/qualification-settings")
def edit_rules():
    user = admin()
    store = SqlGrowthStore(db())
    with _errors(), store.atomic():
        change_qualification_rules(
            store, user, request.form.get("terms", ""), request.form.get("platform_domains", ""),
            request.form.get("daily_auto_budget", "0"), request.form.get("auto_enabled") == "1", now(),
        )
    return redirect(url_for("growth_ops.index"), code=303)


@bp.get("/suppression")
def suppression():
    admin()
    selected = request.args.get("filter", "all")
    rows = [dict(row) for row in SqlGrowthStore(db()).rules_for_display() if selected in {"all", row["kind"]}]
    for row in rows:
        row["reason_label"] = REASON_LABELS[row["reason"]]
        row["source_label"] = SUPPRESSION_SOURCE_LABELS.get(row["source"], row["source"])
    context = list_params(rows, lambda row: " ".join(
        row[field] for field in ("value", "reason", "reason_label", "reason_note", "source_label")
    ), {
        "newest": lambda row: -row["id"], "value": lambda row: row["value"],
    }, "newest")
    return render_template("suppression.html", **context, selected=selected, reason_labels=REASON_LABELS)


@bp.post("/suppression")
def add_rule():
    user = admin()
    upload = request.files.get("rules")
    if upload and upload.filename:
        source = "csv"
        raw = upload.read(128_001)
        if len(raw) > 128_000:
            abort(400, "Use a suppression CSV under 128 KB.")
        try:
            rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), strict=True)
            fields = rows.fieldnames or []
            expected = {"kind", "value", "reason"}
            if not expected <= set(fields) <= expected | {"reason_note"} or len(fields) != len(set(fields)):
                abort(400, "Use CSV columns kind, value, reason, and an optional reason_note.")
            entries = []
            for row in rows:
                if None in row or None in row.values():
                    abort(400, f"CSV row {rows.line_num} does not match the header.")
                entries.append((row["kind"], row["value"], row["reason"], row.get("reason_note", "")))
        except (UnicodeError, csv.Error):
            abort(400, "Upload a valid UTF-8 suppression CSV.")
        if not entries:
            abort(400, "Include at least one suppression rule in the CSV.")
    else:
        source = "manual"
        entries = [(
            submitted("kind", max_length=10), submitted("value", max_length=254),
            submitted("reason", max_length=180), submitted("reason_note", max_length=180),
        )]
    store = SqlGrowthStore(db())
    with _errors(), store.atomic():
        add_suppression_rules(store, user, entries, source=source, clock=now)
    flash("Suppression rules saved.", "success")
    return redirect(url_for("growth_ops.suppression"), code=303)


@bp.post("/suppression/<int:rule_id>/remove")
def remove_rule(rule_id):
    user = admin()
    store = SqlGrowthStore(db())
    with store.atomic():
        remove_suppression_rule(store, user, rule_id, now())
    return redirect(url_for("growth_ops.suppression"), code=303)
