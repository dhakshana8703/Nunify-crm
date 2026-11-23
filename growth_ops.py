"""Offline qualification and credit-controlled enrichment."""

import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from uuid import uuid4

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from workflow_store import account_relation, account_rows, audit, is_blocked, local_date, timestamp, transaction, utcnow
from workflow_web import (
    EMAIL, account_required, db, list_params, now, offset_months, staff_required, submitted,
)


bp = Blueprint("growth_ops", __name__)
QUALIFICATION_BATCH_SIZE = 50
QUALIFICATION_SCOPES = {
    "all": "All saved-source accounts",
    "cvent": "Cvent",
    "directory": "Directory of Associations",
    "both": "Both sources",
}
DEFAULT_RULES = {
    "conference_terms": ["conference", "annual meeting", "summit", "convention", "expo", "symposium"],
    "platform_domains": {"cvent.com": "Cvent", "cventevents.com": "Cvent", "swoogo.com": "Swoogo",
                         "eventbrite.com": "Eventbrite", "whova.com": "Whova"},
    "qualified_at": 40,
    "auto_at": 60,
    "daily_auto_budget": 10,
    "auto_enabled": False,
    "persona_titles": {"association": "Director of Meetings", "corporate": "Head of Events",
                       "agency": "Head of Production", "organizer": "Event Director",
                       "university": "Director of Conferences", "other": "Event Lead"},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS qualifications (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    score INTEGER NOT NULL,
    signals TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    override INTEGER CHECK (override IN (0, 1))
);
CREATE TABLE IF NOT EXISTS qualification_jobs (
    id INTEGER PRIMARY KEY,
    requested_by INTEGER NOT NULL REFERENCES users(id),
    scope TEXT NOT NULL CHECK (scope IN ('all', 'cvent', 'directory', 'both')),
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'failed', 'done')),
    policy_json TEXT NOT NULL,
    planning_start INTEGER NOT NULL,
    planning_end INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    observed_day TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    last_account_id INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    scored INTEGER NOT NULL DEFAULT 0,
    qualified INTEGER NOT NULL DEFAULT 0,
    suppressed INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS qualification_jobs_one_active
    ON qualification_jobs((1)) WHERE state IN ('queued', 'running');
