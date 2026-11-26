import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crm_core.source_worker import fail_run


class FailureStoreDouble:
    def __init__(self):
        self.calls = []

    def retry(self, run_id, retry_after, message):
        self.calls.append(("retry", run_id, retry_after, message))

    def finish(self, run_id, state, observed, *, error=None):
        self.calls.append(("finish", run_id, state, observed, error))

    def health_event(self, source, run_id, kind, message, observed):
        self.calls.append(("health", source, run_id, kind, message, observed))


class SourceWorkerBoundaryTest(unittest.TestCase):
    def test_hb03_scheduled_failure_backoff_and_terminal_health_decisions(self):
        observed = datetime(2026, 11, 26, 3, tzinfo=timezone.utc)
        run = {"id": 1, "source_slug": "directory", "trigger": "scheduled", "retry_count": 1}
        store = FailureStoreDouble()
        fail_run(store, run, "Missing input", observed=observed, retryable=True)
        self.assertEqual(store.calls, [("retry", 1, observed + timedelta(seconds=120), "Missing input")])
        store.calls.clear()
        fail_run(store, dict(run, retry_count=3), "Missing input", observed=observed, retryable=True)
        self.assertEqual(store.calls, [("finish", 1, "failed", observed, "Missing input"),
                                       ("health", "directory", 1, "failed", "Missing input", observed)])

    def test_hb03_worker_loads_without_sources_http_adapter(self):
        probe = "import sys; sys.modules['source_ops'] = None; sys.modules['flask'] = None; import worker"
        result = subprocess.run([sys.executable, "-B", "-c", probe],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
