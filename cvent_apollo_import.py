"""Reconcile saved Apollo candidates from a read-only Cvent cache snapshot."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing, nullcontext
from getpass import getpass
from pathlib import Path

from werkzeug.security import check_password_hash

from crm_core.legacy_contacts import import_saved_leads
from crm_store import connect
from legacy_contact_store import APOLLO_LEADS_SCHEMA as SCHEMA
from legacy_contact_store import SqlLegacyContactStore
from workflow_store import transaction


SOURCE_PARENT = Path(__file__).resolve().parent.parent


def _snapshot(path):
    with closing(connect(path, read_only=True)) as source:
        source.execute("BEGIN")
        candidates = source.execute("""
            SELECT apollo_id, company_id, company_name, domain, name, title, persona,
                   is_unlocked, email, linkedin_url, discovered_at, unlocked_at
            FROM apollo_candidates ORDER BY apollo_id
        """).fetchall()
        proofs = []
        for row in source.execute("SELECT apollo_id, email, raw_json FROM apollo_people_cache WHERE email <> ''"):
            try:
                profile = json.loads(row["raw_json"] or "{}")
            except (ValueError, TypeError):
                continue
            proofs.append(dict(row, profile=profile))
    return candidates, proofs


def import_leads(connection, source_path, *, apply=False, actor_id=None):
    """Reconcile a saved Apollo snapshot against this database in one transaction."""
    candidates, proofs = _snapshot(source_path)
    with transaction(connection) if apply else nullcontext():
        return import_saved_leads(SqlLegacyContactStore(connection), candidates, proofs, apply=apply, actor_id=actor_id)


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
                admin = SqlLegacyContactStore(connection).admin_credentials(args.admin_email.strip())
                if admin is None or not check_password_hash(admin["password_hash"], password):
                    parser.exit(2, "CRM administrator authentication failed.\n")
                admin_id = admin["id"]
            result = import_leads(connection, args.source, apply=args.apply, actor_id=admin_id)
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(2, "The saved Apollo cache or local database could not be processed.\n")
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", **result}, sort_keys=True))


if __name__ == "__main__":
    main()
