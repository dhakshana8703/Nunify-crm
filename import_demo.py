"""Build the curated two-source fixture in a disposable database."""

import argparse
import json
from pathlib import Path

from crm_store import DEFAULT_DB, import_snapshot
from cvent_reads import parse_date_to_iso
from directory_source import load_directory_records
from source_identity import CVENT_EXPORT, CVENT_FIELDS


WORKSPACE = Path(__file__).resolve().parent
SOURCE_PARENT = WORKSPACE.parent
ACCOUNTS = {
    "aarp": {"name": "AARP", "domain": "aarp.org", "organization_type": "Association"},
    "ctaa": {
        "name": "Canadian Technical Asphalt Association",
        "domain": "ctaa.ca",
        "organization_type": "Association",
    },
}
# Selected by original event UUID, with organizer and event host checked below.
AARP_EVENTS = (
    "2446b2ea-3abb-411b-8611-b4132776e274",
    "5f397d9e-6126-452d-8e68-0c2c93c13c6f",
    "31ad5a83-8073-45f8-a114-2ef4e18dcc96",
)


def load_cvent_records(source_dir):
    export = Path(source_dir) / CVENT_EXPORT
    with export.open(encoding="utf-8-sig") as stream:
        rows = json.load(stream)
    selected = {}
    for row in rows:
        event_id = row.get("event_uuid")
        if event_id not in AARP_EVENTS:
            continue
        if event_id in selected:
            raise ValueError(f"Duplicate Cvent event UUID in {export.name}: {event_id}")
        raw = {field: row[field] for field in CVENT_FIELDS if field in row}
        organizer = clean(row.get("organizer")) or clean(row.get("organizer_company"))
        client_domain = clean(row.get("client_domain")).lower()
        source_url = clean(row.get("cvent_url"))
        if (organizer.casefold(), client_domain) != ("aarp", "events.aarp.org"):
            raise ValueError(f"Review the organizer evidence for Cvent event {event_id}")
        if source_url != f"https://web.cvent.com/event/{event_id}":
            raise ValueError(f"Unexpected saved Cvent URL for {event_id}")
        event_url = clean(row.get("resolved_url")) or clean(row.get("event_url"))
        start_date = clean(row.get("start_date"))
        selected[event_id] = {
            "source": "cvent", "source_id": event_id, "source_url": source_url,
            "source_file": export.name, "identity_key": "aarp", "reason": None, "raw": raw,
            "details": {
                "event_name": clean(row.get("event_name")) or "Untitled Cvent Event",
                "event_url": event_url,
                "start_date": start_date,
                "start_date_iso": parse_date_to_iso(start_date),
                "end_date": clean(row.get("end_date")),
                "venue_name": clean(row.get("venue_name")),
                "city": clean(row.get("city")),
                "state": clean(row.get("state_region")) or clean(row.get("state")),
                "country": clean(row.get("country")),
                "organizer_company": organizer,
                "client_domain": client_domain,
                "identity_evidence": "The saved event identifies AARP as the organizer and uses events.aarp.org.",
                "evidence_keys": list(raw),
                "status": "has-website" if event_url else "incomplete",
                "scrape_date": clean(row.get("updated_at")) or clean(row.get("created_at")),
            },
        }
    missing = set(AARP_EVENTS) - selected.keys()
    if missing:
        raise ValueError(f"Missing Cvent events in {export.name}: {', '.join(sorted(missing))}")
    return [selected[event_id] for event_id in AARP_EVENTS]


def clean(value):
    return "" if value is None else str(value).strip()


def import_demo(database_path, cvent_dir, directory_dir):
    records = load_cvent_records(cvent_dir) + load_directory_records(Path(directory_dir))
    return import_snapshot(database_path, ACCOUNTS, records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cvent-dir", type=Path, default=SOURCE_PARENT / "cvent-scrapper")
    parser.add_argument("--directory-dir", type=Path, default=SOURCE_PARENT / "association")
    args = parser.parse_args()
    try:
        result = import_demo(args.db, args.cvent_dir, args.directory_dir)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Import failed: {exc}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
