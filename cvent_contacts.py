"""Import explicit business contacts from an offline Cvent event export."""

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing, nullcontext
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from uuid import UUID

from werkzeug.security import check_password_hash

from crm_store import connect, table_exists
from cvent_import import FILE_NAME as CVENT_EXPORT, _host
from workflow_store import audit, matches_domain, timestamp, transaction


SOURCE_PARENT = Path(__file__).resolve().parent.parent
SCHEMA = """
CREATE TABLE IF NOT EXISTS cvent_contact_evidence (
    person_id INTEGER NOT NULL REFERENCES people(id),
    source_record_id INTEGER NOT NULL REFERENCES source_records(id),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (person_id, source_record_id)
)
"""
ORIGIN = "cvent"
EMAIL_LOCAL = re.compile(r"[a-z0-9]+(?:[.!#$%&'*+/=?^_`{|}~-][a-z0-9]+)*", re.ASCII)
ROLE_WORDS = {
    "admin", "administrator", "billing", "board", "conference", "conferences", "contact", "customerservice",
    "donations", "donotreply", "enquiries", "events", "event", "help", "hello", "info", "inquiries", "mail",
    "marketing", "media", "membership", "noreply", "office", "planning", "postmaster", "reception",
    "registration", "registrations", "sales", "staff", "support", "team",
}
GENERIC_NAME_WORDS = ROLE_WORDS | {
    "association", "department", "first", "last", "name", "planner", "services", "unknown",
}
FREE_MAIL = {
    "aol.com", "gmail.com", "hotmail.com", "icloud.com", "live.com", "mail.com", "outlook.com",
    "proton.me", "protonmail.com", "yahoo.com",
}
BLOCKED_STATUSES = {"opted_out", "bounced"}


@dataclass(frozen=True)
class SavedContact:
    name: str
    email: str
    account_id: int
    source_record_id: int


def initialize(connection):
    connection.execute(SCHEMA)


def evidence_for_account(connection, account_id):
    """Return provenance for use after an account or contact access check."""
    if not table_exists(connection, "cvent_contact_evidence"):
        return []
    return [
        dict(row)
        for row in connection.execute("""
            SELECT p.id AS person_id, s.source_id, s.source_url, e.observed_at,
                   json_extract(s.details_json, '$.event_name') AS event_name
            FROM cvent_contact_evidence e
            JOIN people p ON p.id = e.person_id
            JOIN source_records s ON s.id = e.source_record_id
            WHERE p.account_id = ? AND s.account_id = p.account_id AND s.source = 'cvent'
            ORDER BY e.observed_at DESC, s.id
        """, (account_id,))
    ]


def _event_id(value):
    if not isinstance(value, str):
        return ""
    try:
        return str(UUID(value.strip()))
    except ValueError:
        return ""


def _contact_name(value):
    if not isinstance(value, str) or len(value) > 120 or any(char in value for char in "\r\n\t"):
        return ""
    words = value.split()
    if not 2 <= len(words) <= 6:
        return ""
    if any(
        not any(char.isalpha() for char in word)
        or any(not (char.isalpha() or char in ".-'’") for char in word)
        or word.strip(".").casefold() in GENERIC_NAME_WORDS
        for word in words
    ):
        return ""
    return " ".join(words)


def _work_email(value):
    if not isinstance(value, str) or len(value) > 254:
        return ""
    email = value.strip().casefold()
    local, sep, host = email.partition("@")
    if not sep or len(local) > 64 or not EMAIL_LOCAL.fullmatch(local):
        return ""
    if _host(host) != host.removeprefix("www.") or not host.rpartition(".")[2].isalpha() or any(
        matches_domain(host, provider) for provider in FREE_MAIL
    ):
        return ""
    words = [re.sub(r"\d+$", "", part) for part in re.split(r"[._+-]", local)]
    if any(word in ROLE_WORDS for word in words) or "".join(words) in ROLE_WORDS:
        return ""
    return email


def _load_export(source_path):
    with Path(source_path).open(encoding="utf-8-sig") as saved:
        rows = json.load(saved)
    if not isinstance(rows, list):
        raise ValueError("Expected a saved list of Cvent events.")
    return rows


def _blocked(rules, suppressed_people, account_id, account_host, email):
    email_host = email.rpartition("@")[2]
    return email in suppressed_people or any(
        (row["kind"] == "account" and row["value"] == str(account_id))
        or (row["kind"] == "email" and row["value"].casefold() == email)
        or (row["kind"] == "domain" and (
            matches_domain(account_host, row["value"]) or matches_domain(email_host, row["value"])
        ))
        for row in rules
    )


