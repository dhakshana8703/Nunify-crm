"""Preview or apply a verified Cvent charge export and opening-balance statement."""

import argparse
import csv
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from getpass import getpass
from pathlib import Path

from werkzeug.security import check_password_hash

from credit_history_store import SqlCreditHistoryStore
from crm_core.credit_history import (
    HistoricalCharge, HistoricalStatement, apply_credit_history, charge_reference, preview_credit_history,
)
from crm_store import connect, is_postgres
from workflow_store import transaction, utcnow


REQUIRED_FIELDS = {"reference", "action", "credits", "at"}
OPTIONAL_FIELDS = {"legacy_user_id", "cvent_company_id", "apollo_person_id"}
STATEMENT_FIELDS = {"reference", "total_credits", "as_of"}


def _legacy_id(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**63:
        raise ValueError("Use a positive numeric legacy ID.")
    return int(value)


def _charges(rows):
    headers = [cell.strip().casefold() for cell in next(rows, ())]
    fields = set(headers)
    if len(fields) != len(headers) or not REQUIRED_FIELDS <= fields <= REQUIRED_FIELDS | OPTIONAL_FIELDS:
        raise ValueError("Use confirmed charge columns reference, action, credits and at, with optional link IDs.")
    charges = []
    for number, cells in enumerate(rows, start=2):
        if not any(cells):
            continue
        if len(cells) != len(headers):
            raise ValueError(f"Confirmed charge export row {number} has the wrong number of columns.")
        row = dict(zip(headers, (cell.strip() for cell in cells)))
        try:
            legacy_user_id = row.get("legacy_user_id")
            legacy_company_id = row.get("cvent_company_id")
            apollo_person_id = row.get("apollo_person_id")
            charges.append(HistoricalCharge.verified(
                row["reference"], row["action"], row["credits"], row["at"],
                _legacy_id(legacy_user_id) if legacy_user_id else None,
                _legacy_id(legacy_company_id) if legacy_company_id else None,
                charge_reference(apollo_person_id) if apollo_person_id else None,
            ))
        except ValueError as error:
            raise ValueError(f"Confirmed charge export row {number}. {error}") from error
    return charges


def read_charges(path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        return _charges(csv.reader(source, strict=True))


def _unique_json(pairs):
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("An approved statement or user map contains a duplicate JSON key.")
    return values


def _read_json(path):
    with path.open(encoding="utf-8-sig") as source:
        return json.load(source, parse_float=Decimal, object_pairs_hook=_unique_json)


def read_statement(path):
    fields = _read_json(path)
    if not isinstance(fields, dict) or fields.keys() != STATEMENT_FIELDS:
        raise ValueError("The approved statement must give reference, total_credits and as_of.")
    return HistoricalStatement.verified(**fields)


def read_user_map(path):
    if path is None:
        return {}
    mapped = _read_json(path)
    if not isinstance(mapped, dict):
        raise ValueError("Use a JSON object mapping legacy user IDs to verified CRM email addresses.")
    approved = {}
    for key, email in mapped.items():
        user_id = _legacy_id(key)
        if not isinstance(email, str) or "@" not in email or user_id in approved:
            raise ValueError("Map each legacy user ID to exactly one verified CRM email address.")
        approved[user_id] = email.strip().casefold()
    return approved


def _preview(database, charges, statement, approved):
    with closing(connect(database, read_only=True)) as connection, connection:
        if is_postgres(database):
            connection.execute("SET default_transaction_isolation = 'repeatable read'")
        connection.execute("BEGIN")
        return preview_credit_history(SqlCreditHistoryStore(connection), charges, statement, approved)


def _apply(database, charges, statement, approved, *, expected, email, password):
    with closing(connect(database)) as connection, transaction(connection):
        store = SqlCreditHistoryStore(connection)
        actor = store.admin_credentials(email)
        if not actor or not check_password_hash(actor["password_hash"], password):
            raise PermissionError("An active Admin sign-in is required to apply Cvent credit history.")
        return apply_credit_history(store, charges, statement, approved, expected=expected,
                                    actor_id=actor["id"], observed=utcnow())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite file or PostgreSQL URL. Use - for a private URL prompt.")
    parser.add_argument("--source", type=Path, required=True, help="Approved CSV of verified provider charges.")
    parser.add_argument("--statement", type=Path, required=True, help="Approved opening statement and cutoff in JSON.")
    parser.add_argument("--user-map", type=Path, help="JSON map of legacy user IDs to verified CRM emails.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview reconciled entries without writes (default).")
    mode.add_argument("--apply", action="store_true", help="Apply the reviewed statement as an active Admin.")
    parser.add_argument("--expect", help="Fingerprint from the reviewed preview.")
    parser.add_argument("--admin-email", help="Active CRM Admin email.")
    options = parser.parse_args(argv)
    if options.apply and not (options.expect and options.admin_email):
        parser.error("--apply requires --expect and --admin-email.")
    try:
        charges = read_charges(options.source)
        statement = read_statement(options.statement)
        approved = read_user_map(options.user_map)
        database = getpass("CRM PostgreSQL URL: ").strip() if options.db == "-" else options.db
        if not is_postgres(database) and not Path(database).is_file():
            raise ValueError("Choose an existing CRM SQLite file or PostgreSQL URL.")
        if options.apply:
            report = _apply(database, charges, statement, approved, expected=options.expect,
                            email=options.admin_email.strip(), password=getpass("CRM Admin password: "))
        else:
            report = _preview(database, charges, statement, approved)
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(2, f"{error}\n")
    except sqlite3.Error:
        parser.exit(2, "The credit history database could not be processed. Check its availability and schema.\n")
    print(json.dumps({"mode": "apply" if options.apply else "preview", **report}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
