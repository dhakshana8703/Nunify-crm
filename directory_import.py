"""Stage and selectively promote the saved Directory of Associations export."""

import json
from collections import Counter, defaultdict
from pathlib import Path

from directory_source import RAW_FIELDS, _location, listing_research
from organization_identity import (
    organization_host as _website_host,
    organization_name_keys,
    root_domain,
    text_value as _text,
)
from source_identity import directory_listing_key as _listing_key


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
    names_by_root = defaultdict(list)
    for row in records:
        if root := root_domain(row["details"]["host"]):
            names_by_root[root].append(organization_name_keys(row["details"]["name"]))
    for row in records:
        root = root_domain(row["details"]["host"])
        names = organization_name_keys(row["details"]["name"])
        row["shared_host"] = bool(names and any(
            other and names.isdisjoint(other) for other in names_by_root[root]
        ))
        row["duplicate_id"] = ids[row["source_id"]] > 1
    return records


def evaluate(connection, row, policy, *, promote=False):
    """Compatibility entry point for callers evaluating one saved Directory row."""
    from crm_core.ingest import evaluate_saved
    from ingest_store import SourceStore

    return evaluate_saved(SourceStore(connection), "directory", row, policy, promote=promote)
