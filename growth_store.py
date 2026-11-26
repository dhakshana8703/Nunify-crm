"""SQL adapters for qualification, outreach restrictions and enrichment."""

import json

from account_facts_store import refresh_account_facts
from crm_core.growth import DEFAULT_RULES, local_day
from suppression_projection import refresh_account_suppression
from workflow_store import (
    account_relation, account_rows, audit, save_suppression_rule, suppression_reason, suppression_rules,
    timestamp, transaction,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS qualifications (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    score INTEGER NOT NULL,
    signals TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    override INTEGER CHECK (override IN (0, 1))
);
CREATE TABLE IF NOT EXISTS qualification_jobs (
    id INTEGER PRIMARY KEY,
    requested_by INTEGER NOT NULL REFERENCES users(id),
    scope TEXT NOT NULL CHECK (scope IN ('all', 'cvent', 'directory', 'both')),
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'failed', 'done')),
    policy_json TEXT NOT NULL,
    planning_start INTEGER NOT NULL,
    planning_end INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    observed_day TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    last_account_id INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    scored INTEGER NOT NULL DEFAULT 0,
    qualified INTEGER NOT NULL DEFAULT 0,
    suppressed INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS qualification_jobs_one_active
    ON qualification_jobs((1)) WHERE state IN ('queued', 'running');
