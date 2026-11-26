"""SQLite and PostgreSQL persistence for campaigns and sequencer events."""

import json

from workflow_store import audit, is_blocked, save_suppression_rule, timestamp


SYSTEM_EMAIL = "sequencer-integration@nunify.invalid"
CAMPAIGNS = """
    SELECT c.*, v.name AS view_name FROM campaigns c LEFT JOIN sales_views v ON v.id = c.view_id
"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS sales_views (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    organization_type TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '' CHECK (platform IN ('', 'cvent', 'unknown')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    view_id INTEGER REFERENCES sales_views(id),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_members (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    person_id INTEGER NOT NULL REFERENCES people(id),
    added_by INTEGER NOT NULL REFERENCES users(id),
    added_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused')),
    UNIQUE(campaign_id, person_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_campaign ON campaign_members(person_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS sequence_events (
    id TEXT PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES campaign_members(id),
    event_type TEXT NOT NULL CHECK (event_type IN ('sent', 'opened', 'clicked', 'replied', 'bounced', 'unsubscribed')),
    payload_hash TEXT NOT NULL,
    activity_id INTEGER REFERENCES activities(id),
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sequence_events_member ON sequence_events(member_id, event_type);
"""


def initialize(connection):
    connection.executescript(SCHEMA)


class CampaignStore:
    def __init__(self, connection):
        self.connection = connection

    def views(self):
        return self.connection.execute("SELECT * FROM sales_views ORDER BY name").fetchall()

    def view(self, view_id):
        return self.connection.execute("SELECT * FROM sales_views WHERE id = ?", (view_id,)).fetchone()

    def campaigns(self):
        return self.connection.execute(CAMPAIGNS).fetchall()

    def campaign(self, campaign_id):
        return self.connection.execute(CAMPAIGNS + " WHERE c.id = ?", (campaign_id,)).fetchone()

    def person(self, person_id):
        return self.connection.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()

    def create_view(self, name, account_type, platform, actor_id, occurred_at):
        cursor = self.connection.execute("""
            INSERT INTO sales_views (name, organization_type, platform, created_by, created_at)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT (name) DO NOTHING
        """, (name, account_type, platform, actor_id, timestamp(occurred_at)))
        return cursor.lastrowid if cursor.rowcount == 1 else None

    def create_campaign(self, name, view_id, actor_id, occurred_at):
        cursor = self.connection.execute("""
            INSERT INTO campaigns (name, view_id, created_by, created_at)
            VALUES (?, ?, ?, ?) ON CONFLICT (name) DO NOTHING
        """, (name, view_id, actor_id, timestamp(occurred_at)))
        return cursor.lastrowid if cursor.rowcount == 1 else None

    def has_cvent_evidence(self, account_id):
        return self.connection.execute("""
            SELECT 1 FROM source_records WHERE account_id = ? AND source = 'cvent' LIMIT 1
        """, (account_id,)).fetchone() is not None

    def qualification_signals(self, account_id):
        row = self.connection.execute("SELECT signals FROM qualifications WHERE account_id = ?", (account_id,)).fetchone()
        return json.loads(row["signals"]) if row else []

    def is_blocked(self, account_id, person_id):
        return is_blocked(self.connection, account_id, person_id)

    def members(self, campaign_id):
        return self.connection.execute("""
            SELECT m.*, p.name AS person_name, a.id AS account_id, a.name AS account_name,
                   COUNT(CASE WHEN event.event_type = 'sent' THEN 1 END) AS sent,
                   COUNT(CASE WHEN event.event_type = 'replied' THEN 1 END) AS replies,
                   COUNT(CASE WHEN event.event_type IN ('bounced', 'unsubscribed') THEN 1 END) AS blocked
            FROM campaign_members m JOIN people p ON p.id = m.person_id JOIN accounts a ON a.id = p.account_id
            LEFT JOIN sequence_events event ON event.member_id = m.id
            WHERE m.campaign_id = ? GROUP BY m.id, p.name, a.id, a.name
        """, (campaign_id,)).fetchall()

    def available_people(self, campaign_id):
        return self.connection.execute("""
            SELECT p.* FROM people p WHERE p.unlocked = 1 AND p.email_verified = 1 AND NOT EXISTS (
                SELECT 1 FROM campaign_members m WHERE m.person_id = p.id AND (m.campaign_id = ? OR m.status = 'active')
            ) ORDER BY p.name
        """, (campaign_id,)).fetchall()

    def insert_member(self, campaign_id, person_id, actor_id, occurred_at):
        cursor = self.connection.execute("""
            INSERT INTO campaign_members(campaign_id, person_id, added_by, added_at)
            VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING
        """, (campaign_id, person_id, actor_id, timestamp(occurred_at)))
        return cursor.rowcount == 1

    def membership_exists(self, campaign_id, person_id):
        return self.connection.execute("""
            SELECT 1 FROM campaign_members WHERE campaign_id = ? AND person_id = ?
        """, (campaign_id, person_id)).fetchone() is not None

    def event_hash(self, event_id):
        row = self.connection.execute("SELECT payload_hash FROM sequence_events WHERE id = ?", (event_id,)).fetchone()
        return row["payload_hash"] if row is not None else None

    def event_member(self, member_id):
        return self.connection.execute("""
            SELECT p.id, p.account_id, p.email, p.status, p.do_not_contact, w.stage
            FROM campaign_members m JOIN people p ON p.id = m.person_id
            LEFT JOIN account_work w ON w.account_id = p.account_id WHERE m.id = ?
        """, (member_id,)).fetchone()

    def system_actor(self):
        self.connection.execute("""
            INSERT INTO users(name, email, role, timezone, password_hash, active)
            VALUES ('Sequencer integration', ?, 'sdr', 'UTC', '', 0) ON CONFLICT (email) DO NOTHING
        """, (SYSTEM_EMAIL,))
        return self.connection.execute("SELECT id, active FROM users WHERE email = ?", (SYSTEM_EMAIL,)).fetchone()

    def save_event(self, event_id, member_id, event_type, note, content_hash,
                   occurred_at, received_at, member, actor_id, plan):
        activity_id = None
        if plan.outcome:
            activity_id = self.connection.execute("""
                INSERT INTO activities(account_id, person_id, actor_id, channel, outcome, direction, note, occurred_at)
                VALUES (?, ?, ?, 'email', ?, ?, ?, ?)
            """, (member["account_id"], member["id"], actor_id, plan.outcome,
                  plan.direction, note, timestamp(occurred_at))).lastrowid
        if plan.pause:
            self.connection.execute("UPDATE campaign_members SET status = 'paused' WHERE person_id = ?", (member["id"],))
        if plan.person_status is not None:
            self.connection.execute("""
                UPDATE people SET status = ?, do_not_contact = ? WHERE id = ?
            """, (plan.person_status, 1 if plan.suppression else member["do_not_contact"], member["id"]))
        if plan.stage is not None:
            self.connection.execute("UPDATE account_work SET stage = ? WHERE account_id = ?", (plan.stage, member["account_id"]))
        if plan.suppression and member["email"]:
            self.connection.execute("""
                UPDATE campaign_members SET status = 'paused'
                WHERE person_id IN (SELECT id FROM people WHERE email = ?)
            """, (member["email"],))
            save_suppression_rule(self.connection, "email", member["email"], plan.suppression, actor_id,
                                     received_at, source="sequencer")
        self.connection.execute("""
            INSERT INTO sequence_events(id, member_id, event_type, payload_hash, activity_id, occurred_at, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (event_id, member_id, event_type, content_hash, activity_id,
              timestamp(occurred_at), timestamp(received_at)))

    def audit(self, actor_id, account_id, action, details, occurred_at):
        audit(self.connection, actor_id, account_id, action, details, now=occurred_at)
