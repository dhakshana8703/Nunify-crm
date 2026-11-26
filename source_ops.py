"""Stage source inputs and run promotion in a separate, restartable worker."""

import argparse
import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import time
from contextlib import closing
from pathlib import Path
from xml.parsers import expat
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from crm_core import source_runs
from crm_core.ingest import evaluate_intake, evaluate_saved
from crm_core.source_worker import SAVED_EXPORT_BATCH_SIZE
from crm_core.source_runs import MAX_COLUMNS, MAX_ROWS
from crm_store import configured_database, connect, initialize as initialize_crm
from ingest_store import SourceStore, account_sources, evidence_for_account
from source_connectors import CVENT_DIR, DIRECTORY_DIR, SAVED_EXPORT_MODULES, configured_saved_exports
from source_run_store import _new_run as _new_run
from source_run_store import SqlSourceRuns, _saved_source_policy, initialize
from source_worker_service import work_one as work_source_run
from source_worker_store import SourceWorkerStore
from workflow_store import transaction, utcnow


bp = Blueprint("source_ops", __name__)
MAX_UPLOAD = 2 * 1024 * 1024


def _cell(value):
    return "" if value is None else str(value).strip()


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


def work_one(connection, *, cvent_dir=CVENT_DIR, directory_dir=DIRECTORY_DIR, connectors=None):
    """Adapt configured readers and run one pending source job."""
    return work_source_run(connection, cvent_dir=cvent_dir, directory_dir=directory_dir, connectors=connectors,
                           clock=utcnow, saved_evaluator=evaluate_saved, batch_size=SAVED_EXPORT_BATCH_SIZE)


def _saved_export_run(connection, run_id):
    return SourceWorkerStore(connection).saved_run(run_id)


def _admin_db():
    from workflow_web import db

    try:
        source_runs.require_source_admin(g.crm_user)
    except PermissionError:
        abort(403)
    return db()


def _require_saved_export(slug):
    try:
        return source_runs.saved_connector(slug, SAVED_EXPORT_MODULES, current_app.config["SOURCE_CONNECTORS"])
    except LookupError:
        abort(404)
    except source_runs.SourceRunConflict as error:
        abort(409, str(error))


def _connector_available(run):
    return source_runs.connector_available(run, current_app.config["SOURCE_CONNECTORS"])


def _run_details(connection, run_id):
    run = SqlSourceRuns(connection).run(run_id)
    if run is None:
        abort(404)
    return run


def _history_page_size():
    size = request.args.get("page_size", 25, type=int)
    return size if size in {25, 50, 100} else 25


@bp.get("/sources")
def index():
    from scraper_ingest import get_promotion

    connection = _admin_db()
    store = SqlSourceRuns(connection)
    connectors = current_app.config["SOURCE_CONNECTORS"]
    sources = store.source_list()
    search = request.args.get("search", "")[:100].strip()
    source = request.args.get("source", "")[:40]
    state = request.args.get("state", "")[:20]
    oldest = request.args.get("order") == "oldest"
    runs, pagination = store.run_list(search=search, source=source, state=state, oldest=oldest,
                                      size=_history_page_size(), page=request.args.get("page", 1, type=int))
    promotion_modes = {item["slug"]: get_promotion(connection, item["slug"])
                       for item in sources if item["adapter"] == "external"}
    directory = connectors.get("directory")
    defaults = _saved_source_policy(connection, "directory", directory) if directory is not None else None
    return render_template("sources.html", sources=sources, runs=runs, search=search, source_filter=source,
                           state=state, order="ASC" if oldest else "DESC", directory_defaults=defaults,
                           saved_exports=connectors, health_events=store.health_events(), promotion_modes=promotion_modes,
                           pagination=pagination)


@bp.get("/sources/runs/<int:run_id>")
def run_detail(run_id):
    connection = _admin_db()
    store = SqlSourceRuns(connection)
    run = _run_details(connection, run_id)
    full_import = _saved_export_run(connection, run_id)
    counts = store.row_counts(run_id)
    result_filter = request.args.get("result", "")
    if result_filter not in counts:
        result_filter = ""
    search = request.args.get("search", "").strip()[:100]
    newest = request.args.get("order") == "desc"
    rows, pagination = store.rows(run_id, result=result_filter, search=search, newest=newest,
                                  size=_history_page_size(), page=request.args.get("page", 1, type=int))
    rendered_rows = []
    for row in rows:
        raw = json.loads(row["payload_json"])
        result, detail = row["result"], row["detail"]
        if run["state"] == "ready" and not full_import:
            result, detail = evaluate_intake(SourceStore(connection), run["source_slug"], raw, preview=True)
        if run["adapter"] in {"cvent", "directory"}:
            raw = raw["raw"]
        rendered_rows.append(dict(row, record=raw, result=result, detail=detail))
    return render_template("sources_run.html", run=run, rows=rendered_rows, counts=counts,
                           full_import=full_import, search=search, result_filter=result_filter,
                           order="DESC" if newest else "ASC", connector_available=_connector_available(run),
                           directory_policy=json.loads(full_import["policy_json"]) if full_import else None,
                           pagination=pagination)


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
        run_id = source_runs.stage_upload(SqlSourceRuns(connection), filename, rows, g.crm_user["id"])
    return redirect(url_for("source_ops.run_detail", run_id=run_id), code=303)