def _candidates(connection, rows, counts):
    links = {
        row["source_id"]: row
        for row in connection.execute("""
            SELECT s.id, s.source_id, s.source_url, s.account_id, a.domain
            FROM source_records s JOIN accounts a ON a.id = s.account_id
            WHERE s.source = 'cvent'
        """)
    }
    rules = connection.execute("SELECT kind, value FROM suppression").fetchall()
    blocked_people = {
        row["email"].casefold()
        for row in connection.execute("""
            SELECT email, do_not_contact, status FROM people WHERE email <> ''
        """)
        if row["do_not_contact"] or row["status"] in BLOCKED_STATUSES
    }
    event_rows = defaultdict(list)
    for row in rows:
        event_id = _event_id(row.get("event_uuid")) if isinstance(row, dict) else ""
        if not event_id:
            counts["invalid_event_rows"] += 1
        elif event_id not in links:
            counts["unlinked_event_rows"] += 1
        else:
            event_rows[event_id].append(row)

    groups = defaultdict(list)
    for event_id, saved_rows in event_rows.items():
        if len(saved_rows) != 1:
            counts["duplicate_linked_event_rows"] += len(saved_rows)
            continue
        row = saved_rows[0]
        link = links[event_id]
        if row.get("cvent_url") != link["source_url"]:
            counts["mismatched_source_rows"] += 1
            continue
        name = _contact_name(row.get("contact_name"))
        email = _work_email(row.get("contact_email"))
        if not name or not email:
            counts["ambiguous_contact_rows"] += 1
            continue
        account_host = _host(link["domain"])
        if not account_host or not matches_domain(email.rpartition("@")[2], account_host):
            counts["mismatched_contact_domain_rows"] += 1
            continue
        if _blocked(rules, blocked_people, link["account_id"], account_host, email):
            counts["suppressed_contact_rows"] += 1
            continue
        groups[link["account_id"], email].append(SavedContact(name, email, link["account_id"], link["id"]))

    for group in groups.values():
        if len({candidate.name.casefold() for candidate in group}) != 1:
            counts["conflicting_contact_rows"] += len(group)
        else:
            yield group


def import_contacts(connection, source_path, *, apply=False, actor_id=None):
    """Return aggregate results. Apply checks admin scope and writes in one local transaction."""
    rows = _load_export(source_path)
    counts = dict.fromkeys((
        "invalid_event_rows", "unlinked_event_rows", "duplicate_linked_event_rows",
        "mismatched_source_rows", "ambiguous_contact_rows", "mismatched_contact_domain_rows",
        "suppressed_contact_rows", "conflicting_contact_rows", "eligible_people",
        "new_people", "existing_people", "new_evidence", "existing_evidence",
    ), 0)
    counts["source_rows"] = len(rows)
    account_ids = set()
    with transaction(connection) if apply else nullcontext():
        if apply:
            if connection.execute("""
                SELECT 1 FROM users WHERE id = ? AND role = 'admin' AND active = 1
            """, (actor_id,)).fetchone() is None:
                raise PermissionError("An active administrator is required to apply a contact import.")
            initialize(connection)
        has_evidence = table_exists(connection, "cvent_contact_evidence")
        for group in _candidates(connection, rows, counts):
            candidate = group[0]
            account_ids.add(candidate.account_id)
            counts["eligible_people"] += 1
            person = connection.execute("""
                SELECT id FROM people WHERE account_id = ? AND email = ?
            """, (candidate.account_id, candidate.email)).fetchone()
            person_id = person["id"] if person else None
            if person_id is None:
                counts["new_people"] += 1
                if apply:
                    person_id = connection.execute("""
                        INSERT INTO people(account_id, name, email, origin)
                        VALUES (?, ?, ?, ?)
                    """, (candidate.account_id, candidate.name, candidate.email, ORIGIN)).lastrowid
            else:
                counts["existing_people"] += 1
            for evidence in group:
                if person_id is not None and has_evidence and connection.execute("""
                    SELECT 1 FROM cvent_contact_evidence WHERE person_id = ? AND source_record_id = ?
                """, (person_id, evidence.source_record_id)).fetchone():
                    counts["existing_evidence"] += 1
                    continue
                counts["new_evidence"] += 1
                if apply:
                    connection.execute("""
                        INSERT INTO cvent_contact_evidence(person_id, source_record_id, observed_at)
                        VALUES (?, ?, ?)
                    """, (person_id, evidence.source_record_id, timestamp()))
        counts["eligible_accounts"] = len(account_ids)
        if apply and (counts["new_people"] or counts["new_evidence"]):
            audit(connection, actor_id, None, "cvent_contacts_imported", counts)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Existing local CRM SQLite database")
    parser.add_argument("--source", type=Path, default=SOURCE_PARENT / "cvent-scrapper" / CVENT_EXPORT)
    parser.add_argument("--apply", action="store_true", help="Write eligible contacts and source links")
    parser.add_argument("--admin-email", help="Local CRM administrator sign-in for apply")
    args = parser.parse_args()
    if args.apply and (not args.admin_email or not sys.stdin.isatty()):
        parser.error("Apply requires --admin-email and an interactive password prompt.")
    if not args.db.is_file():
        parser.error("Choose an existing local CRM database.")
    try:
        with closing(connect(args.db, read_only=not args.apply)) as connection:
            admin_id = None
            if args.apply:
                password = getpass("CRM administrator password: ")
                admin = connection.execute("""
                    SELECT id, password_hash FROM users WHERE email = ? AND role = 'admin' AND active = 1
                """, (args.admin_email.strip(),)).fetchone()
                if admin is None or not check_password_hash(admin["password_hash"], password):
                    parser.exit(2, "CRM administrator authentication failed.\n")
                admin_id = admin["id"]
            result = import_contacts(connection, args.source, apply=args.apply, actor_id=admin_id)
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(2, "The saved Cvent export or local database could not be processed.\n")
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", **result}, sort_keys=True))


if __name__ == "__main__":
    main()
