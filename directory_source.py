"""Load the explicitly reviewed Directory of Associations demo listings."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class ListingSelection:
    source_file: str
    source_id: str
    name: str
    host: str | None
    identity_key: str | None
    reason: str


SAMPLE_LISTINGS = (
    ListingSelection(
        "all_associations.json",
        "20BF96A7-8948-42A9-806C-C8880869AE3E",
        "American Association of Retired Persons",
        "aarp.org",
        "aarp",
        "The listing identifies the national AARP organization and uses the AARP homepage.",
    ),
    ListingSelection(
        "canada_associations.json",
        "6B11A19E-4B3F-4DCB-A491-5112D995798A",
        "Canadian Technical Asphalt Association",
        "ctaa.ca",
        "ctaa",
        "The listing identifies a distinct association with its own website.",
    ),
    ListingSelection(
        "all_associations.json",
        "3A137753-0474-45FB-B646-BE89EEC1D64A",
        "AARP Grief, Loss and End of Life Planning",
        "aarp.org",
        None,
        "The listing shares aarp.org and describes a caregiving program with its own website path.",
    ),
    ListingSelection(
        "all_associations.json",
        "74031B9D-44C3-4CCF-8123-CC77414C2220",
        "National Retired Teachers Association, Division of AARP",
        "aarp.org",
        None,
        "The listing shares aarp.org and identifies itself as a division of AARP.",
    ),
    ListingSelection(
        "all_associations.json",
        "3B356874-E4E5-4B78-A008-F6AC437630BA",
        "AARP, Utah State Office",
        "states.aarp.org",
        None,
        "The listing uses an AARP subdomain and identifies itself as the Utah state office.",
    ),
    ListingSelection(
        "all_associations.json",
        "E36C32EE-7FEA-42C5-BA71-2F49EEADEA3F",
        "Delaware Nurses Association",
        None,
        None,
        "The listing has a blank website and needs an identity review.",
    ),
)

RESEARCH_FIELDS = ("contact_person", "contact_title", "phone", "conventions_events")
RAW_FIELDS = (
    "name",
    "type",
    "category",
    "member_count",
    "city",
    "state",
    "state_code",
    "country",
    "address",
    "last_updated",
    "website",
    "source_url",
    *RESEARCH_FIELDS,
)
POSTAL_LOCATION = re.compile(
    r",\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s*"
    r"(?:\d{5}(?:-\d{4})?|[A-Z]\d[A-Z]\s*\d[A-Z]\d)"
    r"\s*,\s*(?P<country>US|CA)\s*$",
    re.IGNORECASE,
)
COUNTRY_NAMES = {"US": "United States", "CA": "Canada"}


def listing_research(row: dict) -> dict[str, str]:
    return {
        field: value.strip() for field in RESEARCH_FIELDS
        if isinstance(value := row.get(field), str) and value.strip()
    }


def _location(row: dict) -> tuple[str | None, str | None, str | None]:
    city = row.get("city")
    state = row.get("state_code") or row.get("state")
    country = row.get("country")
    address_match = POSTAL_LOCATION.search(row.get("address") or "")
    if address_match:
        address_state = address_match["state"].upper()
        if not state or state.upper() == address_state:
            city = city or address_match["city"].strip()
            state = state or address_state
            country = country or address_match["country"]
    return city, state, COUNTRY_NAMES.get((country or "").upper(), country)


def _listing_id(source_url: str) -> str:
    params = parse_qs(urlsplit(source_url).query)
    return params.get("di", [""])[0].removeprefix("{").removesuffix("}").upper()


def _make_record(selection: ListingSelection, row: dict) -> dict:
    source_url = row["source_url"]
    parsed_source = urlsplit(source_url)
    if parsed_source.scheme != "https" or parsed_source.hostname != "directoryofassociations.com":
        raise ValueError(f"Unexpected directory source URL for {selection.source_id}")

    website = row.get("website") or ""
    parsed_website = urlsplit(website)
    host = (parsed_website.hostname or "").removeprefix("www.") or None
    if (
        row.get("name") != selection.name
        or host != selection.host
        or (website and parsed_website.scheme not in {"http", "https"})
        or (selection.identity_key == "aarp" and parsed_website.path not in {"", "/"})
    ):
        raise ValueError(f"Review changed identity evidence for directory listing {selection.source_id}")

    city, state, country = _location(row)
    return {
        "source": "directory",
        "source_id": selection.source_id,
        "source_url": source_url,
        "source_file": selection.source_file,
        "identity_key": selection.identity_key,
        "reason": selection.reason if selection.identity_key is None else None,
        "raw": {field: row[field] for field in RAW_FIELDS if field in row},
        "details": {
            "id": selection.source_id,
            "name": row["name"],
            "listing_type": row.get("type"),
            "category": row.get("category"),
            "members": row.get("member_count"),
            "city": city,
            "state": state,
            "country": country,
            "address": row.get("address"),
            "website": row.get("website"),
            "source_url": source_url,
            "host": host,
            "last_updated": row.get("last_updated"),
            "identity_evidence": selection.reason,
            **listing_research(row),
        },
    }


def load_directory_records(source_dir: Path) -> list[dict]:
    """Read the sample from the saved masters using their original listing IDs."""
    selections = {selection.source_id: selection for selection in SAMPLE_LISTINGS}
    records = {}
    source_files = dict.fromkeys(selection.source_file for selection in SAMPLE_LISTINGS)
    for source_file in source_files:
        with (source_dir / source_file).open(encoding="utf-8-sig") as saved_data:
            for row in json.load(saved_data):
                source_url = row.get("source_url")
                if not source_url:
                    continue
                source_id = _listing_id(source_url)
                selection = selections.get(source_id)
                if selection is None:
                    continue
                if source_id in records or source_file != selection.source_file:
                    raise ValueError(f"Unexpected duplicate or source file for directory listing {source_id}")
                records[source_id] = _make_record(selection, row)

    missing = selections.keys() - records.keys()
    if missing:
        raise ValueError(f"Missing directory listings in saved data: {', '.join(sorted(missing))}")
    return [records[selection.source_id] for selection in SAMPLE_LISTINGS]
