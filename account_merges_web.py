"""Staff forms for previewing and confirming account merges."""

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeSerializer

from account_merge_store import SqlAccountMergeStore
from crm_core.account_merge import apply_merge, plan_merge
from workflow_store import transaction
from workflow_web import db, now, staff_required


bp = Blueprint("account_merges", __name__)
COUNT_LABELS = {
    "account_work": "Account allocations",
    "activities": "Activities",
    "audit_events": "Audit records",
    "coverages": "Coverage records",
    "credit_jobs": "Enrichment requests",
    "credit_ledger": "Credit charges",
    "handoffs": "Handoffs",
    "imported_source_evidence": "Imported source evidence",
    "people": "Contacts",
    "qualification_job_accounts": "Qualification requests",
    "qualifications": "Qualification records",
    "source_records": "Source records",
    "source_review_decisions": "Source review decisions",
    "tasks": "Tasks",
}


def _signer():
    return URLSafeSerializer(current_app.secret_key, salt="account-merge-preview")


def _show_form(*, plan=None, selected_ids=(None, None), token=None):
    accounts = sorted(
        SqlAccountMergeStore(db()).accounts(),
        key=lambda row: (row["name"].casefold(), row["domain"], row["id"]),
    )
    return render_template("account_merges.html", choices=accounts, plan=plan, selected_ids=selected_ids,
                           token=token, count_labels=COUNT_LABELS)


@bp.get("/account-merges")
def index():
    staff_required()
    return _show_form()


@bp.post("/account-merges/preview")
def preview():
    user = staff_required()
    ids = (request.form.get("first_account_id", type=int), request.form.get("second_account_id", type=int))
    if any(account_id is None or not 0 < account_id < 2**63 for account_id in ids) or ids[0] == ids[1]:
        abort(400, "Choose two different accounts to preview the merge.")
    try:
        plan = plan_merge(SqlAccountMergeStore(db()), ids, observed=now())
    except LookupError:
        abort(404, "An account in this selection is no longer available.")
    except ValueError as error:
        abort(409, str(error))
    token = None
    if not plan["conflicts"]:
        token = _signer().dumps({"ids": ids, "fingerprint": plan["fingerprint"], "actor_id": user["id"]})
    return _show_form(plan=plan, selected_ids=ids, token=token)


@bp.post("/account-merges/confirm")
def confirm():
    user = staff_required()
    try:
        reviewed = _signer().loads(request.form.get("merge_preview", ""))
    except BadSignature:
        abort(400, "Preview the selected accounts again before confirming.")
    if reviewed["actor_id"] != user["id"]:
        abort(403)
    connection = db()
    store = SqlAccountMergeStore(connection)
    try:
        with transaction(connection):
            observed = now()
            plan = plan_merge(store, reviewed["ids"], observed=observed)
            if plan["fingerprint"] != reviewed["fingerprint"]:
                abort(409, "The accounts changed. Preview the merge again before confirming.")
            account_id = apply_merge(store, plan, actor_id=user["id"], observed=observed)
    except PermissionError:
        abort(403)
    except LookupError:
        abort(409, "An account is no longer available. Preview the merge again.")
    except ValueError as error:
        abort(409, str(error))
    flash("Accounts merged.", "success")
    return redirect(url_for("account_detail", account_id=account_id), code=303)
