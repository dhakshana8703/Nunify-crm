import unittest

from crm_core.pilot import PilotSignIn, provision_pilot_users


class PilotMemory:
    def __init__(self, rows=()):
        self.rows = rows
        self.changes = []

    def find(self, email, legacy_email):
        return [row for row in self.rows if row["email"] in (email, legacy_email)]

    def add(self, sign_in, password_hash, timezone, quota):
        self.changes.append(("new", sign_in.email, password_hash, timezone, quota))

    def update(self, user_id, email, password_hash):
        self.changes.append(("update", user_id, email, password_hash))


class PilotCoreTest(unittest.TestCase):
    def test_hb04_provisioning_keeps_existing_user_identity_when_sign_in_changes(self):
        sign_in = PilotSignIn("Pilot Rep", "rep@example.test", "old@example.test", "sdr", "PILOT_PASSWORD", "new")
        store = PilotMemory([{"id": 21, "email": "old@example.test", "role": "sdr", "password_hash": "hash:old"}])
        provision_pilot_users(store, [sign_in], lambda digest, value: digest == "hash:" + value,
                              lambda value: "hash:" + value)
        self.assertEqual(store.changes, [("update", 21, "rep@example.test", "hash:new")])