@bp.post("/sources/<slug>/queue")
def queue(slug):
    connection = _admin_db()
    connector = _require_saved_export(slug)
    return _queue_saved_export(connection, slug, connector, _saved_source_policy(connection, slug, connector))


@bp.post("/sources/directory/export")
def queue_directory_export():
    connection = _admin_db()
    connector = _require_saved_export("directory")
    defaults = _saved_source_policy(connection, "directory", connector)
    try:
        policy = source_runs.listing_policy(defaults, request.form.get("min_members", defaults["min_members"]),
                                            {field: request.form.get(field) for field in ("types", "include", "exclude")})
    except ValueError as error:
        abort(400, str(error))
    return _queue_saved_export(connection, "directory", connector, policy, save_policy=True)


def _queue_saved_export(connection, source, connector, policy, *, save_policy=False):
    with transaction(connection):
        run_id, message = source_runs.queue_saved_export(
            SqlSourceRuns(connection), source, connector.adapter.FILE_NAME, policy, g.crm_user["id"],
            save_policy=save_policy,
        )
    if message:
        flash(message, "error")
    return redirect(url_for("source_ops.run_detail", run_id=run_id), code=303)


@bp.post("/sources/runs/<int:run_id>/queue")
def queue_run(run_id):
    connection = _admin_db()
    try:
        with transaction(connection):
            run = _run_details(connection, run_id)
            message = source_runs.queue_run_again(SqlSourceRuns(connection), run, current_app.config["SOURCE_CONNECTORS"],
                                                  g.crm_user["id"], utcnow())
    except source_runs.SourceRunConflict as error:
        abort(409, str(error))
    if message:
        flash(message, "error")
    return redirect(url_for("source_ops.run_detail", run_id=run_id), code=303)


@bp.post("/sources/<slug>/schedule")
def schedule(slug):
    connection = _admin_db()
    _require_saved_export(slug)
    cadence = request.form.get("cadence")
    day_field = "weekday" if cadence == "weekly" else "month_day"
    try:
        with transaction(connection):
            source_runs.save_schedule(
                SqlSourceRuns(connection), slug, g.crm_user["id"], utcnow(), cadence=cadence,
                days=request.form.get("schedule_days", type=int), zone=request.form.get("timezone", ""),
                clock=request.form.get("local_time", ""), day=request.form.get(day_field, type=int),
            )
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("source_ops.index"), code=303)
    flash("Schedule updated.", "success")
    return redirect(url_for("source_ops.index"), code=303)


@bp.post("/sources/keys")
def create_key():
    connection = _admin_db()
    name = request.form.get("name", "").strip()
    slug = request.form.get("slug", "").strip()
    key = secrets.token_urlsafe(32)
    digest = hashlib.sha256(key.encode()).hexdigest()
    try:
        with transaction(connection):
            source_runs.register_key(SqlSourceRuns(connection), slug, name, digest, g.crm_user["id"])
    except source_runs.SourceRunConflict:
        abort(409)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("source_ops.index"), code=303)
    return render_template("sources_key.html", slug=slug, new_key=key), 200, {"Cache-Control": "no-store"}


@bp.post("/sources/<slug>/keys/revoke")
def revoke_key(slug):
    connection = _admin_db()
    with transaction(connection):
        source_runs.revoke_key(SqlSourceRuns(connection), slug, g.crm_user["id"])
    flash("Source key revoked.", "success")
    return redirect(url_for("source_ops.index"), code=303)


@bp.post("/api/sources/<slug>/ingest")
def ingest(slug):
    from workflow_web import db

    connection = db()
    store = SqlSourceRuns(connection)
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    key_hash = hashlib.sha256(token.encode()).hexdigest()
    if scheme.casefold() != "bearer" or not token or not hmac.compare_digest(store.key_digest(slug) or "", key_hash):
        return jsonify(error="Source key is missing or invalid."), 401
    raw = request.stream.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        return jsonify(error="The request is larger than 2 MiB."), 413
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return jsonify(error="Send a JSON object with idempotency_key and records."), 400
    try:
        key, rows, digest = source_runs.legacy_request(body)
        with transaction(connection):
            run_id, state, created = source_runs.submit_legacy_batch(store, slug, key_hash, key, digest, rows)
    except source_runs.SourceRunConflict as error:
        return jsonify(error=str(error)), 409
    except PermissionError as error:
        return jsonify(error=str(error)), 401
    except ValueError as error:
        return jsonify(error=str(error)), 400
    return jsonify(run_id=run_id, state=state), 202 if created else 200


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite file or PostgreSQL URL. Defaults to DATABASE_URL when set.")
    parser.add_argument("--cvent-dir", type=Path, default=CVENT_DIR)
    parser.add_argument("--directory-dir", type=Path, default=DIRECTORY_DIR)
    parser.add_argument("--once", action="store_true", help="Process queued and due runs, then exit.")
    args = parser.parse_args()
    from workflow_store import initialize as initialize_workflow

    database = args.db or configured_database()
    connectors = configured_saved_exports(cvent_dir=args.cvent_dir, directory_dir=args.directory_dir)
    initialize_crm(database)
    with closing(connect(database)) as connection:
        initialize_workflow(connection)
        initialize(connection)
        while True:
            run_id = work_one(connection, connectors=connectors)
            if run_id is not None:
                print(f"Processed source run {run_id}.", flush=True)
            elif args.once:
                break
            else:
                time.sleep(2)
