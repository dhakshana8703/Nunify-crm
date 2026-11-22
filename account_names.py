"""Review the organization names used in account pages and outreach (R1-03)."""

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from cvent_import import _host
from workflow_store import audit, transaction, utcnow
from workflow_web import db, list_params, now, staff_required, submitted


bp = Blueprint("account_names", __name__)
NAME_SOURCES = {
    "unreviewed": "Existing name",
    "organizer": "Cvent organizer",
    "directory": "Directory of Associations",
    "website": "Organization website",
    "manual": "Admin edit",
}
PRIORITY = {"organizer": 0, "directory": 1, "website": 2}
NAME_STATES = {
    "all": "All accounts",
    "different": "Suggested name differs",
    "conflicting": "Source names conflict",
    "matched": "Current name matches evidence",
    "missing": "No reliable name evidence",
    "approved": "Approved source name",
    "manual": "Admin-edited name",
}
STATE_ORDER = {state: index for index, state in enumerate(NAME_STATES)}
GENERIC_NAMES = {
    "association", "cvent", "cvent events", "event", "event organizer", "events", "home", "homepage",
    "no name", "none", "not available", "organizer", "organization", "registration", "unknown", "untitled",
}


def initialize(connection):
    """Add provenance fields to new and existing local accounts."""
    with transaction(connection):
        columns = {row[1] for row in connection.execute("PRAGMA table_info(accounts)")}
        if "name_source" not in columns:
            connection.execute("ALTER TABLE accounts ADD COLUMN name_source TEXT NOT NULL DEFAULT 'unreviewed'")
        if "name_source_ref" not in columns:
            connection.execute("ALTER TABLE accounts ADD COLUMN name_source_ref TEXT")


def _name_key(value):
    return " ".join(re.findall(r"[^\W_]+", value.casefold())) if isinstance(value, str) else ""


def _clean_name(value, *, domain="", event_name="", inferred=True):
    if not isinstance(value, str) or any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        return ""
    name = " ".join(value.split())
    key = _name_key(name)
    if (
        not 2 <= len(name) <= 200 or not any(char.isalpha() for char in name)
        or "@" in name or "://" in name or name.casefold().startswith("www.")
        or key in GENERIC_NAMES or key == _name_key(domain) or key == _name_key(event_name)
    ):
        return ""
    if inferred and " " not in name and len(name) > 15 and not name.isupper():
        return ""
    return name


def _public_url(value):
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return ""


