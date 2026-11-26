import unittest
from datetime import date, datetime, timezone


NOW = datetime(2026, 11, 21, 20, tzinfo=timezone.utc)
ACCOUNT = {"id": 31, "revision": 2, "owner_id": 7, "owner_timezone": "Asia/Kolkata", "name": "Atlas"}


class MemoryWork:
    def __init__(self):
        self.tasks = []
        self.snoozes = []
        self.completed = []
        self.contacts = []
        self.blocked = None

    def previous_task(self, actor_id, key):
        return next((task for task in self.tasks if task["actor_id"] == actor_id and task["key"] == key), None)

    def person_for(self, account_id, person_id):
        return {"id": person_id} if account_id == 31 and person_id == 10 else None

    def restriction_for(self, account_id, person_id=None, *, candidate_email=None):
        return self.blocked if person_id or candidate_email else None

    def save_task(self, task, occurred_at):
        self.tasks.append(task)

    def complete_task(self, task_id):
        self.completed.append(task_id)

    def snooze_task(self, task_id, due_on):
        self.snoozes.append((task_id, due_on))

    def acknowledge_reply(self, activity_id, actor_id, occurred_at):
        self.acknowledged = (activity_id, actor_id, occurred_at)

    def add_contact(self, account_id, fields, actor_id, occurred_at):
        self.contacts.append(({**fields, "account_id": account_id}, actor_id))
        return 9

    def edit_contact(self, account_id, person_id, fields, changed, actor_id, occurred_at):
        self.contacts.append(({**fields, "account_id": account_id}, actor_id))

    def save_status(self, account_id, contact, status, blocked, actor_id, occurred_at):
        self.status_change = (account_id, contact["id"], status, blocked, actor_id)


class WorkflowCoreTest(unittest.TestCase):
    def test_sales_views_keep_ae_restriction_and_sdr_only_pool(self):
        from crm_core.access import SalesQueueDenied, require_sales_queue, uses_unassigned_pool

        with self.assertRaises(SalesQueueDenied):
            require_sales_queue({"role": "ae"})
        require_sales_queue({"role": "sdr"})
        require_sales_queue({"role": "manager"})
        self.assertTrue(uses_unassigned_pool({"role": "sdr"}, "pool"))
        self.assertFalse(uses_unassigned_pool({"role": "manager"}, "pool"))
        self.assertFalse(uses_unassigned_pool({"role": "sdr"}, "owned"))

    def test_standalone_follow_up_replay_and_owner_timezone(self):
        from crm_core.tasks import TaskConflict, TaskSubmission, create_task, update_task

        store = MemoryWork()
        task = TaskSubmission(8, "Asia/Kolkata", 2, "new", "10", "Send the deck", "2026-11-22")
        self.assertEqual(create_task(store, ACCOUNT, task, NOW), 10)
        self.assertEqual(create_task(store, ACCOUNT, task, NOW), 10)
        self.assertEqual([(row["owner_id"], row["actor_id"]) for row in store.tasks], [(7, 8)])
        with self.assertRaises(TaskConflict):
            create_task(store, ACCOUNT, TaskSubmission(8, "Asia/Kolkata", 2, "new", "", "A different task", "2026-11-22"), NOW)
        update_task(store, {"id": 12, "person_id": 10, "status": "open"}, ACCOUNT, "snooze", "", "UTC", NOW)
        self.assertEqual(store.snoozes, [(12, "2026-11-23")])

    def test_suppression_blocks_snoozing_but_allows_completing(self):
        from crm_core.activity import OutreachBlocked
        from crm_core.tasks import update_task

        store = MemoryWork()
        store.blocked = "Contact marked do not contact"
        task = {"id": 12, "person_id": 10, "status": "open"}
        with self.assertRaisesRegex(OutreachBlocked, "do not contact"):
            update_task(store, task, ACCOUNT, "snooze", "", "UTC", NOW)
        update_task(store, task, ACCOUNT, "complete", "", "UTC", NOW)
        self.assertEqual(store.snoozes, [])
        self.assertEqual(store.completed, [12])

    def test_contact_commands_protect_source_email_and_contact_status(self):
        from crm_core.contacts import ContactConflict, ContactRestricted, add_contact, edit_contact, update_status

        store = MemoryWork()
        self.assertEqual(add_contact(store, ACCOUNT, 7, 2, {"name": "Sam", "linkedin_url": "linkedin.com/in/SAM"}, NOW), 9)
        fields, actor = store.contacts[0]
        self.assertEqual(fields["linkedin_url"], "https://www.linkedin.com/in/sam")
        self.assertEqual(actor, 7)
        contact = dict(fields, id=9, origin="cvent", unlocked=1, email_verified=1, details_revision=0,
                       status="connected", email="saved@example.test")
        with self.assertRaises(ContactConflict):
            edit_contact(store, ACCOUNT, contact, 7, 2, 0, {"email": "new@example.test"}, NOW)
        update_status(store, ACCOUNT, contact, 7, 2, 0, "connected", "do_not_contact", NOW)
        self.assertEqual(store.status_change, (31, 9, "do_not_contact", True, 7))
        store.blocked = "Email opt out"
        with self.assertRaises(ContactRestricted):
            add_contact(store, ACCOUNT, 7, 2, {"name": "Sam", "email": "saved@example.test"}, NOW)
        self.assertEqual(len(store.contacts), 1)

    def test_round_robin_caps_owners_and_protects_preview(self):
        from crm_core.assignment import AssignmentBlocked, round_robin_plan

        reps = [{"id": 8, "account_count": 2}, {"id": 7, "account_count": 1}]
        accounts = [(dict(ACCOUNT, id=i, owner_id=None), 2) for i in (21, 22, 23, 24)]
        groups, unallocated, snapshot = round_robin_plan(reps, accounts, [7, 8], 3, lambda account_id: None)
        self.assertEqual([group["id"] for group in groups], [7, 8])
        self.assertEqual(snapshot, [[7, 1, [21, 23]], [8, 2, [22]]])
        self.assertEqual(unallocated, 1)
        with self.assertRaises(AssignmentBlocked):
            round_robin_plan(reps, accounts, [7, 8], 3, lambda account_id: "Opt out" if account_id == 24 else None)

    def test_contact_inspection_does_not_load_restricted_history(self):
        from crm_core.contacts import contact_history

        class History:
            def contact_timeline(self, account_id, person_id):
                return [(account_id, person_id, "activity")]

            def contact_tasks(self, account_id, person_id):
                return [(account_id, person_id, "task")]

            def contact_evidence(self, account_id, person_id):
                return [(account_id, person_id, "evidence")]

        self.assertEqual(contact_history(object(), 31, 9, "Opt out"), ([], [], []))
        self.assertEqual(contact_history(History(), 31, 9, None),
                         ([(31, 9, "activity")], [(31, 9, "task")], [(31, 9, "evidence")]))

    def test_today_core_applies_coverage_window_and_follow_up_priority(self):
        from crm_core.agenda import queue_items

        class Queue:
            def due_tasks(self, account_id, cutoff):
                return [{"id": 1, "person_id": None, "due_on": cutoff.isoformat(), "title": "Follow up"}]

            def coverage_for(self, account, current_time):
                return {"ends_at": "2026-11-22T00:00:00+05:30"}

            def restriction_for(self, account_id, person_id=None):
                return None

        accounts = [dict(ACCOUNT, relation="Coverage")]
        rows = queue_items(Queue(), accounts, "tomorrow", date(2026, 11, 22), "Asia/Kolkata", NOW, 1, 3)
        self.assertEqual(rows, [])
