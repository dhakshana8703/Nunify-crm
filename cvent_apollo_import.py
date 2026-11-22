"""Reconcile saved Apollo candidates from a read-only Cvent cache snapshot."""

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing, nullcontext
from getpass import getpass
from pathlib import Path

from werkzeug.security import check_password_hash

from crm_store import connect
from cvent_contacts import BLOCKED_STATUSES, SOURCE_PARENT, _blocked, _work_email
from cvent_import import _host, _names, _platform, _text
from workflow_store import audit, normalized_linkedin_profile, timestamp, transaction


SCHEMA = """
CREATE TABLE IF NOT EXISTS cvent_apollo_leads (
    apollo_id TEXT PRIMARY KEY,
    person_id INTEGER NOT NULL UNIQUE REFERENCES people(id),
    legacy_company_id INTEGER NOT NULL,
    company_domain TEXT NOT NULL,
    discovered_at TEXT,
    unlocked_at TEXT,
    imported_at TEXT NOT NULL
)
"""


def _snapshot(path):
    with closing(connect(path, read_only=True)) as source:
        source.execute("BEGIN")
        candidates = source.execute("""
            SELECT apollo_id, company_id, company_name, domain, name, title, persona,
                   is_unlocked, email, linkedin_url, discovered_at, unlocked_at
            FROM apollo_candidates ORDER BY apollo_id
        """).fetchall()
        verified = set()
        for row in source.execute("SELECT apollo_id, email, raw_json FROM apollo_people_cache WHERE email <> ''"):
            try:
                profile = json.loads(row["raw_json"] or "{}")
            except (ValueError, TypeError):
                continue
            email = _work_email(row["email"])
            if (email and isinstance(profile, dict) and profile.get("email_status") == "verified"
                    and profile.get("id") == row["apollo_id"] and _work_email(profile.get("email")) == email):
                verified.add((row["apollo_id"], email))
    return candidates, verified


def _eligible(connection, candidates, verified, counts):
    domains = defaultdict(set)
    for account in connection.execute("SELECT id, domain FROM accounts"):
        domains[_host(account["domain"])].add(account["id"])
    organizers = defaultdict(set)
    for row in connection.execute("""
        SELECT account_id, details_json FROM source_records
        WHERE source = 'cvent' AND account_id IS NOT NULL
    """):
        details = json.loads(row["details_json"])
        host = _host(details.get("organizer_domain"))
        if host and details.get("identity_evidence"):
            organizers[row["account_id"], host].update(_names(details.get("organizer_company")))

    company_hosts, host_companies = defaultdict(set), defaultdict(set)
    for row in candidates:
        host = _host(row["domain"])
        if host and row["company_id"]:
            company_hosts[row["company_id"]].add(host)
            host_companies[host].add(row["company_id"])
    rules = connection.execute("SELECT kind, value FROM suppression").fetchall()
    blocked_people = {
        row["email"].casefold()
        for row in connection.execute("SELECT email, do_not_contact, status FROM people WHERE email <> ''")
        if row["do_not_contact"] or row["status"] in BLOCKED_STATUSES
    }

    for row in candidates:
        apollo_id, name, host = _text(row["apollo_id"]), _text(row["name"]), _host(row["domain"])
        company_id = row["company_id"]
        unlocked = row["is_unlocked"]
        if (not apollo_id or apollo_id != row["apollo_id"] or len(apollo_id) > 120 or not apollo_id.isprintable()
                or not isinstance(company_id, int) or company_id <= 0
                or not name or len(name) > 180 or not name.isprintable()
                or not any(char.isalpha() for char in name) or not host or unlocked not in (0, 1)):
            counts["invalid"] += 1
            continue
        if _platform(host):
            counts["suppressed"] += 1
            continue
        account_ids = domains.get(host, ())
        if (len(account_ids) != 1 or len(company_hosts[company_id]) != 1
                or len(host_companies[host]) != 1):
            counts["review_identity"] += 1
            continue
        account_id = next(iter(account_ids))
        if organizers[account_id, host].isdisjoint(_names(row["company_name"])):
            counts["review_identity"] += 1
            continue
        if _blocked(rules, blocked_people, account_id, host, _text(row["email"]).casefold()):
            counts["suppressed"] += 1
            continue

        email = _work_email(row["email"]) if unlocked else ""
        if email and _host(email.rpartition("@")[2]) != host:
            email = ""
        linkedin = normalized_linkedin_profile(_text(row["linkedin_url"])) if unlocked else ""
        yield {
            "apollo_id": apollo_id, "legacy_company_id": company_id, "company_domain": host,
            "account_id": account_id, "name": name, "title": _text(row["title"])[:180],
            "persona": _text(row["persona"])[:24], "email": email, "linkedin_url": linkedin or "",
            "unlocked": unlocked, "email_verified": int(bool(email and (apollo_id, email) in verified)),
            "discovered_at": row["discovered_at"], "unlocked_at": row["unlocked_at"] if unlocked else None,
        }


def _channels(lead):
    return [
        (lead["account_id"], field, lead[field].casefold())
        for field in ("email", "linkedin_url") if lead[field]
    ]


def _has_crosswalk(connection):
    return connection.execute("""
        SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cvent_apollo_leads'
    """).fetchone() is not None


