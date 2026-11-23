"""Stage and selectively promote the saved Directory of Associations export."""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from cvent_import import _names as organization_name_keys
from directory_source import RAW_FIELDS, _listing_id, _location, listing_research
from workflow_store import is_blocked, matches_domain


FILE_NAME = "all_associations.json"
DEFAULT_POLICY = {
    "types": ["International", "National", "State"],
    "min_members": 1000,
    "include": [
        "professional", "trade", "medical", "medicine", "healthcare", "industry", "industrial",
        "business", "technology", "finance", "financial", "legal", "engineering", "education",
    ],
    "exclude": [
        "club", "clubs", "alumni", "fraternity", "sorority", "ymca", "ywca",
        "local", "hobby", "religion", "religious", "support group", "support groups",
    ],
}
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
MEMBERS = re.compile(r"^\s*(\d{1,3}(?:,\d{3})+|\d+)(?![\d,])")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _normalized_words(value):
    return " ".join(re.findall(r"[^\W_]+", _text(value).casefold()))


def _listing_key(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "directoryofassociations.com"
                or parsed.username or parsed.password):
            return ""
        if len(parse_qs(parsed.query, keep_blank_values=True).get("di", [])) != 1:
            return ""
        listing_id = _listing_id(url)
        return listing_id if str(UUID(listing_id)).upper() == listing_id else ""
    except (ValueError, UnicodeError):
        return ""


def _website_host(value):
    website = _text(value)
    if not website:
        return ""
    try:
        url = urlsplit(website if "://" in website else f"https://{website}")
        if url.scheme not in {"http", "https"} or url.username or url.password:
            return ""
        host = (url.hostname or "").rstrip(".").encode("idna").decode("ascii").lower().removeprefix("www.")
    except (ValueError, UnicodeError):
        return ""
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or labels[-1].isdigit():
        return ""
    return host if all(HOST_LABEL.fullmatch(label) for label in labels) else ""


def _record(row):
    if not isinstance(row, dict):
        row = {}
    raw = {
        field: row[field] for field in RAW_FIELDS
        if field in row and (row[field] is None or isinstance(row[field], (str, int, float, bool)))
    }
    url = _text(raw.get("source_url"))
    listing_id = _listing_key(url)
    location = {field: _text(raw.get(field)) for field in ("address", "city", "state", "state_code", "country")}
    city, state, country = _location(location)
    website = _text(raw.get("website"))
    name = _text(raw.get("name"))
    return {
        "source": "directory",
        "source_id": listing_id,
        "source_url": url if listing_id else "",
        "source_file": FILE_NAME,
        # Preserve the staged shape used by existing source review decisions.
        "identity_key": None,
        "reason": None,
        "error": "Use a directory listing with a valid listing ID and organization name."
        if not listing_id or not name or len(name) > 200 else "",
        "raw": raw,
        "details": {
            "id": listing_id, "name": name, "listing_type": _text(raw.get("type")),
            "category": _text(raw.get("category")), "members": raw.get("member_count"),
            "city": city, "state": state, "country": country, "address": location["address"],
            "website": website, "source_url": url if listing_id else "",
            "host": _website_host(website) or None,
            "last_updated": raw.get("last_updated"), "identity_evidence": None,
            **listing_research(raw),
        },
    }


def load_rows(directory_dir):
    """Read the local export and retain the selected original listing values."""
    with (Path(directory_dir) / FILE_NAME).open(encoding="utf-8-sig") as stream:
        data = json.load(stream)
    if not isinstance(data, list) or not data:
        raise ValueError("The saved directory export must contain a nonempty JSON list.")
    records = [_record(row) for row in data]
    ids = Counter(row["source_id"] for row in records if row["source_id"])
    hosts = Counter(row["details"]["host"] for row in records if row["details"]["host"])
    related = set()
    for host in hosts:
        labels = host.split(".")
        for index in range(1, len(labels) - 1):
            ancestor = ".".join(labels[index:])
            if ancestor in hosts:
                related.update((host, ancestor))
    for row in records:
        host = row["details"]["host"]
        row["shared_host"] = hosts[host] > 1 or host in related
        row["duplicate_id"] = ids[row["source_id"]] > 1
    return records


