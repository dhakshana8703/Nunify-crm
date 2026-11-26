"""Nunify CRM web application."""

import argparse
import os
import secrets
import sqlite3
from contextlib import closing

from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException

import account_merges_web
import account_names
import account_profile
import campaigns
import growth_ops
import sales_extensions
import scraper_ingest
import source_ops
import source_review
import workflow_store
import workflow_web
from account_facts_store import initialize_account_facts
from crm_store import (
    configured_database, connect, directory_listings, has_accounts, initialize, record_evidence, unlinked_source_count,
)
from account_redirects import canonical_account_id
from cvent_reads import get_all_cvent_events, get_cvent_event_by_id
from legacy_contact_store import evidence_for_account as cvent_contact_evidence, initialize_cvent_evidence
from source_connectors import configured_saved_exports


def create_app(database_path=None, *, demo=False):
    if database_path is None:
        database_path = configured_database()
    app = Flask(__name__)
    app.config.update(
        DATABASE=database_path,
        SECRET_KEY=os.environ.get("CRM_SECRET_KEY") or secrets.token_hex(32),
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("RENDER") == "true",
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        MACHINE_ENDPOINTS=scraper_ingest.API_ENDPOINTS | {"healthz"},
        SOURCE_CONNECTORS=configured_saved_exports(),
        PLANNING_MONTHS_MIN=int(os.environ.get("CRM_PLANNING_MONTHS_MIN", "4")),
        PLANNING_MONTHS_MAX=int(os.environ.get("CRM_PLANNING_MONTHS_MAX", "9")),
    )
    initialize(database_path)
    with closing(connect(database_path)) as connection:
        account_names.initialize(connection)
        workflow_store.initialize(connection)
        initialize_cvent_evidence(connection)
        sales_extensions.initialize(connection)
        source_ops.initialize(connection)
        scraper_ingest.initialize(connection)
        growth_ops.initialize(connection)
        campaigns.initialize(connection)
        initialize_account_facts(connection)
        if demo:
            workflow_store.seed_demo_users(connection)
    app.register_blueprint(workflow_web.bp)
    app.register_blueprint(account_merges_web.bp)
    app.register_blueprint(account_names.bp)
    app.register_blueprint(account_profile.bp)
    app.register_blueprint(sales_extensions.bp)
    app.register_blueprint(source_ops.bp)
    app.register_blueprint(scraper_ingest.bp)
    app.register_blueprint(source_review.bp)
    app.register_blueprint(growth_ops.bp)
    app.register_blueprint(campaigns.bp)
    app.before_request(workflow_web.authenticate_request)
    app.jinja_env.globals.update(csrf_token=workflow_web.csrf_token, view_endpoints=app.view_functions)

    db = workflow_web.db

    @app.get("/healthz")
    def healthz():
        try:
            has_accounts(db())
        except sqlite3.Error:
            return jsonify(status="unavailable"), 503
        return jsonify(status="ready")

    @app.after_request
    def cache_policy(response):
        if request.endpoint != "static":
            response.cache_control.no_store = True
        return response

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @app.context_processor
    def workspace():
        user = g.get("crm_user")
        return {
            "current_user": user,
            "accounts": workflow_web.readable_accounts()[:8] if user and user["role"] == "sdr" else [],
            "review_count": unlinked_source_count(db()) if user and user["role"] in workflow_web.STAFF else None,
        }

    @app.errorhandler(HTTPException)
    def show_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.name, message=error.description), error.code
        return render_template("workflow_error.html", title=error.name, message=error.description), error.code

    def show_account(account):
        events = []
        listings = []
        context = {}
        if account:
            account_id = account["id"]
            events = get_all_cvent_events(db(), account_id=account_id, sort_by="date")["events"]
            listings = directory_listings(db(), account_id)
            context = workflow_web.activity_context(account)
            context["account_naming"] = account_names.account_context(db(), account_id, g.crm_user)
            context["profile"] = account_profile.context(db(), account, listings)
            contact_evidence = {}
            for item in cvent_contact_evidence(db(), account_id):
                contact_evidence.setdefault(item["person_id"], []).append(item)
            context["cvent_contact_evidence"] = contact_evidence
            account["owner"] = account["owner_name"] or account["owner"]
            context["sales"] = sales_extensions.account_context(db(), account_id, g.crm_user)
            context["growth"] = growth_ops.account_context(db(), account_id, g.crm_user)
            context["additional_evidence"] = source_ops.evidence_for_account(db(), account_id)
            context["account_sources"] = source_ops.account_sources(db(), (account_id,)).get(account_id, ())
        return render_template(
            "account.html", **context, account=account, events=events, listings=listings,
        )

    @app.get("/")
    def home():
        return redirect(url_for(workflow_web.ROLE_HOME[g.crm_user["role"]]))

    @app.get("/accounts/<int:account_id>")
    def account_detail(account_id):
        active_id = canonical_account_id(db(), account_id)
        account = workflow_web.account_required(active_id)
        if active_id != account_id:
            options = {"show_suppressed": 1} if workflow_web.show_suppressed() else {}
            return redirect(url_for("account_detail", account_id=active_id, **options))
        return show_account(account)

    @app.get("/review")
    def review():
        workflow_web.staff_required()
        search = request.args.get("search", "").strip()
        status = request.args.get("status", "all")
        sort = request.args.get("sort", "name")
        page_size = request.args.get("page_size", 25, type=int)
        if status not in {"all", "shared-domain", "no-domain"}:
            status = "all"
        if sort not in {"name", "id"}:
            sort = "name"
        if page_size not in {25, 50, 100}:
            page_size = 25

        items = directory_listings(db(), None)
        if status != "all":
            items = [item for item in items if bool(item["host"]) == (status == "shared-domain")]
        if search:
            query = search.casefold()
            items = [
                item for item in items
                if any(query in (item.get(key) or "").casefold() for key in ("name", "id", "host"))
            ]
        items.sort(key=lambda item: item[sort].casefold())
        total = len(items)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(pages, max(1, request.args.get("page", 1, type=int)))
        offset = (page - 1) * page_size
        return render_template(
            "review.html", items=items[offset:offset + page_size],
            search=search, host_filter=status, sort=sort, page_size=page_size,
            page=page, pages=pages, total=total,
        )

    @app.get("/api/events")
    def api_get_events():
        if g.crm_user["role"] == "ae":
            abort(403)
        account_id = request.args.get("account_id", type=int)
        if account_id is not None:
            account_id = canonical_account_id(db(), account_id)
            workflow_web.account_required(account_id)
        scoped_ids = None
        if g.crm_user["role"] == "sdr" and account_id is None:
            scoped_ids = [row["id"] for row in workflow_web.readable_accounts()]
        return jsonify(get_all_cvent_events(
            db(),
            limit=max(1, min(100, request.args.get("limit", 25, type=int))),
            offset=max(0, request.args.get("offset", 0, type=int)),
            search=request.args.get("search", ""),
            status_filter=request.args.get("status", "all"),
            sort_by=request.args.get("sort_by", "default"),
            sort_dir=request.args.get("sort_dir", "asc"),
            account_id=account_id, scoped_ids=scoped_ids,
        ))

    @app.get("/api/events/<int:event_id>")
    def api_get_event(event_id):
        event = get_cvent_event_by_id(db(), event_id)
        if event is None:
            return jsonify(error="Event not found"), 404
        workflow_web.account_required(event["company_id"])
        event["evidence"] = record_evidence(db(), "cvent", event["source_id"])
        return jsonify(event)

    @app.get("/api/directory/<source_id>")
    def api_get_listing(source_id):
        record = record_evidence(db(), "directory", source_id.upper())
        if record is None:
            return jsonify(error="Directory listing not found"), 404
        if record["account_id"] is None:
            workflow_web.staff_required()
        else:
            workflow_web.account_required(record["account_id"])
        return jsonify(record)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite file or PostgreSQL URL. Defaults to DATABASE_URL when set.")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--demo", action="store_true", help="Seed synthetic local users.")
    args = parser.parse_args()
    create_app(args.db, demo=args.demo).run(host="127.0.0.1", port=args.port)
