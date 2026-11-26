"""Preview or apply reviewed Cvent Team allocations from a saved export."""

import argparse
import csv
import json
import sqlite3
from contextlib import closing
from getpass import getpass
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from werkzeug.security import check_password_hash

from crm_core.legacy_allocations import LegacyAllocation, apply_reviewed_allocations, preview_allocations
from crm_store import connect, is_postgres
from legacy_allocation_store import SqlLegacyAllocationStore
from workflow_store import transaction, utcnow


def _legacy_id(value):
    value = str(value).strip()
    if not value.isascii() or not value.isdigit() or int(value) < 1:
        raise ValueError("Use positive numeric Cvent company and user IDs.")
    return int(value)


def _allocation_rows(rows):
    headers = [str(cell or "").strip().casefold() for cell in next(rows, ())]
    if len(set(headers)) != len(headers) or not {"company_id", "user_id"} <= set(headers):
        raise ValueError("The allocation export must have unique company_id and user_id columns.")
    allocations = []
    for number, cells in enumerate(rows, start=2):
        if not any(cells):
            continue
        row = dict(zip(headers, cells))
        try:
            allocations.append(LegacyAllocation(
                _legacy_id(row.get("company_id")), _legacy_id(row.get("user_id")),
                str(row.get("assigned_at") or "").strip(),
            ))
        except ValueError as error:
            raise ValueError(f"Allocation export row {number}. {error}") from error
    return allocations


def read_allocations(path: Path):
    if path.suffix.casefold() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as source:
            return _allocation_rows(csv.reader(source))
    if path.suffix.casefold() == ".xlsx":
        with closing(load_workbook(path, read_only=True, data_only=True, keep_links=False)) as workbook:
            return _allocation_rows(workbook.active.values)
    raise ValueError("Choose a saved Cvent allocation export in CSV or XLSX format.")


def _mapped_users(pairs):
    approved = {}
    for legacy_user, email in pairs:
        user_id = _legacy_id(legacy_user)
        if not isinstance(email, str) or "@" not in email or user_id in approved:
            raise ValueError("Each legacy user ID must map to one approved SDR email.")
        approved[user_id] = email.strip().casefold()
    return approved


def read_user_map(path):
    if path is None:
        return {}
    with path.open(encoding="utf-8-sig") as source:
        approved = json.load(source, object_pairs_hook=_mapped_users)
    if not isinstance(approved, dict):
        raise ValueError("The approved user map must be a JSON object keyed by legacy user ID.")
    return approved


def _preview(database, rows, approved):
    with closing(connect(database, read_only=True)) as connection, connection:
        if is_postgres(database):
            connection.execute("SET default_transaction_isolation = 'repeatable read'")
        connection.execute("BEGIN")
        return preview_allocations(SqlLegacyAllocationStore(connection), rows, approved)


def _apply(database, rows, approved, *, expected, email, password):
    with closing(connect(database)) as connection, transaction(connection):
        store = SqlLegacyAllocationStore(connection)
        actor = store.active_admin(email)
        if not actor or not check_password_hash(actor["password_hash"], password):
            raise PermissionError("An active Admin sign-in is required to apply Cvent allocations.")
        return apply_reviewed_allocations(store, rows, approved, expected=expected, actor=actor, observed=utcnow())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite file or PostgreSQL URL. Use - for a private URL prompt.")
    parser.add_argument("--source", type=Path, required=True, help="Saved Cvent team allocations in CSV or XLSX.")
    parser.add_argument("--user-map", type=Path, help="JSON object mapping legacy user IDs to approved CRM SDR emails.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show a plan without writes (default).")
    mode.add_argument("--apply", action="store_true", help="Apply a reviewed plan as an active Admin.")
    parser.add_argument("--expect", help="Fingerprint from the reviewed preview.")
    parser.add_argument("--admin-email", help="Active CRM Admin email.")
    options = parser.parse_args(argv)
    if options.apply and not (options.expect and options.admin_email and options.user_map):
        parser.error("--apply requires --expect, --admin-email and --user-map.")
    try:
        rows = read_allocations(options.source)
        approved = read_user_map(options.user_map)
        database = getpass("CRM PostgreSQL URL: ").strip() if options.db == "-" else options.db
        if not is_postgres(database) and not Path(database).is_file():
            raise ValueError("Choose an existing CRM SQLite file or PostgreSQL URL.")
        if options.apply:
            report = _apply(database, rows, approved, expected=options.expect, email=options.admin_email.strip(),
                            password=getpass("CRM Admin password: "))
        else:
            report = _preview(database, rows, approved)
    except (OSError, ValueError, BadZipFile, InvalidFileException, ParseError) as error:
        parser.exit(2, f"{error}\n")
    except sqlite3.Error:
        parser.exit(2, "The allocation database could not be processed. Check its availability and schema.\n")
    print(json.dumps({"mode": "apply" if options.apply else "preview", **report}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
