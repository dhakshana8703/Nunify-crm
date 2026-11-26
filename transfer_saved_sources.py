"""Copy prepared local Cvent and Association data to the existing hosted CRM."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from getpass import getpass
from pathlib import Path
from urllib.parse import urlsplit

from werkzeug.security import check_password_hash

from account_facts_store import refresh_account_facts
from crm_core.transfer import transfer_snapshot
from crm_store import connect, is_postgres
from transfer_store import SqlSnapshotTransfer, read_snapshot, transfer_transaction


DEFAULT_SOURCE = Path(__file__).resolve().parent / "instance" / "association-trial.sqlite3"


def _admin_id(store, credentials):
    email, password = credentials
    user = store.destination_admin(email)
    if user is None or not check_password_hash(user["password_hash"], password):
        raise ValueError("Hosted CRM administrator sign-in failed.")
    return user["id"]


def transfer_saved_sources(source_path, destination, *, apply=False, admin=None, companies_only=False,
                           require_apply_ready=False):
    """Preview or add the source snapshot, preserving all existing destination work."""
    source_path = Path(source_path)
    if not source_path.is_file():
        raise ValueError("Choose an existing local CRM SQLite file.")
    postgres = is_postgres(destination)
    if not postgres and source_path.resolve() == Path(destination).resolve():
        raise ValueError("Choose a separate destination database.")
    snapshot = read_snapshot(source_path, companies_only=companies_only)
    with closing(connect(destination, read_only=not apply)) as connection:
        store = SqlSnapshotTransfer(connection)
        with transfer_transaction(connection, apply=apply, postgres=postgres, require_apply_ready=require_apply_ready):
            actor_id = _admin_id(store, admin) if apply and admin else None
            counts = transfer_snapshot(store, *snapshot, apply=apply, actor_id=actor_id)
            if apply and any(counts.get(key, 0) for key in (
                "accounts_added", "source_records_added", "qualifications_added", "company_evidence_added",
            )):
                refresh_account_facts(connection)
            return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Existing local CRM SQLite file")
    parser.add_argument("--apply", action="store_true", help="Write the transfer. Default is a read-only preview.")
    parser.add_argument("--companies-only", action="store_true", help="Include the saved company roster and omit contact data.")
    parser.add_argument("--admin-email", default="admin@nunify.com", help="Hosted CRM administrator email for apply")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("Run this in an interactive terminal. The database URI is entered privately.")

    destination = getpass("Direct Neon URI for the hosted CRM: ").strip()
    if not is_postgres(destination) or "-pooler." in (urlsplit(destination).hostname or ""):
        parser.error("Enter the direct PostgreSQL URI from Neon Connect with pooling turned off.")
    try:
        if args.companies_only and args.apply:
            preview = transfer_saved_sources(args.source, destination, companies_only=True, require_apply_ready=True)
            print(json.dumps(dict(mode="preview", **preview), indent=2), flush=True)
        admin = (args.admin_email, getpass("Hosted CRM administrator password: ")) if args.apply else None
        counts = transfer_saved_sources(args.source, destination, apply=args.apply, admin=admin,
                                        companies_only=args.companies_only)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(2, f"Transfer stopped. {error}\n")
    print(json.dumps({"mode": "apply" if args.apply else "preview", **counts}, indent=2))


if __name__ == "__main__":
    main()
