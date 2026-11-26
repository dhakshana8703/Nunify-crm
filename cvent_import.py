"""Stage the saved Cvent export and link events with corroborated organizer evidence."""

import ast
import json
import re
from collections import Counter, defaultdict
from datetime import date as calendar_date
from pathlib import Path
from urllib.parse import unquote

from organization_identity import (
    domain_family as _domain_family,
    is_platform_domain as _platform,
    normalized_words as _words,
    organization_host as _host,
    organization_name_keys as _names,
    root_domain,
    text_value as _text,
    web_url as _url,
)
from source_identity import CVENT_EXPORT as FILE_NAME
from source_identity import CVENT_FIELDS, cvent_event_id as _uuid, cvent_url_matches_event, parse_date_to_iso


COMPANY_FILE = "company_p1_p2_breakdown.json"
DOMAIN_MAP_FILE = "cvent_db.py"
DEFAULT_POLICY = {}
ORGANIZER_FIELDS = ("organizer_website", "company_website", "organizer_domain", "company_domain")
EMAIL_ADDRESS = re.compile(r"[^\s@]+@[\w.-]+\.[a-z]{2,}", re.I)
EVENT_SUBDOMAINS = {
    "events", "event", "eventregistration", "registration", "register", "rsvp", "conferences",
    "conference", "attend", "alumni-events", "oee-events",
}
SHARED_ROOTS = {"co.uk", "org.uk", "gov.uk", "com.au", "org.au", "gov.au", "co.nz", "ca.gov"}
MAX_EVENT_DURATION_DAYS = 14  # R1-04 date review criterion.


def _legacy_domain(host):
    label, _, parent = host.partition(".")
    if label in EVENT_SUBDOMAINS and parent.count(".") >= 1 and parent not in SHARED_ROOTS:
        return parent
    return host


def _known_domains(directory):
    """Read the reviewed organization map without importing the legacy provider code."""
    try:
        module = ast.parse((directory / DOMAIN_MAP_FILE).read_text(encoding="utf-8-sig"))
        for assignment in module.body:
            if isinstance(assignment, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "KNOWN_DOMAIN_NAMES"
                for target in assignment.targets
            ):
                values = ast.literal_eval(assignment.value)
                return values if isinstance(values, dict) else {}
    except (OSError, SyntaxError, ValueError, UnicodeError):
        pass
    return {}


def _company_domains(directory):
    try:
        with (directory / COMPANY_FILE).open(encoding="utf-8-sig") as stream:
            companies = json.load(stream)
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(companies, list):
        return {}
    domains = defaultdict(set)
    for company in companies:
        if not isinstance(company, dict):
            continue
        host = _host(company.get("domain"))
        if host and not _platform(host):
            for name in _names(company.get("company_name")):
                domains[name].add(_legacy_domain(host))
    return domains


def _organizer_evidence(raw, organizer, registration_hosts, catalog, known):
    explicit = {_host(raw.get(field)) for field in ORGANIZER_FIELDS} - {""}
    if len(explicit) > 1:
        return "", "", "The saved organization fields specify different domains."
    if explicit:
        return next(iter(explicit)), "Saved organization website or domain", ""

    names = _names(organizer)
    catalog_domains = {domain for name in names for domain in catalog.get(name, ())}
    known_domains = {domain for name in names for domain in known.get(name, ())}
    if len(catalog_domains) > 1 or len(known_domains) > 1 or (
        catalog_domains and known_domains and catalog_domains != known_domains
    ):
        return "", "", "Organization references disagree about the organizer domain."
    if known_domains:
        return next(iter(known_domains)), f"Reviewed organization map in {DOMAIN_MAP_FILE}", ""
    if catalog_domains:
        domain = next(iter(catalog_domains))
        if any(not _platform(host) and _domain_family(domain, host) for host in registration_hosts):
            return domain, f"Organization name and domain in {COMPANY_FILE}, corroborated by the event website", ""
    return "", "", ""


