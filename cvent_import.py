"""Stage the saved Cvent export and link events with corroborated organizer evidence."""

import ast
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date as calendar_date
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID

from cvent_reads import parse_date_to_iso
from workflow_store import is_blocked, matches_domain


FILE_NAME = "master_cvent_events.json"
CVENT_FIELDS = (
    "event_uuid", "cvent_url", "event_name", "event_type", "start_date", "end_date",
    "attendance_type", "venue_name", "city", "state_region", "state", "country",
    "organizer", "organizer_company", "client_domain", "resolved_url", "event_url",
    "created_at", "updated_at",
)
COMPANY_FILE = "company_p1_p2_breakdown.json"
DOMAIN_MAP_FILE = "cvent_db.py"
DEFAULT_POLICY = {}
ORGANIZER_FIELDS = ("organizer_website", "company_website", "organizer_domain", "company_domain")
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
EMAIL_ADDRESS = re.compile(r"[^\s@]+@[\w.-]+\.[a-z]{2,}", re.I)
PLATFORM_DOMAINS = (
    "cvent.com", "cventevents.com", "cvent-production.com", "eventbrite.com", "swoogo.com",
    "eventscloud.com", "eiseverywhere.com", "stova.io", "bizzabo.com", "whova.com", "zoom.us",
    "google.com", "facebook.com", "linkedin.com", "instagram.com", "youtube.com", "tinyurl.com", "bit.ly",
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
)
EVENT_SUBDOMAINS = {
    "events", "event", "eventregistration", "registration", "register", "rsvp", "conferences",
    "conference", "attend", "alumni-events", "oee-events",
}
SHARED_ROOTS = {"co.uk", "org.uk", "gov.uk", "com.au", "org.au", "gov.au", "co.nz", "ca.gov"}
PLACEHOLDER_NAMES = {
    "unknown", "n a", "none", "not specified", "event organizer", "cventevents", "events", "registration", "home",
}
MAX_EVENT_DURATION_DAYS = 14  # R1-04 date review criterion.


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _words(value):
    return " ".join(re.findall(r"[^\W_]+", _text(value).casefold()))


def _names(value):
    """Compare the full name and the verified expansion of a trailing acronym."""
    words = _words(value)
    names = {words} if words else set()
    alias = re.fullmatch(r"(.+?)\s*\(([A-Z][A-Z0-9]{1,11})\)", _text(value))
    if alias:
        base = _words(alias[1])
        initials = "".join(word[0] for word in base.split() if word not in {"of", "the", "and", "for"})
        if initials == alias[2].lower():
            names.add(base)
    return names


def _url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return parsed
    except ValueError:
        pass
    return None


def _host(value):
    value = _text(value)
    if not value:
        return ""
    parsed = _url(value if "://" in value else f"https://{value}")
    if parsed is None:
        return ""
    try:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower().removeprefix("www.")
    except UnicodeError:
        return ""
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or labels[-1].isdigit():
        return ""
    return host if all(HOST_LABEL.fullmatch(label) for label in labels) else ""


def _platform(host):
    return any(matches_domain(host, platform) for platform in PLATFORM_DOMAINS)


def _domain_family(left, right):
    return bool(left and right and (matches_domain(left, right) or matches_domain(right, left)))


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


def _uuid(value):
    try:
        return str(UUID(value))
    except ValueError:
        return ""


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
    parsed = _url(source_url)
    valid_source = (
        source_id and parsed and parsed.scheme == "https" and parsed.hostname == "web.cvent.com"
        and parsed.path.startswith("/event/") and source_id in {_uuid(part) for part in parsed.path.split("/")}
    )
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
        if row["domain"]:
            domains[row["domain"]].append(_names(row["details"]["organizer_company"]))
    for row in rows:
        row["duplicate"] = ids[row["source_id"]] > 1
        names = _names(row["details"]["organizer_company"])
        row["shared_domain"] = bool(names and any(
            other and names.isdisjoint(other) for other in domains.get(row["domain"], ())
        ))
    return rows


def _account(connection, organizer, domain):
    names = _names(organizer)
    if domain:
        candidates = connection.execute("""
            SELECT id, name, domain FROM accounts WHERE lower(domain) IN (?, ?)
               OR lower(domain) LIKE '%.' || ? OR ? LIKE '%.' || lower(domain)
        """, (domain, "www." + domain, domain, domain)).fetchall()
        matching = [row for row in candidates if not names.isdisjoint(_names(row["name"]))]
        if len(matching) == 1 and (
            _host(matching[0]["domain"]) == domain or len(candidates) == 1
        ):
            return matching[0], ""
        if candidates:
            return None, "A related account domain has a different or ambiguous organization name."
        return None, ""
    # With no new domain claim, a full exact name can corroborate an existing Directory account.
    if len(_words(organizer).split()) >= 2:
        candidates = connection.execute("""
            SELECT DISTINCT a.id, a.name, a.domain FROM accounts a
            JOIN source_records r ON r.account_id = a.id AND r.source = 'directory'
        """).fetchall()
        matching = [row for row in candidates if _words(organizer) == _words(row["name"]) and _host(row["domain"])]
        if len(matching) == 1:
            return matching[0], ""
    return None, "The organizer needs a verified website or a unique corroborating organization record."


