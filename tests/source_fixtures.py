"""Build synthetic exports that exercise the curated demo's identity checks."""

import json
from collections import defaultdict

from directory_source import SAMPLE_LISTINGS
from import_demo import AARP_EVENTS, CVENT_EXPORT


DEMO_EVENT_NAME = "AARP Fixture Volunteer Summit"
DEMO_MEMBERS = "12,345"


def write_cvent_fixture(directory):
    events = []
    for index, event_id in enumerate(AARP_EVENTS):
        date = f"2026-09-{30 - index:02d}"
        events.append({
            "event_uuid": event_id,
            "event_name": DEMO_EVENT_NAME if index == 0 else f"AARP Fixture Conference {index + 1}",
            "event_type": "Conference",
            "start_date": date,
            "end_date": date,
            "venue_name": "Example Venue",
            "organizer": "AARP",
            "client_domain": "events.aarp.org",
            "cvent_url": f"https://web.cvent.com/event/{event_id}",
            "resolved_url": f"https://events.aarp.org/fixture-{index + 1}",
        })
    path = directory / CVENT_EXPORT
    path.write_text(json.dumps(events), encoding="utf-8")
    return path


def write_demo_fixtures(directory):
    write_cvent_fixture(directory)
    exports = defaultdict(list)
    for selection in SAMPLE_LISTINGS:
        row = {
            "name": selection.name,
            "type": "National",
            "category": "Business" if selection.identity_key == "ctaa" else "Retirement",
            "member_count": DEMO_MEMBERS,
            "website": f"https://{selection.host}/" if selection.host else "",
            "source_url": f"https://directoryofassociations.com/view/?di={selection.source_id}",
        }
        if selection.identity_key == "aarp":
            row["address"] = "1 Fixture Way, Washington, DC 20001, US"
        exports[selection.source_file].append(row)
    for filename, rows in exports.items():
        (directory / filename).write_text(json.dumps(rows), encoding="utf-8")
