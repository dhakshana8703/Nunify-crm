"""Import explicit business contacts from an offline Cvent event export."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing, nullcontext
from getpass import getpass
from pathlib import Path

from werkzeug.security import check_password_hash

from crm_core.legacy_contacts import BLOCKED_STATUSES, _blocked, _work_email, import_cvent_contacts
from crm_store import connect
from legacy_contact_store import CVENT_CONTACT_EVIDENCE_SCHEMA as SCHEMA
from legacy_contact_store import SqlLegacyContactStore
from legacy_contact_store import evidence_for_account
from legacy_contact_store import initialize_cvent_evidence as initialize
from source_identity import CVENT_EXPORT
from workflow_store import transaction


SOURCE_PARENT = Path(__file__).resolve().parent.parent


def _load_export(source_path):
    with Path(source_path).open(encoding="utf-8-sig") as saved:
        rows = json.load(saved)
    if not isinstance(rows, list):
        raise ValueError("Expected a saved list of Cvent events.")
    return rows


def import_contacts(connection, source_path, *, apply=False, actor_id=None):
    """Run the saved contact reconciliation on the caller's SQLite or PostgreSQL database."""
    rows = _load_export(source_path)
    with transaction(connection) if apply else nullcontext():
        return import_cvent_contacts(SqlLegacyContactStore(connection), rows, apply=apply, actor_id=actor_id)


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
                admin = SqlLegacyContactStore(connection).admin_credentials(args.admin_email.strip())
                if admin is None or not check_password_hash(admin["password_hash"], password):
                    parser.exit(2, "CRM administrator authentication failed.\n")
                admin_id = admin["id"]
            result = import_contacts(connection, args.source, apply=args.apply, actor_id=admin_id)
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(2, "The saved Cvent export or local database could not be processed.\n")
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", **result}, sort_keys=True))


if __name__ == "__main__":
    main()
