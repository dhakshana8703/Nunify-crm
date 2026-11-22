import json
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app import create_app
from crm_store import connect
from source_ops import work_one
from source_schedule import calendar_schedule, next_calendar_run
from workflow_store import DEMO_PASSWORD


def utc_at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class CalendarScheduleTest(unittest.TestCase):
    def test_daily_wall_time_across_skipped_and_repeated_dst_hours(self):
        spring = calendar_schedule("daily", "America/New_York", "02:30", None)
        spring_due = next_calendar_run(spring, utc_at(2026, 3, 7, 8))
        self.assertEqual(spring_due, utc_at(2026, 3, 8, 7, 30))
        self.assertEqual(next_calendar_run(spring, spring_due), utc_at(2026, 3, 9, 6, 30))

        autumn = calendar_schedule("daily", "America/New_York", "01:30", None)
        self.assertEqual(next_calendar_run(autumn, utc_at(2026, 10, 31, 6)), utc_at(2026, 11, 1, 5, 30))
        self.assertEqual(next_calendar_run(autumn, utc_at(2026, 11, 1, 6, 15)), utc_at(2026, 11, 2, 6, 30))

    def test_day_31_clamps_in_february_and_returns_to_31_in_march(self):
        schedule = calendar_schedule("monthly", "Asia/Kolkata", "09:00", 31)
        for year, last_day in ((2026, 28), (2028, 29)):
            with self.subTest(year=year):
                february = next_calendar_run(schedule, utc_at(year, 1, 31, 3, 30))
                self.assertEqual(february, utc_at(year, 2, last_day, 3, 30))
                self.assertEqual(next_calendar_run(schedule, february), utc_at(year, 3, 31, 3, 30))


