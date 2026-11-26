"""Run development jobs against saved inputs and offline provider adapters."""

import argparse
import sqlite3
import time
from contextlib import closing
from functools import partial
from pathlib import Path

import worker_status
import workflow_store
from crm_core.growth import QUALIFICATION_BATCH_SIZE, enrich_next, run_qualification_batch, score_qualification
from crm_core.handoffs import deliver_pending_handoff
from crm_store import configured_database, connect, initialize
from growth_store import OfflineApollo, SqlGrowthStore, initialize as initialize_growth
from handoff_store import OfflineAEProvider, SqlHandoffStore, initialize as initialize_handoffs
from source_connectors import CVENT_DIR, DIRECTORY_DIR, configured_saved_exports
from source_run_store import initialize as initialize_sources
from source_worker_service import work_one as process_source_run


def run_pass(connection, cvent_dir, directory_dir, *, connectors=None):
    clock = workflow_store.utcnow
    growth = SqlGrowthStore(connection)
    handoff = deliver_pending_handoff(SqlHandoffStore(connection), OfflineAEProvider(connection), clock=clock)
    credit = enrich_next(growth, OfflineApollo(), clock=clock)
    sources = process_source_run(connection, cvent_dir=cvent_dir, directory_dir=directory_dir, connectors=connectors,
                                 clock=clock)
    qualification = run_qualification_batch(growth, partial(score_qualification, growth), clock=clock,
                                            limit=QUALIFICATION_BATCH_SIZE)
    return handoff, credit, sources, qualification


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite file or PostgreSQL URL. Defaults to DATABASE_URL when set.")
    parser.add_argument("--cvent-dir", type=Path, default=CVENT_DIR)
    parser.add_argument("--directory-dir", type=Path, default=DIRECTORY_DIR)
    parser.add_argument("--offline", action="store_true", required=True)
    parser.add_argument("--once", action="store_true", help="Drain current jobs and exit.")
    args = parser.parse_args()
    database = args.db or configured_database()
    connectors = configured_saved_exports(cvent_dir=args.cvent_dir, directory_dir=args.directory_dir)
    initialize(database)
    with closing(connect(database)) as connection:
        workflow_store.initialize(connection)
        initialize_handoffs(connection)
        initialize_sources(connection)
        initialize_growth(connection)
        next_status_update = 0
        while True:
            try:
                handoff, credit, sources, qualification = run_pass(
                    connection, args.cvent_dir, args.directory_dir, connectors=connectors,
                )
                observed_at = time.monotonic()
                if observed_at >= next_status_update:
                    worker_status.record_pass(connection)
                    next_status_update = observed_at + 30
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
