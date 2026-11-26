"""Flask forms and views for account name review."""
"""Flask forms and views for account name review."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from account_store import SqlAccountNames, initialize_names as initialize
from crm_core.access import STAFF
from crm_core.account_names import AccountNameConflict, account_naming, apply_name, backfill_name_provenance, is_exact_match
from workflow_store import transaction, utcnow
from workflow_web import db, list_params, now, staff_required


bp = Blueprint("account_names", __name__)
NAME_STATES = {
    "all": "All accounts",
    "different": "Suggested name differs",
    "conflicting": "Source names conflict",
    "review_domain": "Existing name needs review",
    "matched": "Current name matches evidence",
    "missing": "No reliable name evidence",
    "approved": "Approved source name",
    "manual": "Admin-edited name",
}
STATE_ORDER = {state: index for index, state in enumerate(NAME_STATES)}


def backfill_exact_matches(connection, *, actor_id, observed=None):
    """Record evidence for unchanged, exactly matching display names."""
    with transaction(connection):
        return backfill_name_provenance(SqlAccountNames(connection), actor_id, observed or utcnow())


def account_context(connection, account_id, user):
    """The account page can include the same review form as the names list."""
    if user["role"] not in STAFF:
        return None
    store = SqlAccountNames(connection)
    account = store.account(account_id)
    if account is None:
        return None
    return account_naming(account, store.evidence(account_id).get(account_id, ()))


@bp.get("/account-names")
def index():
    staff_required()
    store = SqlAccountNames(db())
    evidence = store.evidence()
    records = [account_naming(row, evidence.get(row["id"], ())) for row in store.accounts()]
    exact_matches = sum(is_exact_match(record) for record in records)
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
    connection = db()
    try:
        with transaction(connection):
            apply_name(
                SqlAccountNames(connection), user, account_id, now(), decision=request.form.get("decision"),
                version=request.form.get("version"), candidate=request.form.get("candidate"),
                name=request.form.get("name", ""), reason=request.form.get("reason", ""),
            )
    except PermissionError:
        abort(403)
    except AccountNameConflict as error:
        abort(409, str(error))
    except LookupError:
        abort(404)
    except ValueError as error:
        abort(400, str(error))
    flash("Account name saved.", "success")
    return redirect(url_for("account_detail", account_id=account_id), code=303)
