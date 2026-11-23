"""Review staged organization identities while preserving the original evidence."""

import hashlib
import json
from urllib.parse import unquote, urlsplit

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

import cvent_import
import directory_import
from workflow_store import audit, is_blocked, timestamp, transaction
from workflow_web import db, now, staff_required, submitted


bp = Blueprint("source_review", __name__)
SOURCES = {"cvent": "Cvent", "directory": "Directory of Associations"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS source_review_decisions (
    decision_key TEXT PRIMARY KEY,
    run_id INTEGER NOT NULL,
    row_number INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('held', 'excluded', 'linked')),
    reason TEXT NOT NULL,
    evidence_url TEXT NOT NULL DEFAULT '',
    account_id INTEGER REFERENCES accounts(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    decided_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (run_id, row_number) REFERENCES source_run_rows(run_id, row_number),
    CHECK ((state = 'linked') = (account_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS source_review_queue_rows ON source_run_rows(result, run_id, row_number);
"""
REVIEW_ROWS = """
WITH staged AS (
    SELECT sr.run_id, sr.row_number, sr.payload_json, sr.result, sr.detail, run.source_slug,
           COALESCE(json_extract(sr.payload_json, '$.source_id'), '') AS source_id,
           COALESCE(json_extract(sr.payload_json, '$.details.event_name'),
                    json_extract(sr.payload_json, '$.details.name'), '') AS title,
           COALESCE(json_extract(sr.payload_json, '$.details.organizer_company'), '') AS organizer,
           CASE WHEN sr.result = 'review' AND COALESCE(json_extract(sr.payload_json, '$.source_id'), '') <> ''
                     AND COALESCE(CAST(json_extract(sr.payload_json, '$.duplicate') AS TEXT), '0') IN ('0', 'false')
                     AND COALESCE(CAST(json_extract(sr.payload_json, '$.duplicate_id') AS TEXT), '0') IN ('0', 'false')
                THEN run.source_slug || ':' || json_extract(sr.payload_json, '$.source_id')
                ELSE 'row:' || sr.run_id || ':' || sr.row_number END AS decision_key
    FROM source_run_rows sr JOIN source_runs run ON run.id = sr.run_id
    WHERE run.source_slug IN ('cvent', 'directory') AND run.state IN ('ready', 'complete', 'partial')
      AND sr.result IN ('review', 'invalid')
), reviewed AS (
    SELECT staged.*,
           CASE WHEN d.state = 'linked' AND staged.payload_json <> original.payload_json THEN NULL
                ELSE d.state END AS decision,
           d.reason, d.evidence_url, d.account_id, d.decided_at,
           COALESCE(d.revision, 0) AS revision
    FROM staged LEFT JOIN source_review_decisions d USING (decision_key)
    LEFT JOIN source_run_rows original ON original.run_id = d.run_id AND original.row_number = d.row_number
)
"""


def initialize(connection):
    connection.executescript(SCHEMA)


def replay_result(connection, source, payload):
    """Keep manual exclusions and exact confirmed links through source replay."""
    source_id = payload.get("source_id")
    if (source not in SOURCES or not source_id or payload.get("error")
            or payload.get("duplicate") or payload.get("duplicate_id")):
        return None
    prior = connection.execute("""
        SELECT d.state, d.reason, d.account_id, sr.payload_json, linked.account_id AS linked_account_id
        FROM source_review_decisions d
        JOIN source_run_rows sr ON sr.run_id = d.run_id AND sr.row_number = d.row_number
        LEFT JOIN source_records linked ON linked.source = ? AND linked.source_id = ?
        WHERE d.decision_key = ? AND d.state IN ('excluded', 'linked')
    """, (source, source_id, f"{source}:{source_id}")).fetchone()
    if prior is None:
        return None
    if prior["state"] == "excluded":
        return "review", f"Excluded in source review. {prior['reason']}"
    if prior["linked_account_id"] == prior["account_id"] and json.loads(prior["payload_json"]) == payload:
        if is_blocked(connection, prior["account_id"]):
            return "suppressed", "The confirmed account matches an outreach restriction."
        return "unchanged", "Previously linked in source review."
    return None


def _get_row(connection, run_id, row_number):
    row = connection.execute(REVIEW_ROWS + """
        SELECT * FROM reviewed WHERE run_id = ? AND row_number = ?
    """, (run_id, row_number)).fetchone()
    if row is None:
        abort(404, "This row is unavailable for review.")
    return row


def _version(row):
    values = (row["run_id"], row["row_number"], row["payload_json"], row["result"], row["detail"], row["revision"])
    return hashlib.sha256(json.dumps(values).encode("utf-8")).hexdigest()


def _identity(row, payload):
    details = payload["details"]
    source = row["source_slug"]
    if source == "cvent":
        return details.get("organizer_company", ""), cvent_import._host(payload.get("domain"))
    return details.get("name", ""), directory_import._website_host(details.get("host"))


def _source_id(row, payload):
    if row["result"] == "invalid" or payload.get("error") or payload.get("duplicate") or payload.get("duplicate_id"):
        return ""
    source_url = payload.get("source_url", "")
    if row["source_slug"] == "directory":
        valid = directory_import._listing_key(source_url)
    else:
        url = cvent_import._url(source_url)
        valid = cvent_import._uuid(row["source_id"])
        if not (url and url.scheme == "https" and url.hostname == "web.cvent.com"
                and url.path.startswith("/event/") and valid in {cvent_import._uuid(part) for part in url.path.split("/")}):
            return ""
    return valid if valid == row["source_id"] else ""


def _proof_host(evidence_url):
    try:
        url = urlsplit(evidence_url)
        if url.scheme == "https" and not url.username and not url.password and url.port in (None, 443):
            if "@" not in unquote(evidence_url):
                return cvent_import._host(evidence_url)
    except ValueError:
        pass
    return ""


def _link(connection, row, payload, source_id, account, evidence_url, corrected_name):
    source = row["source_slug"]
    organizer, staged_host = _identity(row, payload)
    account_host = cvent_import._host(account["domain"])
    if not account_host or cvent_import._platform(account_host) or _proof_host(evidence_url) != account_host:
        abort(400, "Use a first-party HTTPS evidence link on the selected account's organization domain.")
    if is_blocked(connection, account["id"]):
        abort(409, "The selected account matches an outreach restriction.")
    if staged_host and staged_host != account_host:
        abort(409, "The staged organization domain differs from the selected account domain.")
    if cvent_import._names(organizer).isdisjoint(cvent_import._names(account["name"])):
        if payload.get("shared_host") or payload.get("shared_domain"):
            abort(409, "Review the original evidence before linking a differently named organization on a shared domain.")
        if not corrected_name or cvent_import._names(corrected_name).isdisjoint(cvent_import._names(account["name"])):
            abort(400, "Enter the organization name shown on the first-party evidence when correcting a mismatch.")
    elif corrected_name and cvent_import._names(corrected_name).isdisjoint(cvent_import._names(account["name"])):
        abort(400, "The corrected organization name must match the selected account.")

    prior = connection.execute("""
        SELECT account_id FROM source_records WHERE source = ? AND source_id = ?
    """, (source, source_id)).fetchone()
    if prior and prior["account_id"] is not None:
        abort(409, "There is already an account link for this source ID. The existing link is preserved.")
    details = dict(payload["details"], identity_evidence="Organization confirmed in source review",
                   identity_review_url=evidence_url)
    if source == "cvent":
        details.update(organizer_company=corrected_name or organizer, organizer_domain=account_host)
    else:
        details.update(name=corrected_name or organizer, host=account_host)
    connection.execute("""
        INSERT INTO source_records
            (source, source_id, source_url, source_file, account_id, review_reason, details_json, raw_json)
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT (source, source_id) DO UPDATE SET
            source_url = excluded.source_url, source_file = excluded.source_file,
            account_id = excluded.account_id, review_reason = NULL,
            details_json = excluded.details_json, raw_json = excluded.raw_json
        WHERE source_records.account_id IS NULL
    """, (source, source_id, payload["source_url"], payload["source_file"], account["id"],
          json.dumps(details, sort_keys=True), json.dumps(payload["raw"], sort_keys=True)))


@bp.get("/source-review")
def index():
    staff_required()
    connection = db()
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
    orders = {"newest": "run_id DESC, row_number", "oldest": "run_id, row_number",
              "name": "title COLLATE NOCASE, run_id DESC, row_number"}
    if sort not in orders:
        sort = "newest"
    filters = ["(? = '' OR source_slug = ?)", "(? = '' OR result = ?)",
               "INSTR(LOWER(title || ' ' || organizer || ' ' || source_id || ' ' || COALESCE(detail, '')), ?) > 0"]
    parameters = [source, source, result, result, search]
    if run_id is not None:
        filters.append("run_id = ?")
        parameters.append(run_id)
    else:
        filters.append("latest = 1")
    if scope == "open":
        filters.append("(decision IS NULL OR decision = 'held')")
    elif scope != "all":
        filters.append("decision = ?")
        parameters.append(scope)
    rows = REVIEW_ROWS + """
        , latest_rows AS (
            SELECT reviewed.*, ROW_NUMBER() OVER (PARTITION BY decision_key ORDER BY run_id DESC, row_number) AS latest
            FROM reviewed
        )
    """
    where = " AND ".join(filters)
    total = connection.execute(rows + f"SELECT COUNT(*) FROM latest_rows WHERE {where}", parameters).fetchone()[0]
    size = request.args.get("page_size", 25, type=int)
    size = size if size in {25, 50, 100} else 25
    pages = max(1, (total + size - 1) // size)
    page = min(pages, max(1, request.args.get("page", 1, type=int)))
    items = connection.execute(rows + f"""
        SELECT * FROM latest_rows WHERE {where} ORDER BY {orders[sort]} LIMIT ? OFFSET ?
    """, (*parameters, size, (page - 1) * size)).fetchall()
    return render_template("source_review.html", items=items, sources=SOURCES, selected=source, scope=scope,
                           result=result, search=search, sort=sort, run_id=run_id,
                           pagination=dict(size=size, page=page, pages=pages, total=total))


@bp.get("/source-review/<int:run_id>/<int:row_number>")
def detail(run_id, row_number):
    staff_required()
    connection = db()
    row = _get_row(connection, run_id, row_number)
    payload = json.loads(row["payload_json"])
    source_id = _source_id(row, payload)
    existing = connection.execute("""
        SELECT a.id, a.name FROM source_records sr JOIN accounts a ON a.id = sr.account_id
        WHERE sr.source = ? AND sr.source_id = ?
    """, (row["source_slug"], source_id)).fetchone() if source_id else None
    organizer, host = _identity(row, payload)
    search = request.args.get("account_search", "").strip()[:200]
    if search:
        accounts = connection.execute("""
            SELECT id, name, domain FROM accounts WHERE INSTR(LOWER(name || ' ' || domain), LOWER(?)) > 0
            ORDER BY name COLLATE NOCASE LIMIT 30
        """, (search,)).fetchall()
    else:
        accounts = connection.execute("""
            SELECT id, name, domain FROM accounts WHERE LOWER(domain) IN (?, ?) OR LOWER(name) = LOWER(?)
            ORDER BY name COLLATE NOCASE LIMIT 30
        """, (host, "www." + host, organizer)).fetchall()
    return render_template("source_review_detail.html", row=row, payload=payload, source_name=SOURCES[row["source_slug"]],
                           organizer=organizer, host=host, search=search, accounts=accounts, version=_version(row),
                           existing=existing, can_link=bool(source_id) and not existing)


@bp.post("/source-review/<int:run_id>/<int:row_number>")
def decide(run_id, row_number):
    user = staff_required()
    state = request.form.get("decision")
    if state not in {"held", "excluded", "linked"}:
        abort(400, "Choose a review decision.")
    reason = submitted("reason", max_length=500, required=True)
    connection = db()
    with transaction(connection):
        row = _get_row(connection, run_id, row_number)
        if row["decision"] in {"excluded", "linked"} or request.form.get("version") != _version(row):
            abort(409, "This review changed. Reload the row before recording a decision.")
        if connection.execute("""
            SELECT 1 FROM source_runs WHERE source_slug = ? AND state IN ('queued', 'running') LIMIT 1
        """, (row["source_slug"],)).fetchone():
            abort(409, "Wait for the active source run to finish before recording a decision.")
        if not row["decision_key"].startswith("row:"):
            newer = connection.execute("""
                SELECT 1 FROM source_run_rows sr JOIN source_runs r ON r.id = sr.run_id
                WHERE r.source_slug = ? AND r.state IN ('ready', 'complete', 'partial') AND r.id > ?
                  AND json_extract(sr.payload_json, '$.source_id') = ? AND sr.payload_json <> ? LIMIT 1
            """, (row["source_slug"], run_id, row["source_id"], row["payload_json"])).fetchone()
            if newer:
                abort(409, "A newer source snapshot changed this record. Review the latest run.")
        evidence_url = ""
        account_id = None
        if state == "linked":
            payload = json.loads(row["payload_json"])
            source_id = _source_id(row, payload)
            if not source_id:
                abort(409, "Correct the invalid or repeated source ID in a new source run before linking.")
            evidence_url = submitted("evidence_url", max_length=500, required=True)
            corrected_name = submitted("corrected_name", max_length=200)
            account_id = request.form.get("account_id", type=int)
            account = connection.execute("SELECT id, name, domain FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if not account:
                abort(400, "Choose an existing account to link.")
            _link(connection, row, payload, source_id, account, evidence_url, corrected_name)
        observed = now()
        connection.execute("""
            INSERT INTO source_review_decisions
                (decision_key, run_id, row_number, state, reason, evidence_url, account_id, actor_id, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (decision_key) DO UPDATE SET
                run_id = excluded.run_id, row_number = excluded.row_number, state = excluded.state,
                reason = excluded.reason, evidence_url = excluded.evidence_url, account_id = excluded.account_id,
                actor_id = excluded.actor_id, decided_at = excluded.decided_at,
                revision = source_review_decisions.revision + 1
        """, (row["decision_key"], run_id, row_number, state, reason, evidence_url, account_id, user["id"],
              timestamp(observed)))
        audit(connection, user["id"], account_id, "source_review_decision", {
            "source": row["source_slug"], "source_id": row["source_id"], "run_id": run_id,
            "row_number": row_number, "action": state, "reason": reason, "evidence_url": evidence_url,
        }, observed)
    flash({"held": "Kept in review.", "excluded": "Excluded from source promotion.",
           "linked": "Source evidence linked to the existing account."}[state], "success")
    return redirect(url_for("source_review.detail", run_id=run_id, row_number=row_number), code=303)