def _fingerprint(*values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()


def _proposal(source, name, reference, url, *, domain, event_name=""):
    name = _clean_name(name, domain=domain, event_name=event_name)
    if not name:
        return None
    evidence = {"source": source, "source_label": NAME_SOURCES[source], "name": name,
                "reference": reference, "url": _public_url(url)}
    return dict(evidence, key=_fingerprint(evidence))


def _evidence_by_account(connection, account_id=None):
    proposals = defaultdict(list)
    params = () if account_id is None else (account_id,)
    linked = "" if account_id is None else "AND a.id = ?"
    for row in connection.execute(f"""
        SELECT a.id, a.domain, r.source, r.source_id, r.source_url, r.details_json
        FROM source_records r JOIN accounts a ON a.id = r.account_id
        WHERE r.review_reason IS NULL {linked} ORDER BY r.id
    """, params):
        details = json.loads(row["details_json"])
        if row["source"] == "cvent":
            source, name = "organizer", details.get("organizer_company")
        else:
            source, name = "directory", details.get("name")
        proposal = _proposal(source, name, f"{row['source']}:{row['source_id']}", row["source_url"],
                             domain=row["domain"], event_name=details.get("event_name") or "")
        if proposal:
            proposals[row["id"]].append(proposal)

    # A linked first-party website snapshot can provide a site name. An external
    # input's arbitrary name field does not establish website or Apollo provenance.
    for row in connection.execute(f"""
        SELECT a.id, a.domain, e.source_slug, e.external_id, e.source_url, e.payload_json
        FROM imported_source_evidence e JOIN accounts a ON a.id = e.account_id
        WHERE 1 = 1 {linked} ORDER BY e.id
    """, params):
        url = _public_url(row["source_url"])
        host = _host(url)
        if not host or host != _host(row["domain"]):
            continue
        payload = json.loads(row["payload_json"])
        for key in ("og:site_name", "website_title"):
            proposal = _proposal("website", payload.get(key), f"{row['source_slug']}:{row['external_id']}",
                                 url, domain=row["domain"])
            if proposal:
                proposals[row["id"]].append(proposal)
    return proposals


def _account_naming(account, evidence):
    candidates = {}
    for proposal in evidence:
        candidates.setdefault((proposal["source"], _name_key(proposal["name"])), proposal)
    candidates = sorted(candidates.values(), key=lambda item: (PRIORITY[item["source"]], item["name"].casefold()))
    preferred = candidates[0] if candidates else None
    high_priority = preferred and [item for item in candidates if item["source"] == preferred["source"]]
    if account["name_source"] == "manual":
        state = "manual"
    elif account["name_source"] != "unreviewed":
        state = "approved"
    elif not candidates:
        state = "missing"
    elif len(high_priority) > 1:
        state = "conflicting"
    elif _name_key(preferred["name"]) == _name_key(account["name"]):
        state = "matched"
    else:
        state = "different"
    return {
        "id": account["id"], "name": account["name"], "domain": account["domain"], "state": state,
        "source": account["name_source"], "source_label": NAME_SOURCES.get(account["name_source"], "Source evidence"),
        "reference": account["name_source_ref"], "candidates": candidates,
        "preferred": None if state == "conflicting" else preferred,
        "version": _fingerprint(account["name"], account["domain"], account["name_source"],
                                account["name_source_ref"], candidates),
    }


def _account_row(connection, account_id):
    return connection.execute("""
        SELECT id, name, domain, name_source, name_source_ref FROM accounts WHERE id = ?
    """, (account_id,)).fetchone()


def _is_exact(record):
    return record["state"] == "matched" and record["preferred"]["name"] == record["name"]


def backfill_exact_matches(connection, *, actor_id, observed=None):
    """Record evidence for unchanged, exactly matching display names."""
    sources = Counter()
    observed = observed or utcnow()
    with transaction(connection):
        actor = connection.execute("SELECT role, active FROM users WHERE id = ?", (actor_id,)).fetchone()
        if actor is None or actor["role"] != "admin" or not actor["active"]:
            raise PermissionError("Account name provenance requires an active admin.")
        evidence = _evidence_by_account(connection)
        accounts = connection.execute("""
            SELECT id, name, domain, name_source, name_source_ref FROM accounts WHERE name_source = 'unreviewed'
        """).fetchall()
        for account in accounts:
            record = _account_naming(account, evidence[account["id"]])
            if not _is_exact(record):
                continue
            match = record["preferred"]
            connection.execute("""
                UPDATE accounts SET name_source = ?, name_source_ref = ? WHERE id = ?
            """, (match["source"], match["reference"], account["id"]))
            sources[match["source"]] += 1
            audit(connection, actor_id, account["id"], "account_name_source_recorded", {
                "name": account["name"], "source": match["source"], "reference": match["reference"],
                "source_url": match["url"],
            }, observed)
    return {"recorded": sum(sources.values()), "sources": dict(sources)}


def account_context(connection, account_id, user):
    """The account page can include the same review form as the names list."""
    if user["role"] not in {"admin", "manager"}:
        return None
    account = _account_row(connection, account_id)
    if account is None:
        return None
    evidence = _evidence_by_account(connection, account_id)
    return _account_naming(account, evidence[account_id])


@bp.get("/account-names")
def index():
    staff_required()
    connection = db()
    evidence = _evidence_by_account(connection)
    records = [
        _account_naming(row, evidence[row["id"]]) for row in connection.execute(
            "SELECT id, name, domain, name_source, name_source_ref FROM accounts ORDER BY id"
        )
    ]
    exact_matches = sum(_is_exact(record) for record in records)
    selected = request.args.get("state", "all")
    if selected not in NAME_STATES:
        selected = "all"
    if selected != "all":
        records = [record for record in records if record["state"] == selected]
    listing = list_params(
        records,
        lambda record: " ".join([record["name"], record["domain"]] + [
            candidate["name"] for candidate in record["candidates"]
        ]),
        {
            "review": lambda record: (STATE_ORDER[record["state"]], record["name"].casefold()),
            "name": lambda record: record["name"].casefold(),
        },
        "review",
    )
    return render_template("account_names.html", **listing, states=NAME_STATES, selected=selected,
                           exact_matches=exact_matches)


@bp.post("/account-names/record-provenance")
def record_provenance():
    user = staff_required()
    if user["role"] != "admin":
        abort(403)
    try:
        result = backfill_exact_matches(db(), actor_id=user["id"], observed=now())
    except PermissionError:
        abort(403)
    count = result["recorded"]
    flash(f"Recorded name evidence for {count} account{'s' if count != 1 else ''}.", "success")
    return redirect(url_for("account_names.index"), code=303)


@bp.post("/accounts/<int:account_id>/name")
def save(account_id):
    user = staff_required()
    if user["role"] != "admin":
        abort(403)
    connection = db()
    decision = request.form.get("decision")
    with transaction(connection):
        account = _account_row(connection, account_id)
        if account is None:
            abort(404)
        current = _account_naming(account, _evidence_by_account(connection, account_id)[account_id])
        if request.form.get("version") != current["version"]:
            abort(409, "The account name or its evidence changed. Reload the account and review it again.")
        if decision == "manual":
            name = _clean_name(submitted("name", max_length=200, required=True), inferred=False)
            if not name:
                abort(400, "Enter a recognizable organization name.")
            reason = submitted("reason", max_length=500, required=True)
            updates = {"name": name, "source": "manual", "reference": None, "url": ""}
        elif decision == "approve":
            if current["source"] == "manual":
                abort(409, "This name was entered by an admin. Use the manual edit form to change it.")
            requested = request.form.get("candidate")
            updates = next((item for item in current["candidates"] if item["key"] == requested), None)
            if updates is None:
                abort(400, "Choose a name from the current source evidence.")
            reason = "Approved linked organization name evidence."
        else:
            abort(400, "Choose how to save the account name.")
        state = (updates["name"], updates["source"], updates["reference"])
        before = (account["name"], account["name_source"], account["name_source_ref"])
        if before != state:
            connection.execute("""
                UPDATE accounts SET name = ?, name_source = ?, name_source_ref = ? WHERE id = ?
            """, (*state, account_id))
            audit(connection, user["id"], account_id, "account_name_changed", {
                "before": dict(zip(("name", "source", "reference"), before)),
                "after": dict(zip(("name", "source", "reference"), state)),
                "source_url": updates["url"], "reason": reason,
            }, now())
    flash("Account name saved.", "success")
    return redirect(url_for("account_detail", account_id=account_id), code=303)
