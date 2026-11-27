"""Web presentation for confirmed Apollo credit reports."""

from flask import Blueprint, abort, g, jsonify, request

from credit_reporting_store import SqlCreditReportingStore
from crm_core.credit_reporting import credit_overview, daily_credits
from workflow_web import db, now


bp = Blueprint("credit_reporting", __name__)


def credit_amount(value):
    fixed = format(value, "f")
    return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed


def workspace_credits():
    viewer = g.get("crm_user")
    return credit_overview(SqlCreditReportingStore(db()), viewer) if viewer else None


@bp.get("/api/credits/daily")
def daily():
    try:
        result = daily_credits(SqlCreditReportingStore(db()), g.crm_user, request.args.get("user_id"),
                               request.args.get("date"), now())
    except PermissionError as error:
        abort(403, str(error))
    except LookupError as error:
        abort(404, str(error))
    except ValueError as error:
        abort(400, str(error))
    result["credits"] = credit_amount(result["credits"])
    return jsonify(result)
