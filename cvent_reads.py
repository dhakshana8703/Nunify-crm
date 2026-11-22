"""Cvent event read contract adapted from cvent-scrapper's cvent_db.py."""

import json
import re
from datetime import date


_MONTHS = {
    month: index
    for index, month in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_CVENT_DATE = re.compile(r"\b([A-Za-z]{3})\s+(\d{1,2})\b[^\r\n]*?\b(\d{4})\b")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?!\d)")
_SORT_COLUMNS = {"name": "event_name", "date": "start_date_iso", "company": "organizer_company", "city": "city"}


def parse_date_to_iso(date_str):
    """Normalize the saved date formats to a valid sortable date, when available."""
    if not date_str:
        return ""
    cvent_match = _CVENT_DATE.search(date_str)
    iso_match = _ISO_DATE.search(date_str)
    try:
        if cvent_match and cvent_match[1].lower() in _MONTHS:
            return date(int(cvent_match[3]), _MONTHS[cvent_match[1].lower()], int(cvent_match[2])).isoformat()
        if iso_match:
            return date.fromisoformat(iso_match[0]).isoformat()
    except ValueError:
        pass
    return ""


def _event_dict(row):
    event = dict(row)
    event["scraper_source"] = "Cvent"
    event["email_count"] = event["people_count"]
    event["organizer_website"] = event["company_website"] or (
        f"https://{event['company_domain']}" if event["company_domain"] else ""
    )
    event["evidence_keys"] = json.loads(event["evidence_keys"])
    return event


def _event_order(sort_by, sort_dir):
    if sort_by == "default":
        return "CASE WHEN start_date != '' AND creator_email != '' THEN 0 WHEN creator_email != '' THEN 1 ELSE 2 END, id"
    if sort_by == "id_desc":
        return "id DESC"
    column = _SORT_COLUMNS.get(sort_by)
    if column is None:
        return "id"
    direction = "DESC" if sort_dir == "desc" else "ASC"
    value_order = f"{column} {direction}, id"
    if sort_by in ("date", "company", "city"):
        return f"CASE WHEN COALESCE({column}, '') = '' THEN 1 ELSE 0 END, {value_order}"
    return value_order


def get_all_cvent_events(
    connection, limit=50, offset=0, search="", status_filter="all", sort_by="default", sort_dir="asc", account_id=None,
    scoped_ids=None,
):
    """Fetch a page of legacy Cvent event records from the local CRM projection."""
    conditions = []
    params = {"limit": limit, "offset": offset}
    if search:
        conditions.append("""(
            LOWER(event_name) LIKE :search OR LOWER(organizer_company) LIKE :search
            OR LOWER(city) LIKE :search OR LOWER(country) LIKE :search OR LOWER(creator_email) LIKE :search
        )""")
        params["search"] = f"%{search.lower()}%"
    if status_filter and status_filter != "all":
        conditions.append("status = :status")
        params["status"] = status_filter
    if account_id is not None:
        conditions.append("company_id = :account_id")
        params["account_id"] = account_id
    if scoped_ids is not None:
        conditions.append("company_id IN (SELECT value FROM json_each(:scoped_ids))")
        params["scoped_ids"] = json.dumps(scoped_ids)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    total = connection.execute(f"SELECT COUNT(*) FROM cvent_read {where}", params).fetchone()[0]
    order_by = _event_order(sort_by, sort_dir)
    rows = connection.execute(
        f"SELECT * FROM cvent_read {where} ORDER BY {order_by} LIMIT :limit OFFSET :offset", params,
    )
    return {"events": [_event_dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def get_cvent_event_by_id(connection, event_id):
    """Fetch one CRM-backed Cvent event by its stable local row ID."""
    row = connection.execute("SELECT * FROM cvent_read WHERE id = ?", (event_id,)).fetchone()
    return _event_dict(row) if row else None