def _event_start(start, end):
    start_iso = parse_date_to_iso(start)
    end_iso = parse_date_to_iso(end)
    if start and not start_iso or end and (not end_iso or not start_iso):
        return "", "The saved event dates could not be parsed."
    if end_iso:
        days = (calendar_date.fromisoformat(end_iso) - calendar_date.fromisoformat(start_iso)).days
        if days < 0 or days > MAX_EVENT_DURATION_DAYS:
            return "", "The saved event end date is out of the expected range."
    return start_iso, ""


def _record(row, catalog, known):
    if not isinstance(row, dict):
        row = {}
    raw = {}
    for field in (*CVENT_FIELDS, *ORGANIZER_FIELDS):
        value = row.get(field)
        if field in row and (value is None or isinstance(value, str) and not EMAIL_ADDRESS.search(unquote(value))):
            raw[field] = value
    source_id = _uuid(_text(raw.get("event_uuid")))
    source_url = _text(raw.get("cvent_url"))
    valid_source = cvent_url_matches_event(source_url, source_id)
    organizer = _text(raw.get("organizer")) or _text(raw.get("organizer_company"))
    event_url = _text(raw.get("resolved_url")) or _text(raw.get("event_url"))
    client_domain = _text(raw.get("client_domain")).lower()
    start_date = _text(raw.get("start_date"))
    end_date = _text(raw.get("end_date"))
    start_iso, date_issue = _event_start(start_date, end_date)
    domain, proof, reason = _organizer_evidence(raw, organizer, {_host(event_url), _host(client_domain)}, catalog, known)
    return {
        "source": "cvent", "source_id": source_id, "source_url": source_url if valid_source else "",
        "source_file": FILE_NAME, "raw": raw, "domain": domain, "proof": proof, "reason": reason,
        "error": "" if valid_source else "The Cvent event ID and original source URL need validation.",
        "details": {
            "event_name": _text(raw.get("event_name")) or "Untitled Cvent Event",
            "event_url": event_url if _url(event_url) else "",
            "start_date": start_date, "start_date_iso": start_iso, "end_date": end_date,
            "venue_name": _text(raw.get("venue_name")), "city": _text(raw.get("city")),
            "state": _text(raw.get("state_region")) or _text(raw.get("state")), "country": _text(raw.get("country")),
            "organizer_company": organizer, "client_domain": client_domain, "identity_evidence": proof,
            "evidence_keys": list(raw), "status": "has-website" if _url(event_url) else "incomplete",
            "scrape_date": _text(raw.get("updated_at")) or _text(raw.get("created_at")),
            **({"date_review": date_issue} if date_issue else {}),
        },
    }


def load_rows(directory):
    """Stage each saved event while omitting personal contact fields."""
    directory = Path(directory)
    with (directory / FILE_NAME).open(encoding="utf-8-sig") as stream:
        raw = json.load(stream)
    if not isinstance(raw, list) or not raw:
        raise ValueError("The saved Cvent export must contain a nonempty JSON list.")
    known = defaultdict(set)
    for host, name in _known_domains(directory).items():
        host = _host(host)
        if host and not _platform(host):
            for key in _names(name):
                known[key].add(_legacy_domain(host))
    catalog = _company_domains(directory)
    rows = [_record(row, catalog, known) for row in raw]
    ids = Counter(row["source_id"] for row in rows if row["source_id"])
    domains = defaultdict(list)
    for row in rows:
        if root := root_domain(row["domain"]):
            domains[root].append(_names(row["details"]["organizer_company"]))
    for row in rows:
        row["duplicate"] = ids[row["source_id"]] > 1
        names = _names(row["details"]["organizer_company"])
        row["shared_domain"] = bool(names and any(
            other and names.isdisjoint(other) for other in domains.get(root_domain(row["domain"]), ())
        ))
    return rows


def evaluate(connection, row, policy, *, promote=False):
    """Compatibility entry point for callers evaluating one saved Cvent row."""
    from crm_core.ingest import evaluate_saved
    from ingest_store import SourceStore

    return evaluate_saved(SourceStore(connection), "cvent", row, policy, promote=promote)