def import_leads(connection, source_path, *, apply=False, actor_id=None):
    """Import verified account matches and return an aggregate run summary."""
    candidates, verified = _snapshot(source_path)
    counts = dict.fromkeys((
        "invalid", "suppressed", "review_identity", "review_contact", "new_people", "updated_people",
        "existing_people", "linked_accounts", "importable_unlocked", "saved_emails", "verified_emails",
        "saved_linkedin",
    ), 0)
    counts["source_rows"] = len(candidates)
    counts["source_unlocked"] = sum(row["is_unlocked"] == 1 for row in candidates)
    accounts = set()
    with transaction(connection) if apply else nullcontext():
        if apply:
            if connection.execute(
                "SELECT 1 FROM users WHERE id=? AND role='admin' AND active=1", (actor_id,)
            ).fetchone() is None:
                raise PermissionError("An active administrator is required to apply a lead import.")
            connection.execute(SCHEMA)
        existing = {
            row["apollo_id"]: row
            for row in connection.execute("""
                SELECT m.*, p.account_id, p.email, p.linkedin_url, p.unlocked, p.email_verified,
                       p.do_not_contact, p.status
                FROM cvent_apollo_leads m JOIN people p ON p.id = m.person_id
            """)
        } if _has_crosswalk(connection) else {}
        people_channels = {
            channel: row["id"]
            for row in connection.execute("SELECT id, account_id, email, linkedin_url FROM people")
            for channel in _channels(row)
        }
        leads = list(_eligible(connection, candidates, verified, counts))
        claims = Counter(channel for lead in leads for channel in _channels(lead))
        for lead in leads:
            linked = existing.get(lead["apollo_id"])
            person_id = linked["person_id"] if linked else None
            if linked and any(
                linked[field] != lead[field] for field in ("account_id", "legacy_company_id", "company_domain")
            ):
                counts["review_identity"] += 1
                continue
            if linked and (linked["do_not_contact"] or linked["status"] in BLOCKED_STATUSES):
                counts["suppressed"] += 1
                continue
            if any(claims[channel] > 1 or people_channels.get(channel) not in (None, person_id)
                   for channel in _channels(lead)):
                counts["review_contact"] += 1
                continue
            if linked and any(
                linked[field] and lead[field] and linked[field].casefold() != lead[field].casefold()
                for field in ("email", "linkedin_url")
            ):
                counts["review_contact"] += 1
                continue

            accounts.add(lead["account_id"])
            for metric, field in (
                ("importable_unlocked", "unlocked"), ("saved_emails", "email"),
                ("verified_emails", "email_verified"), ("saved_linkedin", "linkedin_url"),
            ):
                counts[metric] += bool(lead[field])
            if linked:
                updated = _refresh_link(connection, linked, lead, apply=apply)
                counts["updated_people" if updated else "existing_people"] += 1
            else:
                counts["new_people"] += 1
                if apply:
                    _insert_lead(connection, lead)
        counts["linked_accounts"] = len(accounts)
        if apply and (counts["new_people"] or counts["updated_people"]):
            audit(connection, actor_id, None, "cvent_apollo_leads_imported", counts)
    return counts


def _insert_lead(connection, lead):
    person_id = connection.execute("""
        INSERT INTO people(account_id, name, title, email, persona, origin, unlocked, email_verified, linkedin_url)
        VALUES (:account_id, :name, :title, :email, :persona, 'apollo', :unlocked, :email_verified, :linkedin_url)
    """, lead).lastrowid
    connection.execute("""
        INSERT INTO cvent_apollo_leads
            (apollo_id, person_id, legacy_company_id, company_domain, discovered_at, unlocked_at, imported_at)
        VALUES (:apollo_id, :person_id, :legacy_company_id, :company_domain, :discovered_at, :unlocked_at, :imported_at)
    """, dict(lead, person_id=person_id, imported_at=timestamp()))


def _refresh_link(connection, linked, lead, *, apply):
    fields = {
        "email": linked["email"] or lead["email"],
        "linkedin_url": linked["linkedin_url"] or lead["linkedin_url"],
        "unlocked": max(linked["unlocked"], lead["unlocked"]),
        "email_verified": max(linked["email_verified"], lead["email_verified"]),
    }
    changed = any(linked[field] != value for field, value in fields.items())
    unlocked_at = linked["unlocked_at"] or lead["unlocked_at"]
    if apply and changed:
        connection.execute("""
            UPDATE people SET email=:email, linkedin_url=:linkedin_url, unlocked=:unlocked,
                              email_verified=:email_verified, details_revision=details_revision+1
            WHERE id=:person_id
        """, dict(fields, person_id=linked["person_id"]))
    if apply and unlocked_at != linked["unlocked_at"]:
        connection.execute("UPDATE cvent_apollo_leads SET unlocked_at=? WHERE apollo_id=?",
                           (unlocked_at, lead["apollo_id"]))
    return changed or unlocked_at != linked["unlocked_at"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Existing local CRM SQLite database")
    parser.add_argument("--source", type=Path, default=SOURCE_PARENT / "cvent-scrapper" / "cvent_cache.db")
    parser.add_argument("--apply", action="store_true", help="Write eligible legacy leads")
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
                    SELECT id, password_hash FROM users WHERE email=? AND role='admin' AND active=1
                """, (args.admin_email.strip(),)).fetchone()
                if admin is None or not check_password_hash(admin["password_hash"], password):
                    parser.exit(2, "CRM administrator authentication failed.\n")
                admin_id = admin["id"]
            result = import_leads(connection, args.source, apply=args.apply, actor_id=admin_id)
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(2, "The saved Apollo cache or local database could not be processed.\n")
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", **result}, sort_keys=True))


if __name__ == "__main__":
    main()
