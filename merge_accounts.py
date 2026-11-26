"""Preview or explicitly apply confirmed same-root account merges."""

import argparse
import json
import sqlite3
from contextlib import closing
from getpass import getpass

from werkzeug.security import check_password_hash

from account_identity_schema import initialize_account_identity
from account_merge_store import SqlAccountMergeStore
from crm_core.account_merge import apply_merge, fingerprint, plan_root_merges
from crm_store import connect
from workflow_store import transaction, utcnow


def _preview(store, observed):
    plans = plan_root_merges(store, observed=observed)
    return plans, fingerprint([plan["fingerprint"] for plan in plans])


def _report_groups(plans):
    fields = ("survivor", "duplicates", "counts", "affected_records", "conflicts", "fingerprint")
    return [{field: plan[field] for field in fields} for plan in plans]


def _preview_database(database):
    with closing(connect(database, read_only=True)) as connection, connection:
        if getattr(connection, "dialect", "sqlite") == "postgresql":
            connection.execute("SET default_transaction_isolation = 'repeatable read'")
        connection.execute("BEGIN")
        plans, version = _preview(SqlAccountMergeStore(connection), utcnow())
    return {"groups": _report_groups(plans), "fingerprint": version}


def _apply_database(database, *, expected, email, password):
    with closing(connect(database)) as connection, transaction(connection):
        actor = connection.execute("""
            SELECT id, password_hash FROM users WHERE email = ? AND role = 'admin' AND active = 1
        """, (email,)).fetchone()
        if not actor or not check_password_hash(actor["password_hash"], password):
            raise PermissionError("An active Admin sign-in is required to apply merges.")
        store = SqlAccountMergeStore(connection)
        observed = utcnow()
        plans, version = _preview(store, observed)
        if not plans:
            return {"merged_groups": 0, "survivor_ids": []}
        if version != expected:
            raise ValueError("The merge plan changed. Review a new dry run before applying it.")
        if any(plan["conflicts"] for plan in plans):
            raise ValueError("Resolve the conflicts shown in the dry run before applying it.")
        initialize_account_identity(connection)
        survivors = [apply_merge(store, plan, actor_id=actor["id"], observed=observed) for plan in plans]
        return {"merged_groups": len(survivors), "survivor_ids": survivors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite database file or PostgreSQL URL.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print the merge plan without making changes (default).")
    mode.add_argument("--apply", action="store_true", help="Apply a reviewed plan as an active Admin.")
    parser.add_argument("--expect", help="Fingerprint from the dry run.")
    parser.add_argument("--admin-email", help="Active CRM Admin email for applying merges.")
    options = parser.parse_args(argv)
    if options.apply and not (options.expect and options.admin_email):
        parser.error("--apply requires --expect and --admin-email")
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
