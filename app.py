"""Read-only Flask UI for the local two-source demo."""

from flask import Flask, abort, g, jsonify, render_template, request

from crm_store import (
    DEFAULT_DB, account_summaries, connect, directory_listings, initialize, record_evidence,
)
from cvent_reads import get_all_cvent_events, get_cvent_event_by_id


def create_app(database_path=DEFAULT_DB):
    app = Flask(__name__)
    initialize(database_path)

    def db():
        if "db" not in g:
            g.db = connect(database_path, read_only=True)
            g.db.execute("BEGIN")
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    def workspace():
        accounts = account_summaries(db())
        return {
            "accounts": accounts,
            "review_count": db().execute("""
                SELECT COUNT(*) FROM source_records WHERE account_id IS NULL
            """).fetchone()[0],
        }

    def show_account(context, account):
        events = []
        listings = []
        if account:
            account_id = account["id"]
            events = get_all_cvent_events(db(), account_id=account_id, sort_by="date")["events"]
            listings = directory_listings(db(), account_id)
        return render_template(
            "account.html", **context, account=account, events=events, listings=listings,
        )

    @app.get("/")
    def home():
        context = workspace()
        accounts = context["accounts"]
        account = next((row for row in accounts if row["identity_key"] == "aarp"), None)
        return show_account(context, account or (accounts[0] if accounts else None))

    @app.get("/accounts/<int:account_id>")
    def account_detail(account_id):
        context = workspace()
        account = next((row for row in context["accounts"] if row["id"] == account_id), None)
        if account is None:
            abort(404)
        return show_account(context, account)

    @app.get("/review")
    def review():
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
            "review.html", **workspace(), items=items[offset:offset + page_size],
            search=search, host_filter=status, sort=sort, page_size=page_size,
            page=page, pages=pages, total=total,
        )

    @app.get("/api/events")
    def api_get_events():
        return jsonify(get_all_cvent_events(
            db(),
            limit=max(1, min(100, request.args.get("limit", 25, type=int))),
            offset=max(0, request.args.get("offset", 0, type=int)),
            search=request.args.get("search", ""),
            status_filter=request.args.get("status", "all"),
            sort_by=request.args.get("sort_by", "default"),
            sort_dir=request.args.get("sort_dir", "asc"),
            account_id=request.args.get("account_id", type=int),
        ))

    @app.get("/api/events/<int:event_id>")
    def api_get_event(event_id):
        event = get_cvent_event_by_id(db(), event_id)
        if event is None:
            return jsonify(error="Event not found"), 404
        event["evidence"] = record_evidence(db(), "cvent", event["source_id"])
        return jsonify(event)

    @app.get("/api/directory/<source_id>")
    def api_get_listing(source_id):
        record = record_evidence(db(), "directory", source_id.upper())
        if record is None:
            return jsonify(error="Directory listing not found"), 404
        return jsonify(record)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000)