class ScheduledSourceTest(unittest.TestCase):
    def setUp(self):
        instance = Path(__file__).resolve().parents[1] / "instance"
        self.folder = instance / f"scheduling-test-{uuid4().hex}"
        self.folder.mkdir(parents=True)
        self.addCleanup(self.remove_files)
        self.database = self.folder / "scheduling.sqlite3"
        self.saved_export = self.folder / "all_associations.json"
        self.app = create_app(self.database, demo=True)
        self.app.testing = True
        self.admin = self.sign_in("admin", "/sources")

    def remove_files(self):
        for path in self.folder.iterdir():
            path.unlink()
        self.folder.rmdir()

    def csrf(self, client):
        with client.session_transaction() as session:
            return session["csrf"]

    def sign_in(self, username, workspace):
        client = self.app.test_client()
        self.assertEqual(client.get("/login").status_code, 200)
        signed_in = self.post(client, "/login", email=f"{username}@example.test", password=DEMO_PASSWORD)
        self.assertEqual(signed_in.status_code, 303)
        self.assertEqual(client.get(workspace).status_code, 200)
        return client

    def post(self, client, url, **data):
        return client.post(url, data={"csrf_token": self.csrf(client), **data})

    def configure(self, observed_at, **settings):
        with patch("source_ops.utcnow", return_value=observed_at):
            response = self.post(self.admin, "/sources/directory/schedule", **settings)
        self.assertEqual((response.status_code, response.location), (303, "/sources"))

    def work_at(self, observed_at):
        with patch("source_ops.utcnow", return_value=observed_at), closing(connect(self.database)) as connection:
            return work_one(connection, directory_dir=self.folder)

    def row(self, query, *parameters):
        with closing(connect(self.database, read_only=True)) as connection:
            return connection.execute(query, parameters).fetchone()

    def source_due_at(self):
        result = self.row("SELECT next_run_at FROM ingest_sources WHERE slug = 'directory'")["next_run_at"]
        return datetime.fromisoformat(result) if result else None

    def save_listings(self, count):
        records = [{
            "name": f"Scheduled association {index}",
            "type": "National",
            "category": "Business",
            "member_count": "2500",
            "website": f"https://scheduled-{index}.example",
            "source_url": f"https://directoryofassociations.com/view/?di=10000000-0000-4000-8000-{index:012d}",
        } for index in range(1, count + 1)]
        self.saved_export.write_text(json.dumps(records), encoding="utf-8")

    def test_admin_calendar_settings_and_pause_are_visible_and_audited(self):
        observed_at = utc_at(2026, 3, 7, 16)
        settings = dict(cadence="weekly", timezone="America/New_York", local_time="09:15", weekday=6)
        manager = self.sign_in("manager", "/assignment")
        self.assertEqual(self.post(manager, "/sources/directory/schedule", **settings).status_code, 403)
        self.configure(observed_at, **settings)
        schedule = self.row("""
            SELECT cadence, timezone, local_time, schedule_day FROM source_schedules WHERE source_slug = 'directory'
        """)
        self.assertEqual(tuple(schedule), ("weekly", "America/New_York", "09:15", 6))
        due_at = utc_at(2026, 3, 8, 13, 15)
        self.assertEqual(self.source_due_at(), due_at)
        self.assertIn(b"Next run 2026-03-08 13:15 UTC", self.admin.get("/sources").data)

        self.configure(observed_at, **{**settings, "timezone": "Invalid/Timezone"})
        self.assertIn(b"Enter a valid IANA timezone", self.admin.get("/sources").data)
        self.assertEqual(self.source_due_at(), due_at)
        self.configure(observed_at, cadence="paused")
        self.assertIsNone(self.source_due_at())
        self.assertIn(b"Schedule paused", self.admin.get("/sources").data)
        self.assertIsNone(self.work_at(due_at))
        audit = self.row("""
            SELECT COUNT(*) AS total FROM audit_events a JOIN users u ON u.id = a.actor_id
            WHERE action = 'source_schedule_changed' AND u.role = 'admin'
        """)
        self.assertEqual(audit["total"], 2)

    def test_due_occurrence_skips_an_active_run_and_keeps_next_occurrence(self):
        observed_at = utc_at(2026, 11, 20, 11, 59)
        due_at = utc_at(2026, 11, 20, 12)
        self.configure(observed_at, cadence="daily", timezone="UTC", local_time="12:00")
        self.save_listings(1)
        queued = self.post(self.admin, "/sources/directory/export")
        self.assertEqual(queued.status_code, 303)
        active_run_id = int(queued.location.rsplit("/", 1)[1])
        self.assertEqual(self.work_at(due_at), active_run_id)
        self.assertIsNone(self.work_at(due_at))
        self.assertEqual(self.row("SELECT COUNT(*) FROM source_runs")[0], 1)
        active_run = self.row("SELECT trigger, state FROM source_runs WHERE id = ?", active_run_id)
        self.assertEqual(tuple(active_run), ("manual", "ready"))
        skipped = self.row("SELECT kind, source_slug, run_id, occurred_at FROM source_health_events")
        self.assertEqual(tuple(skipped)[:3], ("skipped", "directory", active_run_id))
        self.assertEqual(datetime.fromisoformat(skipped["occurred_at"]), due_at)
        self.assertEqual(self.row("SELECT COUNT(*) FROM source_health_events")[0], 1)
        self.assertEqual(self.source_due_at(), due_at + timedelta(days=1))
        self.assertIn(b"Scheduled run skipped because another run is still active", self.admin.get("/sources").data)

    def test_missing_export_retries_three_times_then_reports_failure_and_requeues_same_run(self):
        observed_at = utc_at(2026, 11, 20, 11, 59)
        due_at = utc_at(2026, 11, 20, 12)
        self.configure(observed_at, cadence="daily", timezone="UTC", local_time="12:00")
        run_id = None
        attempt_at = due_at
        for retry_count, wait_seconds in enumerate((60, 120, 240), 1):
            claimed = self.work_at(attempt_at)
            run_id = run_id if run_id is not None else claimed
            self.assertIsNotNone(run_id)
            self.assertEqual(claimed, run_id)
            run = self.row("SELECT * FROM source_runs WHERE id = ?", run_id)
            self.assertEqual((run["trigger"], run["state"], run["attempts"], run["retry_count"]),
                             ("scheduled", "queued", retry_count, retry_count))
            retry_at = attempt_at + timedelta(seconds=wait_seconds)
            self.assertEqual(datetime.fromisoformat(run["retry_after"]), retry_at)
            self.assertIn(b"Retry scheduled for", self.admin.get(f"/sources/runs/{run_id}").data)
            self.assertIsNone(self.work_at(retry_at - timedelta(seconds=1)))
            self.assertEqual(self.row("SELECT COUNT(*) FROM source_health_events")[0], 0)
            attempt_at = retry_at

        self.assertEqual(self.work_at(attempt_at), run_id)
        failed = self.row("SELECT * FROM source_runs WHERE id = ?", run_id)
        self.assertEqual((failed["state"], failed["attempts"], failed["retry_count"], failed["retry_after"]),
                         ("failed", 4, 3, None))
        self.assertIn("FileNotFoundError", failed["error"])
        health = self.row("SELECT kind, run_id, occurred_at FROM source_health_events")
        self.assertEqual((health["kind"], health["run_id"]), ("failed", run_id))
        self.assertEqual(datetime.fromisoformat(health["occurred_at"]), attempt_at)
        self.assertIn(b"Recent source health", self.admin.get("/sources").data)
        self.assertIn(b"Retry run", self.admin.get(f"/sources/runs/{run_id}").data)
        self.assertIsNone(self.work_at(attempt_at + timedelta(minutes=1)))

        self.save_listings(1)
        queued = self.post(self.admin, f"/sources/runs/{run_id}/queue")
        self.assertEqual((queued.status_code, queued.location), (303, f"/sources/runs/{run_id}"))
        self.assertEqual(self.work_at(attempt_at + timedelta(minutes=1)), run_id)
        recovered = self.row("SELECT state, attempts, retry_count FROM source_runs WHERE id = ?", run_id)
        self.assertEqual(tuple(recovered), ("ready", 5, 0))
        self.assertEqual(self.row("SELECT COUNT(*) FROM source_runs")[0], 1)
        self.assertEqual(self.row("SELECT COUNT(*) FROM source_run_rows WHERE run_id = ?", run_id)[0], 1)
        self.assertEqual(self.row("SELECT COUNT(*) FROM source_records")[0], 0)

    def test_reappearing_export_recovers_automatically_and_low_yield_is_visible(self):
        observed_at = utc_at(2026, 11, 20, 11, 59)
        due_at = utc_at(2026, 11, 20, 12)
        self.configure(observed_at, cadence="daily", timezone="UTC", local_time="12:00")
        self.save_listings(5)
        first_run = self.work_at(due_at)
        self.assertEqual(self.row("SELECT state FROM source_runs WHERE id = ?", first_run)["state"], "ready")
        self.saved_export.unlink()
        next_due = due_at + timedelta(days=1)
        second_run = self.work_at(next_due)
        self.assertNotEqual(first_run, second_run)
        retrying = self.row("SELECT state, retry_count FROM source_runs WHERE id = ?", second_run)
        self.assertEqual(tuple(retrying), ("queued", 1))

        self.save_listings(1)
        self.assertEqual(self.work_at(next_due + timedelta(seconds=60)), second_run)
        recovered = self.row("SELECT state, attempts, retry_after FROM source_runs WHERE id = ?", second_run)
        self.assertEqual(tuple(recovered), ("ready", 2, None))
        health = self.row("SELECT kind, run_id, message FROM source_health_events")
        self.assertEqual((health["kind"], health["run_id"]), ("low_yield", second_run))
        self.assertIn("collected 1", health["message"])
        self.assertIn(b"below 30% of its recent average", self.admin.get("/sources").data)


if __name__ == "__main__":
    unittest.main()
