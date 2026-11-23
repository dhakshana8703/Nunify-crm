"""Run development jobs against saved inputs and offline provider adapters."""

import argparse
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import growth_ops
import sales_extensions
import source_ops
import workflow_store
from crm_store import configured_database, connect, initialize


def run_pass(connection, cvent_dir, directory_dir):
    handoff = sales_extensions.deliver_one(connection, sales_extensions.OfflineAEProvider(connection))
    credit = growth_ops.enrich_one(connection, growth_ops.OfflineApollo())
    sources = source_ops.work_one(connection, cvent_dir=cvent_dir, directory_dir=directory_dir)
    qualification = growth_ops.qualify_batch(connection)
    return handoff, credit, sources, qualification


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite file or PostgreSQL URL. Defaults to DATABASE_URL when set.")
    parser.add_argument("--cvent-dir", type=Path, default=source_ops.SOURCE_PARENT / "cvent-scrapper")
    parser.add_argument("--directory-dir", type=Path, default=source_ops.SOURCE_PARENT / "association")
    parser.add_argument("--offline", action="store_true", required=True)
    parser.add_argument("--once", action="store_true", help="Drain current jobs and exit.")
    args = parser.parse_args()
    database = args.db or configured_database()
    initialize(database)
    with closing(connect(database)) as connection:
        workflow_store.initialize(connection)
        sales_extensions.initialize(connection)
        source_ops.initialize(connection)
        growth_ops.initialize(connection)
        while True:
            try:
                handoff, credit, sources, qualification = run_pass(connection, args.cvent_dir, args.directory_dir)
            except sqlite3.OperationalError as error:
                code = getattr(error, "sqlite_errorcode", 0) & 0xFF
                if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                connection.rollback()
                print("SQLite is busy. Retrying the worker pass.", flush=True)
                time.sleep(2)
                continue
            if handoff:
                print(f"Handoff {handoff['id']} - {handoff['status']}", flush=True)
            if credit:
                print(f"Processed enrichment job {credit}", flush=True)
            if sources:
                print(f"Processed source run {sources}", flush=True)
            if qualification:
                print(f"Processed qualification batch for job {qualification}", flush=True)
            if not (handoff or credit or sources or qualification):
                if args.once:
                    break
                time.sleep(2)


if __name__ == "__main__":
    main()
