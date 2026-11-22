"""Account facts backed by saved evidence and the admin type override."""

import json

from flask import Blueprint, abort, flash, redirect, request, url_for

from workflow_store import audit, transaction
from workflow_web import account_required, db, now, staff_required


bp = Blueprint("account_profile", __name__)
ORGANIZATION_TYPES = (
    "Corporate", "Association", "Agency", "Organizer", "University", "Government", "Other", "Unknown",
)


def _consistent_listing_value(listings, field):
    values = list(dict.fromkeys(
        value.strip() for listing in listings
        if isinstance(value := listing.get(field), str) and value.strip().casefold() not in {"", "not available", "n/a"}
    ))
    return values[0] if len(values) == 1 else None


def context(connection, account, listings):
    account_id = account["id"]
    qualification = connection.execute(
        "SELECT score, signals FROM qualifications WHERE account_id = ?", (account_id,),
    ).fetchone()
    cvent = connection.execute("""
        SELECT 1 FROM source_records WHERE account_id = ? AND source = 'cvent' LIMIT 1
    """, (account_id,)).fetchone()
    platform = "Unknown"
    confidence = None
    if cvent:
        platform, confidence = "Cvent", "Confirmed by event evidence"
    elif qualification:
        prefix = "Registration platform - "
        platforms = {
            label.removeprefix(prefix) for item in json.loads(qualification["signals"])
            if (label := str(item.get("label", ""))).startswith(prefix)
        }
        if len(platforms) == 1:
            platform, confidence = next(iter(platforms)), "Detected in saved website evidence"
    return {
        "platform": platform, "platform_confidence": confidence,
        "country": _consistent_listing_value(listings, "country"),
        "members": _consistent_listing_value(listings, "members"),
        "organization_types": ORGANIZATION_TYPES,
        "fit_score": qualification["score"] if qualification else None,
    }


@bp.post("/accounts/<int:account_id>/organization-type")
def update_type(account_id):
    actor = staff_required()
    if actor["role"] != "admin":
        abort(403)
    selected = request.form.get("organization_type")
    if selected not in ORGANIZATION_TYPES:
        abort(400, "Choose an organization type from the list.")
    with transaction(db()):
        account = account_required(account_id)
        previous = account["organization_type"]
        if request.form.get("previous_type") != previous:
            abort(409, "The account type changed. Reload the account before saving.")
        if previous != selected:
            db().execute("UPDATE accounts SET organization_type = ? WHERE id = ?", (selected, account_id))
            audit(db(), actor["id"], account_id, "organization_type_changed",
                  {"previous": previous, "type": selected}, now())
    flash("Organization type saved.", "success")
    return redirect(url_for("account_detail", account_id=account_id), code=303)