def _organization_type(name):
    words = set(_words(name).split())
    if words & {"association", "federation", "society"}:
        return "Association"
    if words & {"university", "college"}:
        return "University"
    return "Unknown"


def evaluate(connection, row, policy, *, promote=False):
    """Preview or apply an organization-only event link without transferring existing evidence."""
    if row["error"]:
        return "invalid", row["error"]
    if row["duplicate"]:
        return "review", "The export contains this Cvent event ID more than once."
    organizer = row["details"]["organizer_company"]
    domain = row["domain"]
    existing = connection.execute("""
        SELECT r.account_id, r.details_json, a.domain FROM source_records r
        LEFT JOIN accounts a ON a.id = r.account_id WHERE r.source = 'cvent' AND r.source_id = ?
    """, (row["source_id"],)).fetchone()
    details = dict(row["details"])
    date_note = f" Event dates need review. {details['date_review']}" if "date_review" in details else ""
    if existing:
        account_id = existing["account_id"]
        previous = json.loads(existing["details_json"])
        if (account_id is None or row["reason"] or _words(organizer) != _words(previous.get("organizer_company"))
                or details["client_domain"] != previous.get("client_domain")
                or domain and not _domain_family(domain, _host(existing["domain"]))):
            return "review", "The saved organizer evidence changed. The existing event link is preserved."
        details["identity_evidence"] = previous.get("identity_evidence", "")
        organizer_domain = previous.get("organizer_domain")
        domain = _host(existing["domain"])
    else:
        if row["reason"]:
            return "review", row["reason"]
        if _words(organizer) in {"cvent", "cvent inc", "cvent inc llc", "cvent llc"}:
            return "suppressed", "The saved organizer identifies Cvent itself."
        if not organizer or _words(organizer) in PLACEHOLDER_NAMES or len(organizer) > 200:
            return "review", "The saved event has no usable organization name."
        if row["shared_domain"]:
            return "review", "This organizer website is shared by differently named organizations."
        account, reason = _account(connection, organizer, domain)
        if reason:
            return "review", reason
        if account:
            account_id = account["id"]
            domain = _host(account["domain"])
            if not details["identity_evidence"]:
                details["identity_evidence"] = "Exact organizer name corroborated by the existing directory account."
        else:
            account_id = None
        organizer_domain = domain
    if (account_id is not None and is_blocked(connection, account_id)) or any(
        matches_domain(domain, rule["value"])
        for rule in connection.execute("SELECT value FROM suppression WHERE kind = 'domain'")
    ):
        return "suppressed", "The verified organizer matches an outreach restriction."
    if _platform(domain):
        return "review", "The saved organization domain is a shared registration or communications platform."
    if not promote:
        if existing:
            return "unchanged", "Already linked. Promotion can refresh its source evidence." + date_note
        detail = "Eligible to link to the existing account." if account_id else "Eligible to create an account."
        return "imported", detail + date_note

    if account_id is None:
        identity = "ingested:" + hashlib.sha256(domain.encode()).hexdigest()
        created = connection.execute("""
            INSERT INTO accounts (identity_key, name, domain, organization_type)
            VALUES (?, ?, ?, ?) ON CONFLICT (identity_key) DO NOTHING
        """, (identity, organizer, domain, _organization_type(organizer)))
        if not created.rowcount:
            return "review", "An existing account identity conflicts with this event."
        account_id = created.lastrowid
    if organizer_domain:
        details["organizer_domain"] = organizer_domain
    changed = connection.execute("""
        INSERT INTO source_records
          (source, source_id, source_url, source_file, account_id, review_reason, details_json, raw_json)
        VALUES ('cvent', ?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT (source, source_id) DO UPDATE SET
            source_url = excluded.source_url, source_file = excluded.source_file,
            details_json = excluded.details_json, raw_json = excluded.raw_json
        WHERE source_records.source_url IS NOT excluded.source_url
           OR source_records.source_file IS NOT excluded.source_file
           OR source_records.details_json IS NOT excluded.details_json
           OR source_records.raw_json IS NOT excluded.raw_json
    """, (row["source_id"], row["source_url"], row["source_file"], account_id,
          json.dumps(details, sort_keys=True, ensure_ascii=False), json.dumps(row["raw"], sort_keys=True, ensure_ascii=False)))
    result, detail = ("imported", "Linked to the organization.") if changed.rowcount else (
        "unchanged", "Source record already imported.",
    )
    return result, detail + date_note
