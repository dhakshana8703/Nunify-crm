"""Flask adapter for saved account facts and organization type edits."""

from flask import Blueprint, abort, flash, redirect, request, url_for

from account_store import SqlAccountProfile
from crm_core.account_profile import OrganizationTypeConflict, describe_profile, set_organization_type, validate_organization_type
from workflow_store import transaction
from workflow_web import account_required, db, now, staff_required


bp = Blueprint("account_profile", __name__)


def context(connection, account, listings):
    qualification, cvent = SqlAccountProfile(connection).evidence(account["id"])
    return describe_profile(listings, qualification, cvent=cvent)


@bp.post("/accounts/<int:account_id>/organization-type")
def update_type(account_id):
    actor = staff_required()
    selected = request.form.get("organization_type")
    connection = db()
    try:
        validate_organization_type(actor, selected)
        with transaction(connection):
            set_organization_type(SqlAccountProfile(connection), actor, account_required(account_id),
                                  request.form.get("previous_type"), selected, now())
    except PermissionError:
        abort(403)
    except OrganizationTypeConflict as error:
        abort(409, str(error))
    except ValueError as error:
        abort(400, str(error))
    flash("Organization type saved.", "success")
    return redirect(url_for("account_detail", account_id=account_id), code=303)
