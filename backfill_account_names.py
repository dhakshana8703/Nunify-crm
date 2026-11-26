"""Preview or apply an audited account name backfill."""

import argparse
import json
import sqlite3
from contextlib import closing
from getpass import getpass
from pathlib import Path

from werkzeug.security import check_password_hash

from account_store import SqlAccountNames, user_for_login
from crm_core.account_names import apply_name_backfill, plan_name_backfill
from crm_store import connect, is_postgres
from workflow_store import transaction, utcnow


def _preview_database(database):
    with closing(connect(database, read_only=True)) as connection, connection:
        if getattr(connection, "dialect", "sqlite") == "postgresql":
            connection.execute("SET default_transaction_isolation = 'repeatable read'")
        connection.execute("BEGIN")
        return plan_name_backfill(SqlAccountNames(connection))


def _apply_database(database, *, expected, email, password):
    with closing(connect(database)) as connection, transaction(connection):
        actor = user_for_login(connection, email)
        if actor is None or actor["role"] != "admin" or not check_password_hash(actor["password_hash"], password):
            raise PermissionError("An active Admin sign-in is required to apply the name backfill.")
        return apply_name_backfill(SqlAccountNames(connection), expected=expected, actor_id=actor["id"], observed=utcnow())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Existing SQLite database file or PostgreSQL URL.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print the name plan without writes (default).")
    mode.add_argument("--apply", action="store_true", help="Apply a reviewed plan as an active Admin.")
    parser.add_argument("--expect", help="Fingerprint from the dry run.")
    parser.add_argument("--admin-email", help="Active CRM Admin email for applying the backfill.")
    options = parser.parse_args(argv)
    if options.apply and not (options.expect and options.admin_email):
        parser.error("--apply requires --expect and --admin-email")
    if not is_postgres(options.db) and not Path(options.db).is_file():
        parser.error("Choose an existing CRM database file or a PostgreSQL connection URL.")
    try:
        if options.apply:
            report = _apply_database(options.db, expected=options.expect, email=options.admin_email.strip(),
                                     password=getpass("CRM Admin password: "))
        else:
            report = _preview_database(options.db)
    except (ValueError, PermissionError) as error:
        parser.exit(2, f"{error}\n")
    except sqlite3.Error:
        parser.exit(2, "The account database could not be processed. Check its availability and schema.\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