CREATE TABLE IF NOT EXISTS qualification_job_accounts (
    job_id INTEGER NOT NULL REFERENCES qualification_jobs(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    PRIMARY KEY (job_id, account_id)
);
CREATE TABLE IF NOT EXISTS credit_jobs (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    person_id INTEGER REFERENCES people(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL CHECK (action IN ('people_search', 'email_unlock')),
    state TEXT NOT NULL CHECK (state IN ('queued', 'processing', 'failed', 'done', 'blocked')),
    on_day TEXT NOT NULL,
    budget_day TEXT NOT NULL,
    request_key TEXT NOT NULL UNIQUE,
    auto INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    lease_until TEXT,
    lease_token TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS credit_jobs_open
ON credit_jobs(account_id, action, COALESCE(person_id, 0)) WHERE state IN ('queued', 'processing', 'failed');
CREATE TABLE IF NOT EXISTS credit_ledger (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE REFERENCES credit_jobs(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    person_id INTEGER REFERENCES people(id),
    action TEXT NOT NULL,
    credits INTEGER NOT NULL CHECK (credits > 0),
    on_day TEXT NOT NULL,
    at TEXT NOT NULL
);
"""


def initialize(connection):
    connection.executescript(SCHEMA)
    with transaction(connection):
        fields = {row["name"] for row in connection.execute("PRAGMA table_info(credit_jobs)")}
        if "budget_day" not in fields:
            connection.execute("ALTER TABLE credit_jobs ADD COLUMN budget_day TEXT NOT NULL DEFAULT ''")
            connection.execute("UPDATE credit_jobs SET budget_day = substr(created_at, 1, 10)")


class SqlGrowthStore:
    def __init__(self, connection):
        self.connection = connection

    def atomic(self):
        return transaction(self.connection)

    def rules(self):
        row = self.connection.execute("SELECT value FROM workflow_settings WHERE key = 'qualification'").fetchone()
        return json.loads(row["value"]) if row else DEFAULT_RULES

    def save_rules(self, config):
        self.connection.execute("""
            INSERT INTO workflow_settings(key, value) VALUES ('qualification', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (json.dumps(config),))

    def evidence(self, account_id):
        rows = self.connection.execute("""
            SELECT source, source_url, details_json FROM source_records WHERE account_id = ? ORDER BY id
        """, (account_id,))
        return [dict(row, details=json.loads(row["details_json"])) for row in rows]

    def save_qualification(self, account_id, score, signals, observed):
        self.connection.execute("""
            INSERT INTO qualifications(account_id, score, signals, observed_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET score = excluded.score,
                signals = excluded.signals, observed_at = excluded.observed_at
        """, (account_id, score, json.dumps(signals), timestamp(observed)))
        refresh_account_facts(self.connection, (account_id,))

    def work_state(self, account_id):
        return self.connection.execute("SELECT owner_id, stage, revision FROM account_work WHERE account_id = ?",
                                       (account_id,)).fetchone()

    def apply_stage(self, account_id, stage, expected_revision):
        self.connection.execute("""
            INSERT INTO account_work(account_id, stage, revision) VALUES (?, ?, 1)
            ON CONFLICT(account_id) DO UPDATE SET stage = excluded.stage, revision = account_work.revision + 1
            WHERE account_work.revision = ?
        """, (account_id, stage, expected_revision))

    def qualification(self, account_id):
        return self.connection.execute("SELECT * FROM qualifications WHERE account_id = ?", (account_id,)).fetchone()

    def active_qualification_job(self):
        job = self.connection.execute("SELECT * FROM qualification_jobs WHERE state IN ('queued', 'running')").fetchone()
        return dict(job, config=json.loads(job["policy_json"])) if job is not None else None

    def create_qualification_job(self, user_id, scope, config, observed, day, planning):
        return self.connection.execute("""
            INSERT INTO qualification_jobs(requested_by, scope, policy_json, planning_start, planning_end,
                                           observed_at, observed_day)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, scope, json.dumps(config), *planning, timestamp(observed), day.isoformat())).lastrowid

    def qualification_candidates(self):
        return self.connection.execute("""
            SELECT r.account_id, q.score, q.override, w.owner_id, COALESCE(w.stage, 'discovered') AS stage,
                   COUNT(CASE WHEN r.source = 'cvent' THEN 1 END) AS has_cvent,
                   COUNT(CASE WHEN r.source = 'directory' THEN 1 END) AS has_directory
            FROM source_records r
            LEFT JOIN qualifications q ON q.account_id = r.account_id
            LEFT JOIN account_work w ON w.account_id = r.account_id
            WHERE r.account_id IS NOT NULL
            GROUP BY r.account_id, q.score, q.override, w.owner_id, w.stage
        """)

    def save_qualification_targets(self, job_id, account_ids):
        self.connection.execute("""
            INSERT INTO qualification_job_accounts(job_id, account_id)
            SELECT ?, value FROM (SELECT value FROM json_each(?)) AS selected
        """, (job_id, json.dumps(account_ids)))
        self.connection.execute("UPDATE qualification_jobs SET total = ? WHERE id = ?", (len(account_ids), job_id))

    def qualification_status(self, job_id):
        return self.connection.execute("SELECT state FROM qualification_jobs WHERE id = ?", (job_id,)).fetchone()

    def requeue_qualification_job(self, job_id):
        self.connection.execute("UPDATE qualification_jobs SET state = 'queued', error = NULL WHERE id = ?", (job_id,))

    def qualification_targets(self, job, limit):
        return [row["account_id"] for row in self.connection.execute("""
            SELECT account_id FROM qualification_job_accounts
            WHERE job_id = ? AND account_id > ? ORDER BY account_id LIMIT ?
        """, (job["id"], job["last_account_id"], limit))]

    def finish_qualification_batch(self, job, *, status, processed, cursor, scored, qualified, suppressed, finished_at):
        self.connection.execute("""
            UPDATE qualification_jobs
            SET state = ?, processed = ?, last_account_id = ?, scored = scored + ?,
                qualified = qualified + ?, suppressed = suppressed + ?, finished_at = ?
            WHERE id = ?
        """, (status, processed, cursor, scored, qualified, suppressed,
              timestamp(finished_at) if finished_at is not None else None, job["id"]))

    def fail_qualification_batch(self, job, message):
        self.connection.execute("""
            UPDATE qualification_jobs SET state = 'failed', error = ?
            WHERE id = ? AND last_account_id = ? AND state IN ('queued', 'running')
        """, (message, job["id"], job["last_account_id"]))

    def account(self, account_id, day):
        return account_rows(self.connection, day, account_id)

    def account_relation(self, account, actor, observed):
        return account_relation(self.connection, account, actor, observed)

    def restriction_for(self, account_id, person_id=None, *, candidate_email=None):
        return suppression_reason(self.connection, account_id, person_id, candidate_email=candidate_email)

    def person(self, account_id, person_id):
        return self.connection.execute("SELECT * FROM people WHERE id = ? AND account_id = ?",
                                       (person_id, account_id)).fetchone()

    def has_credit_job(self, account_id, action, person_id):
        return self.connection.execute("""
            SELECT 1 FROM credit_jobs j WHERE account_id = ? AND action = ?
              AND (person_id = ? OR (person_id IS NULL AND CAST(? AS INTEGER) IS NULL))
              AND (state IN ('queued', 'processing', 'failed', 'done')
                   OR EXISTS (SELECT 1 FROM credit_ledger l WHERE l.job_id = j.id))
        """, (account_id, action, person_id, person_id)).fetchone() is not None

    def daily_quota(self, user_id):
        return self.connection.execute("SELECT daily_credit_quota FROM users WHERE id = ?", (user_id,)).fetchone()[0]

    def replace_daily_quota(self, user_id, quota):
        user = self.connection.execute("SELECT daily_credit_quota FROM users WHERE id = ? AND role = 'sdr'",
                                      (user_id,)).fetchone()
        if user is None:
            return None
        self.connection.execute("UPDATE users SET daily_credit_quota = ? WHERE id = ?", (quota, user_id))
        return user["daily_credit_quota"]

    def credit_usage(self, user_id, day):
        return self.connection.execute("""
            SELECT COALESCE((SELECT SUM(credits) FROM credit_ledger WHERE user_id = ? AND on_day = ?), 0)
                 + (SELECT COUNT(*) FROM credit_jobs WHERE actor_id = ? AND on_day = ?
                      AND state IN ('queued', 'processing', 'failed'))
        """, (user_id, day, user_id, day)).fetchone()[0]

    def auto_usage(self, day):
        return self.connection.execute("""
            SELECT COUNT(*) FROM credit_jobs j WHERE auto = 1 AND budget_day = ?
              AND (state != 'blocked' OR EXISTS (SELECT 1 FROM credit_ledger l WHERE l.job_id = j.id))
        """, (day,)).fetchone()[0]

    def reserve_credit_job(self, job):
        self.connection.execute("""
            INSERT INTO credit_jobs(account_id, person_id, actor_id, action, state,
                                    on_day, budget_day, request_key, auto, created_at)
            VALUES (:account_id, :person_id, :actor_id, :action, 'queued',
                    :on_day, :budget_day, :request_key, :auto, :created_at)
        """, dict(job, auto=int(job["auto"]), created_at=timestamp(job["observed"])))

    def claimable_credit_jobs(self, observed):
        return self.connection.execute("""
            SELECT j.*, a.organization_type, u.role, u.timezone, u.active, u.daily_credit_quota
            FROM credit_jobs j JOIN accounts a ON a.id = j.account_id JOIN users u ON u.id = j.actor_id
            WHERE j.state = 'queued' OR (j.state = 'processing' AND j.lease_until <= ?)
            ORDER BY j.id
        """, (timestamp(observed),))

    def hold_credit_job(self, job_id, reason, *, release=False):
        if release:
            self.connection.execute("""
                UPDATE credit_jobs SET state = 'blocked', error = ?, lease_token = NULL, lease_until = NULL WHERE id = ?
            """, (reason, job_id))
        else:
            self.connection.execute("UPDATE credit_jobs SET state = 'blocked', error = ? WHERE id = ?", (reason, job_id))

    def claim_credit_job(self, job, token, expires_at):
        self.connection.execute("""
            UPDATE credit_jobs SET state = 'processing', lease_token = ?, lease_until = ?,
                on_day = ?, budget_day = ?, attempts = attempts + 1
            WHERE id = ?
        """, (token, timestamp(expires_at), job["on_day"], job["budget_day"], job["id"]))

    def owns_credit_job(self, job_id, token):
        return self.connection.execute("""
            SELECT 1 FROM credit_jobs WHERE id = ? AND state = 'processing' AND lease_token = ?
        """, (job_id, token)).fetchone() is not None

    def charge_credit(self, job, observed):
        self.connection.execute("""
            INSERT INTO credit_ledger(job_id, user_id, account_id, person_id, action, credits, on_day, at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        """, (job["id"], job["actor_id"], job["account_id"], job["person_id"], job["action"],
              job["on_day"], timestamp(observed)))

    def save_unlocked_email(self, person_id, email):
        self.connection.execute("UPDATE people SET email = ?, unlocked = 1, email_verified = 1 WHERE id = ?",
                                (email, person_id))

    def save_enriched_contact(self, account_id, name, title):
        self.connection.execute("""
            INSERT INTO people(account_id, name, title, persona, origin) VALUES (?, ?, ?, 'P1', 'apollo_offline')
        """, (account_id, name, title))

    def complete_credit_job(self, job_id):
        self.connection.execute("UPDATE credit_jobs SET state = 'done', error = NULL WHERE id = ?", (job_id,))

    def fail_credit_job(self, job_id, token, reason):
        self.connection.execute("""
            UPDATE credit_jobs SET state = 'failed', error = ?
            WHERE id = ? AND state = 'processing' AND lease_token = ?
        """, (reason, job_id, token))

    def credit_job(self, job_id):
        return self.connection.execute("SELECT * FROM credit_jobs WHERE id = ?", (job_id,)).fetchone()

    def retry_credit_job(self, job_id):
        self.connection.execute("UPDATE credit_jobs SET state = 'queued', lease_token = NULL, lease_until = NULL WHERE id = ?",
                                (job_id,))

    def account_exists(self, account_id):
        return self.connection.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone() is not None

    def save_rule(self, kind, value, reason, actor_id, observed, *, source, reason_note):
        return save_suppression_rule(self.connection, kind, value, reason, actor_id, observed,
                                     source=source, reason_note=reason_note, refresh=False)

    def remove_rule(self, rule_id, observed):
        return self.connection.execute("""
            UPDATE suppression SET removed_at = ? WHERE id = ? AND removed_at IS NULL
            RETURNING kind, value, reason, source, reason_note, created_by, created_at
        """, (timestamp(observed), rule_id)).fetchone()

    def refresh_suppressed_accounts(self):
        refresh_account_suppression(self.connection)

    def rules_for_display(self):
        return suppression_rules(self.connection)

    def audit(self, actor_id, account_id, action, details, observed):
        audit(self.connection, actor_id, account_id, action, details, observed)

    def account_jobs(self, account_id):
        return self.connection.execute("SELECT * FROM credit_jobs WHERE account_id = ? ORDER BY id DESC LIMIT 10",
                                       (account_id,)).fetchall()

    def workload(self, observed):
        result = []
        for user in self.connection.execute("""
            SELECT u.id, u.name, u.timezone, u.daily_credit_quota,
                   (SELECT COUNT(*) FROM account_work w WHERE w.owner_id = u.id
                    AND NOT EXISTS (SELECT 1 FROM account_merges WHERE from_id = w.account_id)) AS accounts
            FROM users u WHERE u.role = 'sdr' AND u.active = 1 ORDER BY u.name
        """):
            day = local_day(user, observed).isoformat()
            counts = self.connection.execute("""
                SELECT COUNT(*) AS open_count, COUNT(CASE WHEN due_on < ? THEN 1 END) AS overdue
                FROM tasks WHERE owner_id = ? AND status != 'done'
            """, (day, user["id"])).fetchone()
            result.append(dict(user, **dict(counts)))
        return result

    def jobs_for_display(self):
        return [dict(row) for row in self.connection.execute("""
            SELECT j.*, a.name, u.name AS actor FROM credit_jobs j JOIN accounts a ON a.id = j.account_id
            JOIN users u ON u.id = j.actor_id
        """)]

    def total_credits(self):
        return self.connection.execute("SELECT COALESCE(SUM(credits), 0) FROM credit_ledger").fetchone()[0]

    def coverage(self, day):
        return self.connection.execute("""
            SELECT (SELECT COUNT(*) FROM accounts a
                    WHERE NOT EXISTS (SELECT 1 FROM account_merges WHERE from_id = a.id)) AS accounts,
                   (SELECT COUNT(*) FROM account_work w WHERE w.owner_id IS NOT NULL
                    AND NOT EXISTS (SELECT 1 FROM account_merges WHERE from_id = w.account_id)) AS assigned,
                   (SELECT COUNT(DISTINCT account_id) FROM people
                    WHERE origin NOT IN ('demo', 'apollo_offline')) AS accounts_with_contacts,
                   (SELECT COUNT(DISTINCT company_id) FROM cvent_read WHERE start_date_iso >= ?) AS upcoming
        """, (day,)).fetchone()

    def qualification_jobs(self):
        return self.connection.execute("""
            SELECT j.*, u.name AS requested_by_name FROM qualification_jobs j
            JOIN users u ON u.id = j.requested_by ORDER BY j.id DESC LIMIT 10
        """).fetchall()

    def source_funnel(self):
        return self.connection.execute("""
            WITH evidence AS (
                SELECT source AS source_name, account_id FROM source_records WHERE account_id IS NOT NULL
                UNION
                SELECT source_slug AS source_name, account_id FROM imported_source_evidence
            )
            SELECT source_name, COUNT(*) AS discovered,
                   COUNT(CASE WHEN EXISTS(SELECT 1 FROM qualifications q WHERE q.account_id = e.account_id)
                              THEN 1 END) AS scored,
                   COUNT(CASE WHEN EXISTS(SELECT 1 FROM account_work w
                                          WHERE w.account_id = e.account_id AND w.owner_id IS NOT NULL)
                              THEN 1 END) AS assigned,
                   COUNT(CASE WHEN EXISTS(SELECT 1 FROM activities a WHERE a.account_id = e.account_id)
                              THEN 1 END) AS worked,
                   COUNT(CASE WHEN EXISTS(SELECT 1 FROM handoffs h WHERE h.account_id = e.account_id)
                              THEN 1 END) AS positive,
                   COUNT(CASE WHEN EXISTS(SELECT 1 FROM handoffs h
                                          WHERE h.account_id = e.account_id AND h.status = 'delivered')
                              THEN 1 END) AS delivered,
                   SUM(COALESCE((SELECT SUM(credits) FROM credit_ledger l WHERE l.account_id = e.account_id), 0)) AS credits
            FROM evidence e GROUP BY source_name ORDER BY source_name
        """).fetchall()


class OfflineApollo:
    def __call__(self, job, idempotency_key):
        if job["action"] == "email_unlock":
            return {"email": f"apollo-{job['person_id']}@example.test"}
        return {"name": "Apollo Demo Contact", "title": job["title"]}