def _filter_reason(details, policy):
    kind = _normalized_words(details["listing_type"])
    if kind not in {_normalized_words(value) for value in policy["types"]}:
        return "Association type is outside the current promotion filter."
    member_count = MEMBERS.match(str(details["members"] or ""))
    count_digits = member_count[1].replace(",", "") if member_count else ""
    if not count_digits or len(count_digits) > 12 or int(count_digits) < policy["min_members"]:
        return "Membership is missing or below the current promotion minimum."
    category = f" {_normalized_words(details['category'])} "

    def matches(terms):
        return any(phrase and f" {phrase} " in category for phrase in map(_normalized_words, terms))

    if matches(policy["exclude"]) or (policy["include"] and not matches(policy["include"])):
        return "Directory category is outside the current promotion filter."
    return ""


def _find_account(connection, domain, name):
    exact = connection.execute("""
        SELECT id, name FROM accounts WHERE lower(domain) IN (?, ?)
    """, (domain, "www." + domain)).fetchall()
    if exact:
        if len(exact) == 1 and not organization_name_keys(exact[0]["name"]).isdisjoint(organization_name_keys(name)):
            return exact[0]["id"], ""
        return None, "The domain matches a differently named or ambiguous account."
    related = connection.execute("""
        SELECT 1 FROM accounts WHERE lower(domain) LIKE '%.' || ? OR ? LIKE '%.' || lower(domain) LIMIT 1
    """, (domain, domain)).fetchone()
    return None, "A related account domain needs identity review." if related else ""


def evaluate(connection, row, policy, *, promote=False):
    """Classify one staged listing or promote its verified organizational evidence."""
    if row.get("error"):
        return "invalid", row["error"]
    if row["duplicate_id"]:
        return "review", "The export contains the same directory listing ID more than once."
    if row.get("reason"):
        return "review", row["reason"]
    details = row["details"]
    domain = details["host"]
    existing = connection.execute("""
        SELECT account_id, review_reason, details_json FROM source_records
        WHERE source = 'directory' AND source_id = ?
    """, (row["source_id"],)).fetchone()
    account_id = None
    if existing:
        account_id = existing["account_id"]
        if account_id is None:
            return "review", existing["review_reason"]
        previous = json.loads(existing["details_json"])
        if previous.get("host") != domain or _normalized_words(previous.get("name")) != _normalized_words(details["name"]):
            return "review", "The listing changed its identity evidence. The existing link is preserved."
        for field in ("identity_evidence", "identity_review_url"):
            if field in previous:
                details = {**details, field: previous[field]}
    else:
        reason = _filter_reason(details, policy)
        if reason:
            return "staged", reason
        if not domain:
            return "review", "A valid organization website is required to promote this listing."
        if row["shared_host"]:
            return "review", "Distinct directory listings share this website or a related domain."
        account_id, conflict = _find_account(connection, domain, details["name"])
        if conflict:
            return "review", conflict

    if (account_id is not None and is_blocked(connection, account_id)) or (
        domain and any(matches_domain(domain, rule["value"]) for rule in connection.execute(
            "SELECT value FROM suppression WHERE kind = 'domain'"
        ))
    ):
        return "suppressed", "The account or organization website matches an outreach restriction."

    if not promote:
        if existing:
            return "unchanged", "Already linked. Promotion can refresh its source evidence."
        if account_id is not None:
            return "imported", "Eligible to link to the existing account."
        return "imported", "Eligible to create an account."

    if account_id is None:
        map_identity = "ingested:" + hashlib.sha256(domain.encode()).hexdigest()
        result = connection.execute("""
            INSERT INTO accounts (identity_key, name, domain, organization_type)
            VALUES (?, ?, ?, 'Association') ON CONFLICT (identity_key) DO NOTHING
        """, (map_identity, details["name"], domain))
        if not result.rowcount:
            return "review", "An existing account identity conflicts with this listing."
        account_id = result.lastrowid
    changed = connection.execute("""
        INSERT INTO source_records
          (source, source_id, source_url, source_file, account_id, review_reason, details_json, raw_json)
        VALUES ('directory', ?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT (source, source_id) DO UPDATE SET
          source_url = excluded.source_url, source_file = excluded.source_file,
          details_json = excluded.details_json, raw_json = excluded.raw_json
        WHERE source_records.source_url <> excluded.source_url
           OR source_records.source_file <> excluded.source_file
           OR source_records.details_json <> excluded.details_json
           OR source_records.raw_json <> excluded.raw_json
    """, (row["source_id"], row["source_url"], row["source_file"], account_id,
          json.dumps(details, sort_keys=True, ensure_ascii=False), json.dumps(row["raw"], sort_keys=True, ensure_ascii=False)))
    return ("imported", "Linked to the organization.") if changed.rowcount else ("unchanged", "Source record already imported.")
