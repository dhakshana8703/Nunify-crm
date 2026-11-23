"""Stage source inputs and run promotion in a separate, restartable worker."""

import argparse
import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from xml.parsers import expat
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

import cvent_import
import directory_import
from crm_store import configured_database, connect, initialize as initialize_crm
from source_schedule import calendar_schedule, next_calendar_run
from workflow_store import audit, is_blocked, matches_domain, timestamp, transaction, utcnow


bp = Blueprint("source_ops", __name__)
SOURCE_PARENT = Path(__file__).resolve().parent.parent
MAX_UPLOAD = 2 * 1024 * 1024
MAX_ROWS = 1000
MAX_COLUMNS = 64
LEASE_MINUTES = 5
SCHEDULED_RETRY_DELAYS = (60, 120, 240)
SAVED_EXPORTS = {"cvent": cvent_import, "directory": directory_import}
SAVED_EXPORT_BATCH_SIZE = 500
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
SLUG = re.compile(r"[a-z][a-z0-9-]{1,39}\Z")
SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_sources (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    adapter TEXT NOT NULL CHECK (adapter IN ('cvent', 'directory', 'upload', 'external')),
    schedule_days INTEGER CHECK (schedule_days BETWEEN 1 AND 365),
    next_run_at TEXT,
    key_hash TEXT
);
CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY,
    source_slug TEXT NOT NULL REFERENCES ingest_sources(slug),
    input_name TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'scheduled', 'upload', 'api')),
    state TEXT NOT NULL CHECK (state IN ('ready', 'queued', 'running', 'complete', 'partial', 'failed')),
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    retry_after TEXT,
    error TEXT,
    request_key TEXT,
    request_hash TEXT,
    UNIQUE (source_slug, request_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS source_run_one_active ON source_runs(source_slug)
    WHERE state IN ('queued', 'running');
CREATE TABLE IF NOT EXISTS source_run_rows (
    run_id INTEGER NOT NULL REFERENCES source_runs(id),
    row_number INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT 'staged'
        CHECK (result IN ('staged', 'imported', 'unchanged', 'review', 'suppressed', 'invalid')),
    detail TEXT,
    PRIMARY KEY (run_id, row_number)
);
CREATE TABLE IF NOT EXISTS imported_source_evidence (
    id INTEGER PRIMARY KEY,
    source_slug TEXT NOT NULL REFERENCES ingest_sources(slug),
    external_id TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    source_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (source_slug, external_id)
);
CREATE INDEX IF NOT EXISTS imported_source_account ON imported_source_evidence(account_id);
CREATE TABLE IF NOT EXISTS saved_export_runs (
    run_id INTEGER PRIMARY KEY REFERENCES source_runs(id),
    policy_json TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'preview' CHECK (phase IN ('preview', 'promote')),
    last_row INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS source_schedules (
    source_slug TEXT PRIMARY KEY REFERENCES ingest_sources(slug),
    cadence TEXT NOT NULL CHECK (cadence IN ('daily', 'weekly', 'monthly')),
    timezone TEXT NOT NULL,
    local_time TEXT NOT NULL,
    schedule_day INTEGER
);
CREATE TABLE IF NOT EXISTS source_promotion_policies (
    source_slug TEXT PRIMARY KEY REFERENCES ingest_sources(slug),
    policy_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_health_events (
    id INTEGER PRIMARY KEY,
    source_slug TEXT NOT NULL REFERENCES ingest_sources(slug),
    run_id INTEGER REFERENCES source_runs(id),
    kind TEXT NOT NULL CHECK (kind IN ('skipped', 'failed', 'low_yield')),
    message TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
INSERT OR IGNORE INTO ingest_sources (slug, name, adapter) VALUES
    ('cvent', 'Cvent', 'cvent'),
    ('directory', 'Directory of Associations', 'directory'),
    ('upload', 'File uploads', 'upload');
"""


def initialize(connection):
    from source_review import initialize as initialize_review

    if getattr(connection, "dialect", "sqlite") == "sqlite":
        with transaction(connection):
            if connection.execute("""
                SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'directory_import_runs'
            """).fetchone():
                connection.execute("ALTER TABLE directory_import_runs RENAME TO saved_export_runs")
    connection.executescript(SCHEMA)
    with transaction(connection):
        columns = {row[1] for row in connection.execute("PRAGMA table_info(source_runs)")}
        if "retry_count" not in columns:
            connection.execute("ALTER TABLE source_runs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
        if "retry_after" not in columns:
            connection.execute("ALTER TABLE source_runs ADD COLUMN retry_after TEXT")
    initialize_review(connection)


def _compact(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _cell(value):
    return "" if value is None else str(value).strip()


def _domain(value):
    if not value:
        return ""
    try:
        url = urlsplit(value if "://" in value else f"https://{value}")
        if url.scheme not in {"http", "https"} or url.username or url.password:
            return ""
        host = (url.hostname or "").rstrip(".").encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return ""
    host = host.lower().removeprefix("www.")
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or labels[-1].isdigit():
        return ""
    return host if all(HOST_LABEL.fullmatch(label) for label in labels) else ""


def _safe_source_url(value):
    return value if value.startswith(("https://", "http://")) and _domain(value) else ""


def _header(value):
    return re.sub(r"[^a-z0-9]+", "_", _cell(value).casefold()).strip("_")


def _file_rows(matrix):
    headers = [_header(item) for item in next(matrix, [])]
    while headers and not headers[-1]:
        headers.pop()
    if len(headers) > MAX_COLUMNS or "name" not in headers or not all(headers) or len(headers) != len(set(headers)):
        raise ValueError("Use unique column names, including name, with no more than 64 columns.")
    if not set(headers) & {"domain", "website"}:
        raise ValueError("Include a domain or website column.")

    rows = []
    for row_number, values in enumerate(matrix, 2):
        if row_number > MAX_ROWS + 1:
            raise ValueError(f"Use no more than {MAX_ROWS} data rows per file.")
        if not any(_cell(value) for value in values):
            continue
        if any(_cell(value) for value in values[len(headers):]):
            raise ValueError(f"Row {row_number} contains data beyond the headers.")
        rows.append((row_number, dict(zip(headers, values))))
    if not rows:
        raise ValueError("The file has no data rows.")
    return rows


def _uploaded_rows(upload):
    filename = Path(upload.filename).name
    content = upload.stream.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise ValueError("Use a CSV or XLSX file smaller than 2 MiB.")
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        return filename, _file_rows(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    if suffix == ".xlsx":
        data = io.BytesIO(content)
        with ZipFile(data) as zipped:
            _verify_workbook_xml(zipped)
        data.seek(0)
        workbook = load_workbook(data, read_only=True, keep_links=False)
        try:
            sheet = workbook.worksheets[0]
            if (sheet.max_column or 0) > MAX_COLUMNS or (sheet.max_row or 0) > MAX_ROWS + 1:
                raise ValueError(f"Use at most {MAX_COLUMNS} columns and {MAX_ROWS} data rows.")
            sheet.reset_dimensions()
            return filename, _file_rows(sheet.iter_rows(max_col=MAX_COLUMNS + 1, values_only=True))
        finally:
            workbook.close()
    raise ValueError("Choose a .csv or .xlsx file.")


def _verify_workbook_xml(zipped):
    def reject_doctype(*_args):
        raise ValueError("Excel uploads cannot contain a document type declaration.")

    members = zipped.infolist()
    if sum(member.file_size for member in members) > 24 * 1024 * 1024:
        raise ValueError("The workbook is too large after opening.")
    for member in members:
        if member.filename.lower().endswith((".xml", ".rels", ".vml")):
            parser = expat.ParserCreate()
            parser.StartDoctypeDeclHandler = reject_doctype
            with zipped.open(member) as contents:
                parser.ParseFile(contents)


def _new_run(connection, slug, input_name, trigger, state="queued", *, rows=(), key=None, digest=None):
    run_id = connection.execute("""
        INSERT INTO source_runs (source_slug, input_name, trigger, state, queued_at, request_key, request_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (slug, input_name, trigger, state, timestamp(), key, digest)).lastrowid
    _stage(connection, run_id, rows)
    return run_id


def _stage(connection, run_id, rows):
    connection.executemany("""
        INSERT INTO source_run_rows (run_id, row_number, payload_json) VALUES (?, ?, ?)
    """, ((run_id, row_number, _compact(data)) for row_number, data in rows))


def _active(connection, slug):
    return connection.execute("""
        SELECT id FROM source_runs WHERE source_slug = ? AND state IN ('queued', 'running')
    """, (slug,)).fetchone()


def _new_saved_export(connection, source, policy, trigger):
    run_id = _new_run(connection, source, SAVED_EXPORTS[source].FILE_NAME, trigger)
    connection.execute("INSERT INTO saved_export_runs(run_id, policy_json) VALUES (?, ?)",
                       (run_id, _compact(policy)))
    return run_id


def _saved_source_policy(connection, source):
    row = connection.execute("SELECT policy_json FROM source_promotion_policies WHERE source_slug = ?", (source,)).fetchone()
    return json.loads(row["policy_json"]) if row else SAVED_EXPORTS[source].DEFAULT_POLICY


def _domain_blocked(connection, domain):
    rows = connection.execute("SELECT value FROM suppression WHERE kind = 'domain'")
    return any(matches_domain(domain, row["value"]) for row in rows)


def _normalized_name(value):
    return " ".join(re.findall(r"[^\W_]+", value.casefold()))


def _match_account(connection, domain, name):
    matches = connection.execute("""
        SELECT id, name FROM accounts WHERE lower(domain) IN (?, ?)
    """, (domain, "www." + domain)).fetchall()
    if not matches:
        family = connection.execute("""
            SELECT 1 FROM accounts WHERE lower(domain) LIKE '%.' || ?
                OR ? LIKE '%.' || lower(domain) LIMIT 1
        """, (domain, domain)).fetchone()
        return None, "Related account domain needs identity review." if family else None
    if len(matches) == 1 and _normalized_name(matches[0]["name"]) == _normalized_name(name):
        return matches[0]["id"], None
    return None, "The domain is shared with a differently named or ambiguous account."


def _contact(connection, payload):
    name = _cell(payload.get("contact_name"))
    address = _cell(payload.get("contact_email")).casefold()
    if not (name or address):
        return None, ""
    local, _, host = address.rpartition("@")
    if (not name or len(name) > 200 or len(address) > 254 or not local
            or any(c in local for c in " @\t\r\n") or _domain(host) != host or not host):
        return None, "The contact needs a name and valid email before import."
    if _domain_blocked(connection, host) or connection.execute(
        "SELECT 1 FROM suppression WHERE kind = 'email' AND value = ?", (address,),
    ).fetchone():
        return None, "The contact matches an outreach restriction."
    title = _cell(payload.get("contact_title"))[:200]
    return (name, title, address), ""


def _project(connection, run, payload, *, preview=False):
    name = _cell(payload.get("name"))
    domain = _domain(_cell(payload.get("domain") or payload.get("website")))
    source_id = _cell(payload.get("source_id"))
    if not name or len(name) > 200 or not domain or len(source_id) > 200:
        return "invalid", "Provide an organization name and valid public website or domain."
    if _domain_blocked(connection, domain):
        return "suppressed", "Domain is on the suppression list."
    if not source_id:
        source_id = hashlib.sha256((domain + "\0" + _normalized_name(name)).encode()).hexdigest()
    evidence = connection.execute("""
        SELECT e.account_id, e.payload_json, a.domain FROM imported_source_evidence e
        JOIN accounts a ON a.id = e.account_id WHERE e.source_slug = ? AND e.external_id = ?
    """, (run["source_slug"], source_id)).fetchone()
    if evidence is not None:
        if _domain(evidence["domain"]) != domain:
            return "review", "This source ID already belongs to a different account."
        account_id = evidence["account_id"]
    else:
        account_id, review = _match_account(connection, domain, name)
        if review:
            return "review", review
    if account_id is not None and is_blocked(connection, account_id):
        return "suppressed", "Account is on the suppression list."

    raw = _compact(payload)
    if evidence is not None and evidence["payload_json"] == raw:
        return "unchanged", "Source record already imported."
    contact, contact_note = _contact(connection, payload)
    if preview:
        action = "Matches an existing account." if account_id else "Would create an account."
        return "staged", " ".join(part for part in (action, contact_note) if part)

    if account_id is None:
        identity = "ingested:" + hashlib.sha256(domain.encode()).hexdigest()
        account_id = connection.execute("""
            INSERT INTO accounts (identity_key, name, domain, organization_type)
            VALUES (?, ?, ?, ?)
        """, (identity, name, domain, _cell(payload.get("organization_type"))[:60] or "Unknown")).lastrowid
    source_url = _safe_source_url(_cell(payload.get("source_url")))
    connection.execute("""
        INSERT INTO imported_source_evidence (source_slug, external_id, account_id, source_url, observed_at, payload_json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (source_slug, external_id) DO UPDATE SET
            source_url = excluded.source_url, observed_at = excluded.observed_at, payload_json = excluded.payload_json
    """, (run["source_slug"], source_id, account_id, source_url, timestamp(), raw))
    if contact:
        connection.execute("""
            INSERT INTO people (account_id, name, title, email, origin)
            VALUES (?, ?, ?, ?, 'source') ON CONFLICT DO NOTHING
        """, (account_id, *contact))
    if contact_note:
        return "review", "Account evidence linked. " + contact_note
    return "imported", "Linked to the organization."


def account_sources(connection, account_ids=None):
    """Return the registered sources with linked evidence for each account."""
    account_filter = ""
    parameters = ()
    if account_ids is not None:
        parameters = tuple(account_ids)
        if not parameters:
            return {}
        placeholders = ", ".join("?" for _ in parameters)
        account_filter = f"WHERE evidence.account_id IN ({placeholders})"
    rows = connection.execute(f"""
        WITH linked_sources AS (
            SELECT account_id, source AS slug FROM source_records WHERE account_id IS NOT NULL
            UNION
            SELECT account_id, source_slug FROM imported_source_evidence
        )
        SELECT evidence.account_id, source.slug, source.name
        FROM linked_sources evidence
        JOIN ingest_sources source ON source.slug = evidence.slug
        {account_filter}
        ORDER BY evidence.account_id, source.name COLLATE NOCASE, source.slug
    """, parameters)
    sources = {}
    for row in rows:
        sources.setdefault(row["account_id"], []).append(row)
    return sources


def evidence_for_account(connection, account_id):
    rows = connection.execute("""
        SELECT e.*, s.name AS source_name FROM imported_source_evidence e
        JOIN ingest_sources s ON s.slug = e.source_slug WHERE e.account_id = ?
        ORDER BY e.observed_at DESC, e.id DESC
    """, (account_id,)).fetchall()
    return [dict(row, raw=json.loads(row["payload_json"])) for row in rows]


def _health_event(connection, source, run_id, kind, message, now):
    connection.execute("""
        INSERT INTO source_health_events(source_slug, run_id, kind, message, occurred_at)
        VALUES (?, ?, ?, ?, ?)
    """, (source, run_id, kind, message, timestamp(now)))


def _fail_run(connection, run, message, *, now, retryable):
    retry_count = run["retry_count"]
    if run["trigger"] == "scheduled" and retryable and retry_count < len(SCHEDULED_RETRY_DELAYS):
        retry_after = now + timedelta(seconds=SCHEDULED_RETRY_DELAYS[retry_count])
        connection.execute("""
            UPDATE source_runs SET state = 'queued', retry_count = retry_count + 1, retry_after = ?,
                lease_until = NULL, error = ? WHERE id = ?
        """, (timestamp(retry_after), message, run["id"]))
    else:
        connection.execute("""
            UPDATE source_runs SET state = 'failed', error = ?, finished_at = ?,
                lease_until = NULL, retry_after = NULL WHERE id = ?
        """, (message, timestamp(now), run["id"]))
        _health_event(connection, run["source_slug"], run["id"], "failed", message, now)


def _record_yield(connection, run):
    totals = connection.execute("""
        SELECT (SELECT COUNT(*) FROM source_run_rows WHERE run_id = r.id) AS total
        FROM source_runs r
        WHERE r.source_slug = ? AND r.input_name = ? AND r.id != ?
            AND r.state IN ('ready', 'complete', 'partial') AND r.finished_at IS NOT NULL
        ORDER BY r.finished_at DESC LIMIT 5
    """, (run["source_slug"], run["input_name"], run["id"])).fetchall()
    count = connection.execute("SELECT COUNT(*) FROM source_run_rows WHERE run_id = ?", (run["id"],)).fetchone()[0]
    if totals and count < 0.30 * (sum(row["total"] for row in totals) / len(totals)):
        _health_event(connection, run["source_slug"], run["id"], "low_yield",
                      f"The run collected {count} rows, below 30% of its recent average.", utcnow())


def _claim(connection):
    now = utcnow()
    with transaction(connection):
        expired = connection.execute("""
            SELECT * FROM source_runs WHERE state = 'running' AND lease_until <= ?
        """, (timestamp(now),)).fetchall()
        for run in expired:
            _fail_run(connection, run, "Worker stopped before finishing. Retry this run.", now=now, retryable=True)
        due = connection.execute("""
            SELECT s.*, cal.cadence, cal.timezone, cal.local_time, cal.schedule_day
            FROM ingest_sources s LEFT JOIN source_schedules cal ON cal.source_slug = s.slug
            WHERE s.next_run_at <= ? AND (s.schedule_days IS NOT NULL OR cal.cadence IS NOT NULL)
        """, (timestamp(now),)).fetchall()
        for source in due:
            adapter = SAVED_EXPORTS.get(source["slug"])
            if adapter is None:
                continue
            active = _active(connection, source["slug"])
            if active:
                _health_event(connection, source["slug"], active["id"], "skipped",
                              "Scheduled run skipped because another run is still active.", now)
            else:
                _new_saved_export(connection, source["slug"], _saved_source_policy(connection, source["slug"]), "scheduled")
            next_at = next_calendar_run(source, now) if source["cadence"] else now + timedelta(days=source["schedule_days"])
            connection.execute("UPDATE ingest_sources SET next_run_at = ? WHERE slug = ?", (timestamp(next_at), source["slug"]))
        run = connection.execute("""
            SELECT r.*, s.adapter FROM source_runs r JOIN ingest_sources s ON s.slug = r.source_slug
            WHERE r.state = 'queued' AND (r.retry_after IS NULL OR r.retry_after <= ?)
            ORDER BY r.id LIMIT 1
        """, (timestamp(now),)).fetchone()
        if run is None:
            return None
        connection.execute("""
            UPDATE source_runs SET state = 'running', started_at = ?, finished_at = NULL, attempts = attempts + 1,
                error = NULL, lease_until = ?, retry_after = NULL WHERE id = ?
        """, (timestamp(now), timestamp(now + timedelta(minutes=LEASE_MINUTES)), run["id"]))
        return dict(run, attempts=run["attempts"] + 1)


def work_one(connection, *, cvent_dir=SOURCE_PARENT / "cvent-scrapper", directory_dir=SOURCE_PARENT / "association"):
    """Claim, process and persist one run. Provider reads happen outside writer locks."""
    run = _claim(connection)
    if run is None:
        return None
    try:
        if run["adapter"] in SAVED_EXPORTS:
            full_import = _saved_export_run(connection, run["id"])
            if full_import is None:
                raise ValueError("This saved-file run predates full export staging. Start a new preview in Sources.")
            source_dir = cvent_dir if run["adapter"] == "cvent" else directory_dir
            _work_saved_export(connection, run, full_import, source_dir)
            return run["id"]
        with transaction(connection):
            if not _owns(connection, run):
                return run["id"]
            rows = connection.execute("""
                SELECT row_number, payload_json FROM source_run_rows WHERE run_id = ? ORDER BY row_number
            """, (run["id"],)).fetchall()
            if not rows:
                raise ValueError("The source did not return any rows.")
            results = set()
            for row in rows:
                payload = json.loads(row["payload_json"])
                result, detail = _project(connection, run, payload)
                connection.execute("""
                    UPDATE source_run_rows SET result = ?, detail = ? WHERE run_id = ? AND row_number = ?
                """, (result, detail, run["id"], row["row_number"]))
                results.add(result)
            state = "partial" if results & {"invalid", "review", "suppressed"} else "complete"
            connection.execute("""
                UPDATE source_runs SET state = ?, finished_at = ?, lease_until = NULL WHERE id = ?
            """, (state, timestamp(), run["id"]))
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error) as exc:
        message = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__} while reading source data."
        with transaction(connection):
            if _owns(connection, run):
                _fail_run(connection, run, message[:240], now=utcnow(),
                          retryable=isinstance(exc, (OSError, RuntimeError, sqlite3.Error)))
    return run["id"]


def _saved_export_run(connection, run_id):
    return connection.execute("SELECT * FROM saved_export_runs WHERE run_id = ?", (run_id,)).fetchone()


def _work_saved_export(connection, run, config, source_dir):
    from source_review import replay_result

    run_id = run["id"]
    adapter = SAVED_EXPORTS[run["adapter"]]
    promote = config["phase"] == "promote"
    policy = json.loads(config["policy_json"])
    has_staged = connection.execute(
        "SELECT 1 FROM source_run_rows WHERE run_id = ? LIMIT 1", (run_id,),
    ).fetchone()
    if not has_staged:
        if promote:
            raise ValueError("The preview has no staged rows. Start a new preview.")
        records = adapter.load_rows(Path(source_dir))
        if not records:
            raise ValueError("The saved export did not contain any records.")
        with transaction(connection):
            if not _owns(connection, run):
                return
            _stage(connection, run_id, enumerate(records, 1))
    cursor = config["last_row"]
    while True:
        with transaction(connection):
            if not _owns(connection, run):
                return
            rows = connection.execute("""
                SELECT row_number, payload_json FROM source_run_rows WHERE run_id = ? AND row_number > ?
                ORDER BY row_number LIMIT ?
            """, (run_id, cursor, SAVED_EXPORT_BATCH_SIZE)).fetchall()
            if not rows:
                state = "ready"
                if promote:
                    needs_review = connection.execute("""
                        SELECT 1 FROM source_run_rows WHERE run_id = ?
                        AND result IN ('invalid', 'review', 'suppressed') LIMIT 1
                    """, (run_id,)).fetchone()
                    state = "partial" if needs_review else "complete"
                else:
                    _record_yield(connection, run)
                connection.execute("""
                    UPDATE source_runs SET state = ?, finished_at = ?, lease_until = NULL WHERE id = ?
                """, (state, timestamp(), run_id))
                return
            changes = []
            for row in rows:
                payload = json.loads(row["payload_json"])
                decision = replay_result(connection, run["adapter"], payload)
                result, detail = decision or adapter.evaluate(connection, payload, policy, promote=promote)
                changes.append((result, detail, run_id, row["row_number"]))
            connection.executemany("""
                UPDATE source_run_rows SET result = ?, detail = ? WHERE run_id = ? AND row_number = ?
            """, changes)
            cursor = rows[-1]["row_number"]
            connection.execute("UPDATE saved_export_runs SET last_row = ? WHERE run_id = ?", (cursor, run_id))
            connection.execute("UPDATE source_runs SET lease_until = ? WHERE id = ?",
                               (timestamp(utcnow() + timedelta(minutes=LEASE_MINUTES)), run_id))


def _owns(connection, run):
    return connection.execute("""
        SELECT 1 FROM source_runs WHERE id = ? AND state = 'running' AND attempts = ?
    """, (run["id"], run["attempts"])).fetchone() is not None


def _admin_db():
    from workflow_web import db, staff_required

    if staff_required()["role"] != "admin":
        abort(403)
    return db()


def _run_details(connection, run_id):
    run = connection.execute("""
        SELECT r.*, s.name AS source_name, s.adapter FROM source_runs r
        JOIN ingest_sources s ON s.slug = r.source_slug WHERE r.id = ?
    """, (run_id,)).fetchone()
    if run is None:
        abort(404)
    return run


@bp.get("/sources")
def index():
    from scraper_ingest import get_promotion

    connection = _admin_db()
    sources = connection.execute("""
        SELECT s.slug, s.name, s.adapter, s.schedule_days, s.next_run_at, s.key_hash IS NOT NULL AS key_active,
               cal.cadence, cal.timezone, cal.local_time, cal.schedule_day
        FROM ingest_sources s LEFT JOIN source_schedules cal ON cal.source_slug = s.slug
        ORDER BY s.name
    """).fetchall()
    search = request.args.get("search", "")[:100].strip()
    source = request.args.get("source", "")[:40]
    state = request.args.get("state", "")[:20]
    order = "ASC" if request.args.get("order") == "oldest" else "DESC"
    size = request.args.get("page_size", 25, type=int)
    size = size if size in {25, 50, 100} else 25
    clauses = ["instr(lower(s.name || ' ' || r.input_name), lower(?)) > 0"]
    params = [search]
    if source:
        clauses.append("s.slug = ?")
        params.append(source)
    if state:
        clauses.append("r.state = ?")
        params.append(state)
    query = " FROM source_runs r JOIN ingest_sources s ON s.slug = r.source_slug WHERE " + " AND ".join(clauses)
    total = connection.execute("SELECT count(*)" + query, params).fetchone()[0]
    pages = max(1, (total + size - 1) // size)
    page = min(pages, max(1, request.args.get("page", 1, type=int)))
    runs = connection.execute("SELECT r.*, s.name AS source_name" + query + f" ORDER BY r.id {order} LIMIT ? OFFSET ?",
                              (*params, size, (page - 1) * size)).fetchall()
    health_events = connection.execute("""
        SELECT e.*, s.name FROM source_health_events e JOIN ingest_sources s ON s.slug = e.source_slug
        ORDER BY e.id DESC LIMIT 10
    """).fetchall()
    promotion_modes = {source["slug"]: get_promotion(connection, source["slug"])
                       for source in sources if source["adapter"] == "external"}
    return render_template("sources.html", sources=sources, runs=runs, search=search, source_filter=source,
                           state=state, order=order, directory_defaults=_saved_source_policy(connection, "directory"),
                           health_events=health_events, promotion_modes=promotion_modes,
                           pagination={"size": size, "page": page, "pages": pages})


@bp.get("/sources/runs/<int:run_id>")
def run_detail(run_id):
    connection = _admin_db()
    run = _run_details(connection, run_id)
    full_import = _saved_export_run(connection, run_id)
    size = request.args.get("page_size", 25, type=int)
    size = size if size in {25, 50, 100} else 25
    counts = dict(connection.execute("SELECT result, count(*) FROM source_run_rows WHERE run_id = ? GROUP BY result", (run_id,)).fetchall())
    result_filter = request.args.get("result", "")
    if result_filter not in counts:
        result_filter = ""
    search = request.args.get("search", "").strip()[:100]
    order = "DESC" if request.args.get("order") == "desc" else "ASC"
    where = """
        WHERE run_id = ? AND (? = '' OR result = ?) AND (
            ? = '' OR instr(lower(COALESCE(json_extract(payload_json, '$.raw.name'),
            json_extract(payload_json, '$.raw.event_name'), json_extract(payload_json, '$.name'), '')), lower(?)) > 0
        )
    """
    params = (run_id, result_filter, result_filter, search, search)
    total = connection.execute("SELECT COUNT(*) FROM source_run_rows" + where, params).fetchone()[0]
    pages = max(1, (total + size - 1) // size)
    page = min(pages, max(1, request.args.get("page", 1, type=int)))
    rows = connection.execute("SELECT * FROM source_run_rows" + where + f" ORDER BY row_number {order} LIMIT ? OFFSET ?",
                              (*params, size, (page - 1) * size)).fetchall()
    rendered_rows = []
    for row in rows:
        raw = json.loads(row["payload_json"])
        result, detail = row["result"], row["detail"]
        if run["state"] == "ready" and not full_import:
            result, detail = _project(connection, run, raw, preview=True)
        if run["adapter"] in {"cvent", "directory"}:
            raw = raw["raw"]
        rendered_rows.append(dict(row, record=raw, result=result, detail=detail))
    return render_template("sources_run.html", run=run, rows=rendered_rows, counts=counts,
                           full_import=full_import, search=search, result_filter=result_filter, order=order,
                           directory_policy=json.loads(full_import["policy_json"]) if full_import else None,
                           pagination={"size": size, "page": page, "pages": pages})


@bp.get("/sources/runs/<int:run_id>/status")
def run_status(run_id):
    run = _run_details(_admin_db(), run_id)
    return jsonify(state=run["state"], attempts=run["attempts"])


@bp.post("/sources/upload")
def upload():
    connection = _admin_db()
    try:
        incoming = request.files.get("source_file")
        if not incoming or not incoming.filename:
            raise ValueError("Choose a CSV or XLSX file.")
        filename, rows = _uploaded_rows(incoming)
    except (BadZipFile, InvalidFileException, UnicodeError, csv.Error, ValueError, ParseError, expat.ExpatError,
            OSError, KeyError, IndexError, RuntimeError) as exc:
        flash(str(exc) if type(exc) is ValueError else "Could not read this CSV or XLSX file.", "error")
        return redirect(url_for("source_ops.index"), code=303)
    with transaction(connection):
        run_id = _new_run(connection, "upload", filename, "upload", "ready", rows=rows)
        audit(connection, g.crm_user["id"], None, "source_staged", {"run_id": run_id, "rows": len(rows)})
    return redirect(url_for("source_ops.run_detail", run_id=run_id), code=303)


@bp.post("/sources/<slug>/queue")
def queue(slug):
    connection = _admin_db()
    if slug not in SAVED_EXPORTS:
        abort(404)
    return _queue_saved_export(connection, slug, _saved_source_policy(connection, slug))


@bp.post("/sources/directory/export")
def queue_directory_export():
    connection = _admin_db()
    defaults = _saved_source_policy(connection, "directory")
    try:
        minimum = int(request.form.get("min_members", defaults["min_members"]))
        if not 1 <= minimum <= 100_000_000:
            raise ValueError
    except ValueError:
        abort(400, "Set a membership minimum between 1 and 100 million.")
    policy = dict(defaults, min_members=minimum)
    for field in ("types", "include", "exclude"):
        submitted = request.form.get(field)
        if submitted is not None:
            terms = list(dict.fromkeys(term.strip() for term in submitted.split(",") if term.strip()))
            if len(terms) > 80 or any(len(term) > 80 for term in terms):
                abort(400, "Provide up to 80 category terms of up to 80 characters.")
            policy[field] = terms
    if not policy["types"]:
        abort(400, "Provide at least one listing type.")
    return _queue_saved_export(connection, "directory", policy, save_policy=True)


def _queue_saved_export(connection, source, policy, *, save_policy=False):
    with transaction(connection):
        active = _active(connection, source)
        if active:
            flash("A source run is already queued or in progress.", "error")
            run_id = active["id"]
        else:
            if save_policy:
                connection.execute("""
                    INSERT INTO source_promotion_policies(source_slug, policy_json)
                    VALUES (?, ?) ON CONFLICT (source_slug) DO UPDATE SET policy_json = excluded.policy_json
                """, (source, _compact(policy)))
            run_id = _new_saved_export(connection, source, policy, "manual")
            audit(connection, g.crm_user["id"], None, "saved_export_queued",
                  {"run_id": run_id, "source": source, "policy": policy})
    return redirect(url_for("source_ops.run_detail", run_id=run_id), code=303)


@bp.post("/sources/runs/<int:run_id>/queue")
def queue_run(run_id):
    connection = _admin_db()
    with transaction(connection):
        run = _run_details(connection, run_id)
        if run["state"] not in {"ready", "failed"}:
            flash("This run has already been queued or processed.", "error")
        elif _active(connection, run["source_slug"]):
            flash("Another run for this source is queued or in progress.", "error")
        else:
            if run["state"] == "ready":
                connection.execute("""
                    UPDATE saved_export_runs SET phase = 'promote', last_row = 0 WHERE run_id = ?
                """, (run_id,))
            connection.execute("""
                UPDATE source_runs SET state = 'queued', error = NULL, queued_at = ?,
                    retry_count = 0, retry_after = NULL WHERE id = ?
            """, (timestamp(), run_id))
            audit(connection, g.crm_user["id"], None, "source_run_queued", {"run_id": run_id, "prior_state": run["state"]})
    return redirect(url_for("source_ops.run_detail", run_id=run_id), code=303)


@bp.post("/sources/<slug>/schedule")
def schedule(slug):
    connection = _admin_db()
    if slug not in SAVED_EXPORTS:
        abort(404)
    cadence = request.form.get("cadence")
    settings = None
    days = None
    try:
        if cadence in {None, "interval"}:
            days = request.form.get("schedule_days", type=int)
            if days is None or not 0 <= days <= 365:
                raise ValueError("Enter an interval between 1 and 365 days, or 0 to pause the schedule.")
        elif cadence != "paused":
            day_field = "weekday" if cadence == "weekly" else "month_day"
            settings = calendar_schedule(cadence, request.form.get("timezone", ""),
                                         request.form.get("local_time", ""), request.form.get(day_field, type=int))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("source_ops.index"), code=303)
    with transaction(connection):
        now = utcnow()
        due_at = None
        if settings:
            connection.execute("""
                INSERT INTO source_schedules(source_slug, cadence, timezone, local_time, schedule_day)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT (source_slug) DO UPDATE SET
                    cadence = excluded.cadence, timezone = excluded.timezone, local_time = excluded.local_time,
                    schedule_day = excluded.schedule_day
            """, (slug, settings["cadence"], settings["timezone"], settings["local_time"], settings["schedule_day"]))
            due_at = timestamp(next_calendar_run(settings, now))
        elif days:
            connection.execute("DELETE FROM source_schedules WHERE source_slug = ?", (slug,))
            due_at = timestamp(now + timedelta(days=days))
        connection.execute("""
            UPDATE ingest_sources SET schedule_days = ?, next_run_at = ? WHERE slug = ?
        """, (days or None, due_at, slug))
        audit(connection, g.crm_user["id"], None, "source_schedule_changed", {
            "source": slug, "calendar": settings, "interval_days": days or None, "next_run_at": due_at,
        })
    flash("Schedule updated.", "success")
    return redirect(url_for("source_ops.index"), code=303)


@bp.post("/sources/keys")
def create_key():
    connection = _admin_db()
    name = request.form.get("name", "").strip()
    slug = request.form.get("slug", "").strip()
    if not (name and len(name) <= 100 and SLUG.fullmatch(slug)):
        flash("Enter a name and a 2-40 character source ID using lowercase letters, numbers, and hyphens.", "error")
        return redirect(url_for("source_ops.index"), code=303)
    key = secrets.token_urlsafe(32)
    digest = hashlib.sha256(key.encode()).hexdigest()
    with transaction(connection):
        source = connection.execute("SELECT adapter FROM ingest_sources WHERE slug = ?", (slug,)).fetchone()
        if source is not None and source["adapter"] != "external":
            abort(409)
        connection.execute("""
            INSERT INTO ingest_sources (slug, name, adapter, key_hash) VALUES (?, ?, 'external', ?)
            ON CONFLICT (slug) DO UPDATE SET name = excluded.name, key_hash = excluded.key_hash
        """, (slug, name, digest))
        audit(connection, g.crm_user["id"], None, "source_key_rotated", {"source": slug})
    return render_template("sources_key.html", slug=slug, new_key=key), 200, {"Cache-Control": "no-store"}


@bp.post("/sources/<slug>/keys/revoke")
def revoke_key(slug):
    connection = _admin_db()
    with transaction(connection):
        result = connection.execute("""
            UPDATE ingest_sources SET key_hash = NULL WHERE slug = ? AND adapter = 'external' AND key_hash IS NOT NULL
        """, (slug,))
        if result.rowcount:
            audit(connection, g.crm_user["id"], None, "source_key_revoked", {"source": slug})
    flash("Source key revoked.", "success")
    return redirect(url_for("source_ops.index"), code=303)


@bp.post("/api/sources/<slug>/ingest")
def ingest(slug):
    from workflow_web import db

    connection = db()
    source = connection.execute("SELECT key_hash FROM ingest_sources WHERE slug = ? AND adapter = 'external'", (slug,)).fetchone()
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    key_hash = hashlib.sha256(token.encode()).hexdigest()
    if source is None or scheme.casefold() != "bearer" or not token or not hmac.compare_digest(source["key_hash"] or "", key_hash):
        return jsonify(error="Source key is missing or invalid."), 401
    raw = request.stream.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        return jsonify(error="The request is larger than 2 MiB."), 413
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return jsonify(error="Send a JSON object with idempotency_key and records."), 400
    rows = body.get("records") if isinstance(body, dict) else None
    key = body.get("idempotency_key") if isinstance(body, dict) else None
    if not (isinstance(key, str) and 1 <= len(key) <= 120 and isinstance(rows, list) and 1 <= len(rows) <= MAX_ROWS
            and all(isinstance(row, dict) and len(row) <= MAX_COLUMNS for row in rows)):
        return jsonify(error="Provide an idempotency_key and up to 1000 record objects."), 400
    try:
        digest = hashlib.sha256(_compact(rows).encode()).hexdigest()
    except (ValueError, RecursionError):
        return jsonify(error="Invalid record shape."), 400
    with transaction(connection):
        if not connection.execute("SELECT 1 FROM ingest_sources WHERE slug = ? AND key_hash = ?", (slug, key_hash)).fetchone():
            return jsonify(error="Source key is missing or invalid."), 401
        prior = connection.execute("""
            SELECT id, state, request_hash FROM source_runs WHERE source_slug = ? AND request_key = ?
        """, (slug, key)).fetchone()
        if prior:
            if prior["request_hash"] != digest:
                return jsonify(error="This idempotency key was used for different data."), 409
            return jsonify(run_id=prior["id"], state=prior["state"]), 200
        if _active(connection, slug):
            return jsonify(error="A run for this source is already active."), 409
        run_id = _new_run(connection, slug, "External API", "api", rows=enumerate(rows, 1), key=key, digest=digest)
    return jsonify(run_id=run_id, state="queued"), 202


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite file or PostgreSQL URL. Defaults to DATABASE_URL when set.")
    parser.add_argument("--cvent-dir", type=Path, default=SOURCE_PARENT / "cvent-scrapper")
    parser.add_argument("--directory-dir", type=Path, default=SOURCE_PARENT / "association")
    parser.add_argument("--once", action="store_true", help="Process queued and due runs, then exit.")
    args = parser.parse_args()
    from workflow_store import initialize as initialize_workflow

    database = args.db or configured_database()
    initialize_crm(database)
    with closing(connect(database)) as connection:
        initialize_workflow(connection)
        initialize(connection)
        while True:
            run_id = work_one(connection, cvent_dir=args.cvent_dir, directory_dir=args.directory_dir)
            if run_id is not None:
                print(f"Processed source run {run_id}.", flush=True)
            elif args.once:
                break
            else:
                time.sleep(2)