CREATE TABLE IF NOT EXISTS qualification_job_accounts (
    job_id INTEGER NOT NULL REFERENCES qualification_jobs(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    PRIMARY KEY (job_id, account_id)
);
CREATE TABLE IF NOT EXISTS credit_jobs (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    person_id INTEGER REFERENCES people(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL CHECK (action IN ('people_search', 'email_unlock')),
    state TEXT NOT NULL CHECK (state IN ('queued', 'processing', 'failed', 'done', 'blocked')),
    on_day TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    request_key TEXT NOT NULL UNIQUE,
    auto INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    lease_until TEXT,
    lease_token TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS credit_jobs_open
ON credit_jobs(account_id, action, COALESCE(person_id, 0)) WHERE state IN ('queued', 'processing', 'failed');
CREATE TABLE IF NOT EXISTS credit_ledger (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE REFERENCES credit_jobs(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    person_id INTEGER REFERENCES people(id),
    action TEXT NOT NULL,
    credits INTEGER NOT NULL CHECK (credits > 0),
    on_day TEXT NOT NULL,
    at TEXT NOT NULL
);
"""


def initialize(connection):
    connection.executescript(SCHEMA)
    with transaction(connection):
        fields = {row["name"] for row in connection.execute("PRAGMA table_info(credit_jobs)")}
        if "budget_day" not in fields:
            connection.execute("ALTER TABLE credit_jobs ADD COLUMN budget_day TEXT NOT NULL DEFAULT ''")
            connection.execute("UPDATE credit_jobs SET budget_day = substr(created_at, 1, 10)")


def rules(connection):
    row = connection.execute("SELECT value FROM workflow_settings WHERE key = 'qualification'").fetchone()
    return json.loads(row["value"]) if row else DEFAULT_RULES


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
    from urllib.parse import urlsplit

    account_id = account["id"]
    source_rows = connection.execute("""
        SELECT source, source_url, details_json FROM source_records WHERE account_id = ? ORDER BY id
    """, (account_id,)).fetchall()
    cvent = [row for row in source_rows if row["source"] == "cvent"]
    directories = [row for row in source_rows if row["source"] == "directory"]
    headings = [(json.loads(row["details_json"]).get("event_name") or "", row["source_url"]) for row in cvent]
    signals = []

    def add(label, points, url=None):
        signals.append({"label": label, "points": points, "url": url})

    platform = ("Cvent", cvent[0]["source_url"]) if cvent else None
    if page:
        parsed = PageEvidence()
        parsed.feed(page)
        headings.extend((heading, page_url) for heading in parsed.headings)
        for link in parsed.links:
            if not link.startswith(("https://", "http://")):
                continue
            try:
                host = urlsplit(link).hostname or ""
            except ValueError:
                continue
            for domain, name in config["platform_domains"].items():
                if host == domain or host.endswith("." + domain):
                    if platform is None or name == "Cvent":
                        platform = (name, page_url)
    if platform:
        add(f"Registration platform - {platform[0]}", 30 if platform[0] == "Cvent" else 15, platform[1])
    for heading, evidence in headings:
        if any(re.search(r"\b" + re.escape(term) + r"\b", heading, re.I) for term in config["conference_terms"]):
            add(f"Event - {heading}", 20, evidence)
            break
    if cvent and directories:
        add("Appears in two sources", 10)
    for row in directories:
        value = str(json.loads(row["details_json"]).get("members") or "").replace(",", "")
        match = re.search(r"\d+", value)
        if match and int(match[0]) >= 1000:
            add("Directory membership", 15 if int(match[0]) >= 10000 else 8, row["source_url"])
            break
    day = day or observed.date()
    start, end = (offset_months(day, months).isoformat() for months in planning)
    if account.get("next_event") and start <= account["next_event"] <= end:
        add("Next event in the planning window", 20)
    score = sum(signal["points"] for signal in signals)
    connection.execute("""
        INSERT INTO qualifications(account_id, score, signals, observed_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET score = excluded.score,
            signals = excluded.signals, observed_at = excluded.observed_at
    """, (account_id, score, json.dumps(signals), timestamp(observed)))
    if update_stage:
        _apply_fit_stage(connection, account_id, meets_fit(connection, account_id, config))
    return score


def _apply_fit_stage(connection, account_id, qualified):
    connection.execute("""
        INSERT INTO account_work(account_id, stage, revision) VALUES (?, ?, 1)
        ON CONFLICT(account_id) DO UPDATE SET stage = excluded.stage, revision = account_work.revision + 1
        WHERE account_work.owner_id IS NULL
          AND account_work.stage IN ('discovered', 'qualified', 'disqualified')
          AND account_work.stage <> excluded.stage
    """, (account_id, "qualified" if qualified else "disqualified"))


def _active_qualification_job(connection):
    return connection.execute("""
        SELECT id FROM qualification_jobs WHERE state IN ('queued', 'running')
    """).fetchone()


def _queue_qualification_job(connection, user, scope, observed, planning):
    if _active_qualification_job(connection):
        return None
    config = rules(connection)
    job_id = connection.execute("""
        INSERT INTO qualification_jobs(requested_by, scope, policy_json, planning_start, planning_end,
                                       observed_at, observed_day)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user["id"], scope, json.dumps(config), *planning, timestamp(observed),
          local_date(user, observed).isoformat())).lastrowid
    target_count = connection.execute("""
        INSERT INTO qualification_job_accounts (job_id, account_id)
        SELECT :job_id, r.account_id FROM source_records r
        LEFT JOIN qualifications q ON q.account_id = r.account_id
        LEFT JOIN account_work w ON w.account_id = r.account_id
        WHERE r.account_id IS NOT NULL
          AND (q.account_id IS NULL OR (
              w.owner_id IS NULL
              AND COALESCE(w.stage, 'discovered') IN ('discovered', 'qualified', 'disqualified')
              AND COALESCE(w.stage, 'discovered') <> CASE
                  WHEN COALESCE(q.override = 1, q.score >= :cutoff) THEN 'qualified' ELSE 'disqualified' END
          ))
        GROUP BY r.account_id
        HAVING :scope = 'all'
            OR (:scope IN ('cvent', 'both') AND COUNT(CASE WHEN r.source = 'cvent' THEN 1 END) > 0
                AND (:scope != 'both' OR COUNT(CASE WHEN r.source = 'directory' THEN 1 END) > 0))
            OR (:scope = 'directory' AND COUNT(CASE WHEN r.source = 'directory' THEN 1 END) > 0)
    """, {"job_id": job_id, "scope": scope, "cutoff": config["qualified_at"]}).rowcount
    connection.execute("UPDATE qualification_jobs SET total = ? WHERE id = ?", (target_count, job_id))
    audit(connection, user["id"], None, "qualification_job_queued",
          {"job_id": job_id, "scope": scope, "accounts": target_count}, observed)
    return job_id


def qualify_batch(connection, *, clock=utcnow):
    """Process one atomic batch. A restarted worker resumes at the saved cursor."""
    job = None
    try:
        with transaction(connection):
            job = connection.execute("""
                SELECT * FROM qualification_jobs WHERE state IN ('queued', 'running')
            """).fetchone()
            if job is None:
                return None
            targets = connection.execute("""
                SELECT account_id FROM qualification_job_accounts
                WHERE job_id = ? AND account_id > ?
                ORDER BY account_id LIMIT ?
            """, (job["id"], job["last_account_id"], QUALIFICATION_BATCH_SIZE)).fetchall()
            config = json.loads(job["policy_json"])
            observed = datetime.fromisoformat(job["observed_at"])
            day = date.fromisoformat(job["observed_day"])
            planning = (job["planning_start"], job["planning_end"])
            scored = qualified = suppressed = 0
            for target in targets:
                account_id = target["account_id"]
                if is_blocked(connection, account_id):
                    suppressed += 1
                    continue
                if not connection.execute("SELECT 1 FROM qualifications WHERE account_id = ?", (account_id,)).fetchone():
                    account = account_rows(connection, job["observed_day"], account_id)
                    qualify(connection, account, observed=observed, day=day, config=config,
                            planning=planning, update_stage=False)
                    scored += 1
                fit = meets_fit(connection, account_id, config)
                _apply_fit_stage(connection, account_id, fit)
                qualified += int(fit)
            processed = job["processed"] + len(targets)
            done = processed == job["total"]
            cursor = targets[-1]["account_id"] if targets else job["last_account_id"]
            connection.execute("""
                UPDATE qualification_jobs
                SET state = ?, processed = ?, last_account_id = ?, scored = scored + ?,
                    qualified = qualified + ?, suppressed = suppressed + ?, finished_at = ?
                WHERE id = ?
            """, ("done" if done else "running", processed, cursor, scored, qualified, suppressed,
                  timestamp(clock()) if done else None, job["id"]))
    except Exception as error:
        if job is None:
            raise
        with transaction(connection):
            connection.execute("""
                UPDATE qualification_jobs SET state = 'failed', error = ?
                WHERE id = ? AND last_account_id = ? AND state IN ('queued', 'running')
            """, (f"Qualification failed ({type(error).__name__}). Retry to resume the unfinished batch.",
                  job["id"], job["last_account_id"]))
    return job["id"]


def meets_fit(connection, account_id, config):
    result = connection.execute("SELECT * FROM qualifications WHERE account_id = ?", (account_id,)).fetchone()
    return bool(result and (result["override"] if result["override"] is not None
                            else result["score"] >= config["qualified_at"]))


def credit_usage(connection, user_id, day):
    return connection.execute("""
        SELECT COALESCE((SELECT SUM(credits) FROM credit_ledger WHERE user_id = ? AND on_day = ?), 0)
             + (SELECT COUNT(*) FROM credit_jobs WHERE actor_id = ? AND on_day = ?
                  AND state IN ('queued', 'processing', 'failed'))
    """, (user_id, day, user_id, day)).fetchone()[0]


def auto_usage(connection, day):
    return connection.execute("""
        SELECT COUNT(*) FROM credit_jobs WHERE auto = 1 AND budget_day = ? AND state != 'blocked'
    """, (day,)).fetchone()[0]


def queue_job(connection, account, person_id, user, *, observed, auto=False):
    account_id = account["id"]
    if is_blocked(connection, account_id, person_id):
        raise ValueError("Outreach restrictions block enrichment for this record.")
    config = rules(connection)
    if not meets_fit(connection, account_id, config):
        raise ValueError("Qualify the account before using enrichment credits.")
    if person_id:
        person = connection.execute("SELECT * FROM people WHERE id = ? AND account_id = ?", (person_id, account_id)).fetchone()
        if not person or person["origin"] != "apollo_offline":
            raise ValueError("Choose a contact returned by the enrichment provider.")
        if person["unlocked"]:
            return False
    action = "email_unlock" if person_id else "people_search"
    existing = connection.execute("""
        SELECT 1 FROM credit_jobs WHERE account_id = ? AND action = ?
          AND (person_id = ? OR (person_id IS NULL AND CAST(? AS INTEGER) IS NULL))
          AND state IN ('queued', 'processing', 'failed', 'done')
    """, (account_id, action, person_id, person_id)).fetchone()
    if existing:
        return False
    day = local_date(user, observed).isoformat()
    budget_day = timestamp(observed)[:10]
    if user["role"] == "sdr":
        quota = connection.execute("SELECT daily_credit_quota FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
        if credit_usage(connection, user["id"], day) >= quota:
            raise ValueError("Daily credit quota reached. Your quota resets at local midnight.")
    if auto:
        if auto_usage(connection, budget_day) >= config["daily_auto_budget"]:
            raise ValueError("Daily automatic enrichment budget reached.")
    connection.execute("""
        INSERT INTO credit_jobs(account_id, person_id, actor_id, action, state,
                                on_day, budget_day, request_key, auto, created_at)
        VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
    """, (account_id, person_id, user["id"], action, day, budget_day, uuid4().hex, int(auto), timestamp(observed)))
    return True


class OfflineApollo:
    def __call__(self, job, idempotency_key):
        if job["action"] == "email_unlock":
            return {"email": f"apollo-{job['person_id']}@example.test"}
        return {"name": "Apollo Demo Contact", "title": job["title"]}


def claimable_job(connection, observed):
    config = rules(connection)
    at = timestamp(observed)
    rows = connection.execute("""
        SELECT j.*, a.organization_type, u.role, u.timezone, u.active, u.daily_credit_quota
        FROM credit_jobs j JOIN accounts a ON a.id = j.account_id JOIN users u ON u.id = j.actor_id
        WHERE j.state = 'queued' OR (j.state = 'processing' AND j.lease_until <= ?)
        ORDER BY j.id
    """, (at,))
    for row in rows:
        job = dict(row)
        if job["attempts"] == 0:
            actor_day = local_date(job, observed).isoformat()
            budget_day = at[:10]
            if (job["role"] == "sdr" and actor_day != job["on_day"]
                    and credit_usage(connection, job["actor_id"], actor_day) >= job["daily_credit_quota"]):
                continue
            if (job["auto"] and budget_day != job["budget_day"]
                    and auto_usage(connection, budget_day) >= config["daily_auto_budget"]):
                continue
            job.update(on_day=actor_day, budget_day=budget_day)
        return job
    return None


def enrich_one(connection, provider, *, clock=utcnow):
    with transaction(connection):
        observed = clock()
        job = claimable_job(connection, observed)
        if not job:
            return None
        account = account_rows(connection, observed.date().isoformat(), job["account_id"])
        actor = {"id": job["actor_id"], "role": job["role"]}
        lost_access = not job["active"] or job["role"] == "ae" or (
            job["role"] == "sdr"
            and account_relation(connection, account, actor, observed) not in {"Owned", "Coverage"}
        )
        if lost_access or is_blocked(connection, job["account_id"], job["person_id"]):
            connection.execute("UPDATE credit_jobs SET state = 'blocked', error = ? WHERE id = ?",
                               ("Authorization or suppression changed before delivery.", job["id"]))
            return job["id"]
        titles = rules(connection)["persona_titles"]
        job["title"] = titles.get(job["organization_type"].casefold(), titles["other"])
        token = uuid4().hex
        connection.execute("""
            UPDATE credit_jobs SET state = 'processing', lease_token = ?, lease_until = ?,
                on_day = ?, budget_day = ?, attempts = attempts + 1
            WHERE id = ?
        """, (token, timestamp(observed + timedelta(minutes=2)), job["on_day"], job["budget_day"], job["id"]))
    try:
        result = provider(job, idempotency_key=job["request_key"])
        with transaction(connection):
            claimed = connection.execute(
                "SELECT 1 FROM credit_jobs WHERE id = ? AND state = 'processing' AND lease_token = ?",
                (job["id"], token),
            ).fetchone()
            if claimed is None:
                return job["id"]
            connection.execute("""
                INSERT INTO credit_ledger(job_id, user_id, account_id, person_id, action, credits, on_day, at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """, (job["id"], job["actor_id"], job["account_id"], job["person_id"], job["action"],
                  job["on_day"], timestamp(clock())))
            if job["person_id"]:
                connection.execute("UPDATE people SET email = ?, unlocked = 1, email_verified = 1 WHERE id = ?",
                                   (result["email"], job["person_id"]))
            else:
                connection.execute("""
                    INSERT INTO people(account_id, name, title, persona, origin)
                    VALUES (?, ?, ?, 'P1', 'apollo_offline')
                """, (job["account_id"], result["name"], result["title"]))
            connection.execute("UPDATE credit_jobs SET state = 'done', error = NULL WHERE id = ?", (job["id"],))
    except Exception as error:
        with transaction(connection):
            connection.execute("""
                UPDATE credit_jobs SET state = 'failed', error = ?
                WHERE id = ? AND state = 'processing' AND lease_token = ?
            """, (f"Provider result pending reconciliation ({type(error).__name__}).", job["id"], token))
    return job["id"]


def account_context(connection, account_id, user):
    qualification = connection.execute("SELECT * FROM qualifications WHERE account_id = ?", (account_id,)).fetchone()
    remaining = None
    if user["role"] == "sdr":
        quota = connection.execute("SELECT daily_credit_quota FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
        remaining = max(0, quota - credit_usage(connection, user["id"], local_date(user, now()).isoformat()))
    return {
        "qualification": dict(qualification, signals=json.loads(qualification["signals"])) if qualification else None,
        "jobs": connection.execute("SELECT * FROM credit_jobs WHERE account_id = ? ORDER BY id DESC LIMIT 10",
                                   (account_id,)).fetchall(),
        "can_approve": user["role"] in {"admin", "manager"},
        "can_enrich": meets_fit(connection, account_id, rules(connection)),
        "auto_at": rules(connection)["auto_at"],
        "remaining": remaining,
    }


@bp.get("/operations")
def index():
    viewer = staff_required()
    workload = []
    observed = now()
    for user in db().execute("""
        SELECT u.id, u.name, u.timezone, u.daily_credit_quota,
               (SELECT COUNT(*) FROM account_work WHERE owner_id = u.id) AS accounts
        FROM users u WHERE u.role = 'sdr' AND u.active = 1 ORDER BY u.name
    """):
        day = local_date(user, observed).isoformat()
        counts = db().execute("""
            SELECT COUNT(*) AS open_count, COUNT(CASE WHEN due_on < ? THEN 1 END) AS overdue
            FROM tasks WHERE owner_id = ? AND status != 'done'
        """, (day, user["id"])).fetchone()
        workload.append(dict(user, **dict(counts)))
    jobs = [dict(row) for row in db().execute("""
        SELECT j.*, a.name, u.name AS actor FROM credit_jobs j JOIN accounts a ON a.id = j.account_id
        JOIN users u ON u.id = j.actor_id
    """)]
    selected = request.args.get("filter", "all")
    rows = jobs if selected == "all" else [row for row in jobs if row["state"] == selected]
    context = list_params(rows, lambda row: f"{row['name']} {row['actor']} {row['state']}", {
        "recent": lambda row: -row["id"], "account": lambda row: row["name"].casefold(),
    }, "recent")
    total = db().execute("SELECT COALESCE(SUM(credits), 0) FROM credit_ledger").fetchone()[0]
    coverage = db().execute("""
        SELECT (SELECT COUNT(*) FROM accounts) AS accounts,
               (SELECT COUNT(*) FROM account_work WHERE owner_id IS NOT NULL) AS assigned,
               (SELECT COUNT(DISTINCT account_id) FROM people
                WHERE origin NOT IN ('demo', 'apollo_offline')) AS accounts_with_contacts,
               (SELECT COUNT(DISTINCT company_id) FROM cvent_read WHERE start_date_iso >= ?) AS upcoming
    """, (local_date(viewer, observed).isoformat(),)).fetchone()
    qualification_jobs = db().execute("""
        SELECT j.*, u.name AS requested_by_name FROM qualification_jobs j
        JOIN users u ON u.id = j.requested_by ORDER BY j.id DESC LIMIT 10
    """).fetchall()
    return render_template("operations.html", **context, selected=selected, workload=workload,
                           credits=total, coverage=coverage, config=rules(db()), funnel=source_funnel(db()),
                           qualification_jobs=qualification_jobs, qualification_scopes=QUALIFICATION_SCOPES,
                           qualification_active=bool(_active_qualification_job(db())))


@bp.post("/operations/qualification-jobs")
def queue_qualification():
    user = staff_required()
    scope = request.form.get("scope", "all")
    if scope not in QUALIFICATION_SCOPES:
        abort(400, "Choose a valid qualification source.")
    planning = (current_app.config["PLANNING_MONTHS_MIN"], current_app.config["PLANNING_MONTHS_MAX"])
    with transaction(db()):
        job_id = _queue_qualification_job(db(), user, scope, now(), planning)
    flash("Qualification queued." if job_id else "A qualification job is already active.",
          "success" if job_id else "error")
    return redirect(url_for("growth_ops.index"), code=303)


@bp.post("/operations/qualification-jobs/<int:job_id>/retry")
def retry_qualification(job_id):
    user = staff_required()
    with transaction(db()):
        row = db().execute("SELECT state FROM qualification_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row or row["state"] != "failed":
            abort(409, "Only a failed qualification job can be retried.")
        if _active_qualification_job(db()):
            abort(409, "Wait for the active qualification job to finish before retrying.")
        db().execute("UPDATE qualification_jobs SET state = 'queued', error = NULL WHERE id = ?", (job_id,))
        audit(db(), user["id"], None, "qualification_job_retried", {"job_id": job_id}, now())
    return redirect(url_for("growth_ops.index"), code=303)


def source_funnel(connection):
    return connection.execute("""
        WITH evidence AS (
            SELECT source AS source_name, account_id FROM source_records WHERE account_id IS NOT NULL
            UNION
            SELECT source_slug AS source_name, account_id FROM imported_source_evidence
        )
        SELECT source_name, COUNT(*) AS discovered,
               COUNT(CASE WHEN EXISTS(SELECT 1 FROM qualifications q WHERE q.account_id = e.account_id)
                          THEN 1 END) AS scored,
               COUNT(CASE WHEN EXISTS(SELECT 1 FROM account_work w
                                      WHERE w.account_id = e.account_id AND w.owner_id IS NOT NULL)
                          THEN 1 END) AS assigned,
               COUNT(CASE WHEN EXISTS(SELECT 1 FROM activities a WHERE a.account_id = e.account_id)
                          THEN 1 END) AS worked,
               COUNT(CASE WHEN EXISTS(SELECT 1 FROM handoffs h WHERE h.account_id = e.account_id)
                          THEN 1 END) AS positive,
               COUNT(CASE WHEN EXISTS(SELECT 1 FROM handoffs h
                                      WHERE h.account_id = e.account_id AND h.status = 'delivered')
                          THEN 1 END) AS delivered,
               SUM(COALESCE((SELECT SUM(credits) FROM credit_ledger l WHERE l.account_id = e.account_id), 0)) AS credits
        FROM evidence e GROUP BY source_name ORDER BY source_name
    """).fetchall()


@bp.post("/operations/source-report")
def export_source_funnel():
    user = admin()
    with transaction(db()):
        rows = source_funnel(db())
        audit(db(), user["id"], None, "source_report_export", {"rows": len(rows)}, now())
    body = io.StringIO()
    writer = csv.writer(body)
    writer.writerow(("Source", "Discovered", "Scored", "Assigned", "Worked", "Positive", "Delivered", "Credits"))
    for row in rows:
        writer.writerow(tuple(row))
    return Response(body.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=nunify-source-funnel.csv",
    })


@bp.post("/operations/users/<int:user_id>/quota")
def set_quota(user_id):
    admin_user = admin()
    quota = request.form.get("quota", type=int)
    if quota is None or not 0 <= quota <= 100_000:
        abort(400, "Enter a nonnegative quota.")
    with transaction(db()):
        user = db().execute("SELECT daily_credit_quota FROM users WHERE id = ? AND role = 'sdr'", (user_id,)).fetchone()
        if user is None:
            abort(404)
        db().execute("UPDATE users SET daily_credit_quota = ? WHERE id = ?", (quota, user_id))
        audit(db(), admin_user["id"], None, "sdr_quota_updated",
              {"user_id": user_id, "previous": user["daily_credit_quota"], "quota": quota}, now())
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
    with transaction(db()):
        account = account_required(account_id)
        config = rules(db())
        observed = now()
        score = qualify(db(), account, observed=observed, day=local_date(user, observed), config=config,
                        page=page, page_url=page_url)
        audit(db(), user["id"], account_id, "qualification", {"score": score}, now())
        if config["auto_enabled"] and score >= config["auto_at"]:
            try:
                queue_job(db(), account, None, user, observed=now(), auto=True)
            except ValueError as error:
                flash(str(error), "error")
    flash("Qualification saved.", "success")
    return redirect(url_for("account_detail", account_id=account_id), code=303)


@bp.post("/accounts/<int:account_id>/enrich")
def enrich(account_id):
    user = g.crm_user
    person_id = request.form.get("person_id", type=int)
    with transaction(db()):
        account = account_required(account_id, write=True)
        qualification = db().execute("SELECT score FROM qualifications WHERE account_id = ?", (account_id,)).fetchone()
        if user["role"] == "sdr" and not person_id and qualification and qualification["score"] < rules(db())["auto_at"]:
            abort(403, "A manager must approve enrichment for this score.")
        try:
            queued = queue_job(db(), account, person_id, user, observed=now())
        except ValueError as error:
            abort(409, str(error))
    flash("Enrichment queued." if queued else "This request is already recorded.", "success")
    return redirect(url_for("account_detail", account_id=account_id), code=303)


@bp.post("/operations/credits/<int:job_id>/retry")
def retry(job_id):
    user = staff_required()
    with transaction(db()):
        job = db().execute("SELECT * FROM credit_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job or job["state"] != "failed":
            abort(409, "Only a failed job can be retried.")
        db().execute("UPDATE credit_jobs SET state = 'queued', lease_token = NULL, lease_until = NULL WHERE id = ?",
                     (job_id,))
        audit(db(), user["id"], job["account_id"], "credit_retry", {"job_id": job_id}, now())
    return redirect(url_for("growth_ops.index"), code=303)


def admin():
    if staff_required()["role"] != "admin":
        abort(403)
    return g.crm_user


@bp.post("/operations/qualification-settings")
def edit_rules():
    user = admin()
    terms = [term.strip() for term in request.form.get("terms", "").split(",") if term.strip()]
    if not terms or len(terms) > 100 or any(len(term) > 70 for term in terms):
        abort(400, "Enter the conference terms, separated by commas.")
    try:
        domains = {}
        for line in request.form.get("platform_domains", "").splitlines():
            if line.strip():
                domain, value = (part.strip() for part in line.split("=", 1))
                if not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", domain) or not value:
                    raise ValueError
                domains[domain] = value
        budget = int(request.form.get("daily_auto_budget", "0"))
        if not 0 <= budget <= 100_000:
            raise ValueError
    except ValueError:
        abort(400, "Enter valid platform domains and a nonnegative budget.")
    with transaction(db()):
        config = dict(rules(db()), conference_terms=terms, platform_domains=domains, daily_auto_budget=budget,
                      auto_enabled=request.form.get("auto_enabled") == "1")
        db().execute("""
            INSERT INTO workflow_settings(key, value) VALUES ('qualification', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (json.dumps(config),))
        audit(db(), user["id"], None, "qualification_settings", {"budget": budget, "enabled": config["auto_enabled"]}, now())
    return redirect(url_for("growth_ops.index"), code=303)


@bp.get("/suppression")
def suppression():
    admin()
    selected = request.args.get("filter", "all")
    rows = [dict(row) for row in db().execute("SELECT * FROM suppression") if selected in {"all", row["kind"]}]
    context = list_params(rows, lambda row: f"{row['value']} {row['reason']}", {
        "newest": lambda row: -row["id"], "value": lambda row: row["value"],
    }, "newest")
    return render_template("suppression.html", **context, selected=selected)


@bp.post("/suppression")
def add_rule():
    user = admin()
    kind = submitted("kind", max_length=10)
    value = submitted("value", max_length=254)
    reason = submitted("reason", max_length=180)
    upload = request.files.get("rules")
    if upload and upload.filename:
        raw = upload.read(128_001)
        if len(raw) > 128_000:
            abort(400, "Use a suppression CSV under 128 KB.")
        try:
            rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
            entries = [(row["kind"], row["value"], row["reason"]) for row in rows]
        except (UnicodeError, KeyError, csv.Error):
            abort(400, "Use a CSV with kind, value, and reason columns.")
    else:
        entries = [(kind, value, reason)]
    with transaction(db()):
        for input_kind, input_value, input_reason in entries:
            kind = (input_kind or "").strip().lower()
            value = (input_value or "").strip().lower().rstrip(".")
            reason = (input_reason or "").strip()
            valid = (kind == "domain" and bool(re.fullmatch(r"(?:\*\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}", value)))
            valid |= kind == "email" and bool(EMAIL.fullmatch(value))
            valid |= kind == "account" and value.isdecimal() and len(value) < 18 and bool(
                db().execute("SELECT 1 FROM accounts WHERE id = ?", (value,)).fetchone())
            if not valid or not reason or len(reason) > 180:
                abort(400, "Provide a valid suppression rule and reason.")
            if kind == "account":
                value = str(int(value))
            result = db().execute("""
                INSERT OR IGNORE INTO suppression(kind, value, reason, created_by, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (kind, value, reason, user["id"], timestamp(now())))
            if result.rowcount:
                audit(db(), user["id"], int(value) if kind == "account" else None,
                      "suppression_add", {"rule_id": result.lastrowid}, now())
    flash("Suppression rules saved.", "success")
    return redirect(url_for("growth_ops.suppression"), code=303)


@bp.post("/suppression/<int:rule_id>/remove")
def remove_rule(rule_id):
    user = admin()
    with transaction(db()):
        removed = db().execute("DELETE FROM suppression WHERE id = ?", (rule_id,))
        if removed.rowcount:
            audit(db(), user["id"], None, "suppression_remove", {"rule_id": rule_id}, now())
    return redirect(url_for("growth_ops.suppression"), code=303)
