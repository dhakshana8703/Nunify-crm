"""Stable identifiers and metadata for evidence from Cvent and the Directory."""

import re
from datetime import date
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from organization_identity import web_url


CVENT_EXPORT = "master_cvent_events.json"
CVENT_FIELDS = (
    "event_uuid", "cvent_url", "event_name", "event_type", "start_date", "end_date",
    "attendance_type", "venue_name", "city", "state_region", "state", "country",
    "organizer", "organizer_company", "client_domain", "resolved_url", "event_url",
    "created_at", "updated_at",
)
_MONTHS = {
    month: index
    for index, month in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_CVENT_DATE = re.compile(r"\b([A-Za-z]{3})\s+(\d{1,2})\b[^\r\n]*?\b(\d{4})\b")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?!\d)")


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


def cvent_event_id(value):
    try:
        return str(UUID(value))
    except ValueError:
        return ""


def cvent_url_matches_event(source_url, event_id):
    parsed = web_url(source_url)
    return bool(
        event_id and parsed and parsed.scheme == "https"
        and parsed.hostname == "web.cvent.com" and parsed.path.startswith("/event/")
        and event_id in {cvent_event_id(part) for part in parsed.path.split("/")}
    )


def directory_listing_id(source_url: str) -> str:
    params = parse_qs(urlsplit(source_url).query)
    return params.get("di", [""])[0].removeprefix("{").removesuffix("}").upper()


def directory_listing_key(source_url):
    try:
        parsed = urlsplit(source_url)
        if (parsed.scheme != "https" or parsed.hostname != "directoryofassociations.com"
                or parsed.username or parsed.password):
            return ""
        if len(parse_qs(parsed.query, keep_blank_values=True).get("di", [])) != 1:
            return ""
        listing_id = directory_listing_id(source_url)
        return listing_id if str(UUID(listing_id)).upper() == listing_id else ""
    except (ValueError, UnicodeError):
        return ""
