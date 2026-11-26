"""Compose configured file readers with the SQL source worker adapter."""

import sqlite3
from functools import partial

from crm_core.ingest import evaluate_saved
from crm_core.source_worker import SAVED_EXPORT_BATCH_SIZE, SourceReader, process_next
from source_connectors import CVENT_DIR, DIRECTORY_DIR, configured_saved_exports
from source_worker_store import SourceWorkerStore
from workflow_store import utcnow


def work_one(connection, *, cvent_dir=CVENT_DIR, directory_dir=DIRECTORY_DIR, connectors=None,
             clock=utcnow, saved_evaluator=evaluate_saved, batch_size=SAVED_EXPORT_BATCH_SIZE):
    if connectors is None:
        connectors = configured_saved_exports(cvent_dir=cvent_dir, directory_dir=directory_dir)
    readers = {
        slug: SourceReader(connector.adapter.FILE_NAME, connector.adapter.DEFAULT_POLICY,
                           partial(connector.adapter.load_rows, connector.directory))
        for slug, connector in connectors.items()
    }
    return process_next(SourceWorkerStore(connection), readers, clock, saved_evaluator=saved_evaluator,
                        batch_size=batch_size, storage_errors=(sqlite3.Error,))
