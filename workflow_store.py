"""Sales records, account scope, and transaction helpers."""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from werkzeug.security import generate_password_hash

from crm_core.access import account_relation as relation_for_account
from crm_core.account_facts import verified_event_date
from crm_core.contacts import normalized_linkedin_profile
from crm_core.growth import ACCOUNT_STAGES
from crm_core.suppression import matching_rule
from crm_core.suppression_metadata import REASON_CODES, legacy_reason_metadata, rule_reason_text
from organization_identity import matches_domain
from account_identity_schema import initialize_account_identity
from account_redirects import account_aliases
from suppression_projection import ensure_account_projection, refresh_account_suppression, suppression_rules


SUPPRESSION_SEED_DOMAINS = (
    "cvent.com", "cventevents.com", "cvent-production.com", "*.cventevents.com",
)
SUPPRESSION_REASON_CHECK = "CONSTRAINT suppression_reason_valid CHECK (reason IN (" + ", ".join(
    f"'{code}'" for code in REASON_CODES
) + "))"
SUPPRESSION_METADATA_COLUMNS = {
    "source": "TEXT NOT NULL DEFAULT 'manual'",
    "reason_note": "TEXT NOT NULL DEFAULT ''",
    "removed_at": "TEXT",
}
SUPPRESSION_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS suppression (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('domain', 'email', 'account')),
    value TEXT NOT NULL COLLATE NOCASE,
    reason TEXT NOT NULL,
    source {SUPPRESSION_METADATA_COLUMNS["source"]},
    reason_note {SUPPRESSION_METADATA_COLUMNS["reason_note"]},
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    removed_at {SUPPRESSION_METADATA_COLUMNS["removed_at"]},
    {SUPPRESSION_REASON_CHECK},
    UNIQUE (kind, value)
);
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role TEXT NOT NULL CHECK (role IN ('admin', 'manager', 'sdr', 'ae')),
    timezone TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    daily_credit_quota INTEGER NOT NULL DEFAULT 25 CHECK (daily_credit_quota >= 0)
);
CREATE TABLE IF NOT EXISTS account_work (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    owner_id INTEGER REFERENCES users(id),
    stage TEXT NOT NULL DEFAULT 'discovered',
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    assigned_at TEXT
);
CREATE INDEX IF NOT EXISTS account_work_owner ON account_work(owner_id);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    status TEXT NOT NULL DEFAULT 'not_contacted',
    do_not_contact INTEGER NOT NULL DEFAULT 0 CHECK (do_not_contact IN (0, 1)),
    persona TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'manual',
    unlocked INTEGER NOT NULL DEFAULT 0 CHECK (unlocked IN (0, 1)),
    email_verified INTEGER NOT NULL DEFAULT 0 CHECK (email_verified IN (0, 1)),
    phone TEXT NOT NULL DEFAULT '',
    linkedin_url TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    details_revision INTEGER NOT NULL DEFAULT 0 CHECK (details_revision >= 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS people_account_email ON people(account_id, email) WHERE email != '';
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    person_id INTEGER REFERENCES people(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    channel TEXT NOT NULL,
    outcome TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound', 'internal')),
    note TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    request_key TEXT,
    request_hash TEXT,
    acknowledged_at TEXT,
    acknowledged_by INTEGER REFERENCES users(id),
    UNIQUE (actor_id, request_key)
);
CREATE INDEX IF NOT EXISTS activities_account ON activities(account_id, occurred_at);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    person_id INTEGER REFERENCES people(id),
    owner_id INTEGER NOT NULL REFERENCES users(id),
    activity_id INTEGER UNIQUE REFERENCES activities(id),
    title TEXT NOT NULL,
    due_on TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'snoozed')),
    created_by INTEGER REFERENCES users(id),
    request_key TEXT,
    request_hash TEXT
);
CREATE INDEX IF NOT EXISTS tasks_owner_due ON tasks(owner_id, due_on);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    account_id INTEGER REFERENCES accounts(id),
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_events_account_name_change ON audit_events(account_id)
    WHERE action = 'account_name_changed';
{SUPPRESSION_SCHEMA}
CREATE TABLE IF NOT EXISTS workflow_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

ACCOUNT_QUERY = """
    SELECT a.*, COALESCE(w.stage, 'discovered') AS stage, w.owner_id,
           w.assigned_at, COALESCE(w.revision, 0) AS revision,
           u.name AS owner_name, u.timezone AS owner_timezone,
           (SELECT MIN(start_date_iso) FROM cvent_read e
            WHERE e.company_id = a.id AND e.start_date_iso >= ?) AS next_event
    FROM accounts a
    LEFT JOIN account_work w ON w.account_id = a.id
    LEFT JOIN users u ON u.id = w.owner_id
    WHERE NOT EXISTS (SELECT 1 FROM account_merges WHERE from_id = a.id)
"""

DEMO_PASSWORD = "nunify-demo"
DEMO_USERS = (
    ("Morgan Manager", "manager@example.test", "manager", "Asia/Kolkata"),
    ("Riya Rao", "riya@example.test", "sdr", "Asia/Kolkata"),
    ("Jordan Lee", "jordan@example.test", "sdr", "America/New_York"),
    ("Alex AE", "ae@example.test", "ae", "America/New_York"),
    ("Avery Admin", "admin@example.test", "admin", "Asia/Kolkata"),
)


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(now=None):
    return (now or utcnow()).astimezone(timezone.utc).isoformat(timespec="microseconds")


def local_date(user, now=None):
    return (now or utcnow()).astimezone(ZoneInfo(user["timezone"])).date()


@contextmanager
def transaction(connection):
    connection.execute("BEGIN IMMEDIATE")
    with connection:
        yield


def _legacy_suppression_row(rule, columns):
    reason, original = legacy_reason_metadata(rule["reason"])
    note = (rule["reason_note"] if "reason_note" in columns else "") or original
    is_seed = (
        rule["created_by"] is None and rule["kind"] == "domain" and reason == "competitor"
        and rule["value"].lower() in SUPPRESSION_SEED_DOMAINS
    )
    source = (rule["source"] if "source" in columns else "") or ("seed" if is_seed else "legacy")
    removed_at = rule["removed_at"] if "removed_at" in columns else None
    return (rule["id"], rule["kind"], rule["value"], reason, source, note,
            rule["created_by"], rule["created_at"], removed_at)


def _migrate_suppression_metadata(connection):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(suppression)")}
    missing = SUPPRESSION_METADATA_COLUMNS.keys() - columns
    if not missing:
        return False

    rows = [_legacy_suppression_row(rule, columns) for rule in connection.execute("SELECT * FROM suppression")]
    if getattr(connection, "dialect", "sqlite") == "postgresql":
        for column, definition in SUPPRESSION_METADATA_COLUMNS.items():
            if column in missing:
                connection.execute(f"ALTER TABLE suppression ADD COLUMN {column} {definition}")
        connection.executemany("""
            UPDATE suppression SET reason = ?, source = ?, reason_note = ? WHERE id = ?
        """, [(row[3], row[4], row[5], row[0]) for row in rows])
        connection.execute(f"ALTER TABLE suppression ADD {SUPPRESSION_REASON_CHECK}")
    else:
        connection.execute("ALTER TABLE suppression RENAME TO suppression_pre_r101")
        connection.execute(SUPPRESSION_SCHEMA)
        connection.executemany("""
            INSERT INTO suppression(id, kind, value, reason, source, reason_note, created_by, created_at, removed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        connection.execute("DROP TABLE suppression_pre_r101")
    return True


def _install_account_stage_guards(connection):
    installed = connection.execute("""
        INSERT OR IGNORE INTO workflow_settings(key, value) VALUES ('account_stage_guards_v1', '1')
    """)
    if not installed.rowcount:
        return
    stages = ", ".join(f"'{stage}'" for stage in ACCOUNT_STAGES)
    # Allow an existing legacy stage to follow its work record through a merge or reassignment.
    recognized = f"""NEW.stage IN ({stages})
        OR EXISTS (SELECT 1 FROM account_work WHERE stage = NEW.stage)"""
    if getattr(connection, "dialect", "sqlite") == "postgresql":
        connection.execute(f"""
            CREATE OR REPLACE FUNCTION check_account_work_stage() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NOT ({recognized}) THEN
                    RAISE EXCEPTION 'Unsupported account stage' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$
        """)
        connection.execute("""
            CREATE TRIGGER account_work_stage_guard
            BEFORE INSERT OR UPDATE OF stage ON account_work
            FOR EACH ROW EXECUTE FUNCTION check_account_work_stage()
        """)
    else:
        for name, event in (("insert", "INSERT"), ("update", "UPDATE OF stage")):
            connection.execute(f"""
                CREATE TRIGGER account_work_stage_guard_{name} BEFORE {event} ON account_work
                WHEN NOT ({recognized})
                BEGIN SELECT RAISE(ABORT, 'Unsupported account stage'); END
            """)


def initialize(connection):
    connection.executescript(SCHEMA)
    with transaction(connection):
        _install_account_stage_guards(connection)
        if not connection.execute("PRAGMA table_info(account_merges)").fetchone():
            initialize_account_identity(connection)
        columns_to_add = {
            "people": {
                "phone": "TEXT NOT NULL DEFAULT ''",
                "linkedin_url": "TEXT NOT NULL DEFAULT '' COLLATE NOCASE",
                "details_revision": "INTEGER NOT NULL DEFAULT 0 CHECK (details_revision >= 0)",
            },
            "tasks": {
                "created_by": "INTEGER REFERENCES users(id)",
                "request_key": "TEXT",
                "request_hash": "TEXT",
            },
        }
        for table, columns in columns_to_add.items():
            existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS people_account_linkedin
            ON people(account_id, linkedin_url) WHERE linkedin_url != ''
        """)
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS tasks_creation_request
            ON tasks(created_by, request_key) WHERE request_key IS NOT NULL
        """)
        metadata_changed = _migrate_suppression_metadata(connection)
        seeded = connection.execute("""
            INSERT OR IGNORE INTO workflow_settings(key, value) VALUES ('suppression_seed', '1')
        """)
        if seeded.rowcount:
            connection.executemany("""
                INSERT OR IGNORE INTO suppression(kind, value, reason, source, created_at)
                VALUES ('domain', ?, 'competitor', 'seed', ?)
            """, [(domain, timestamp()) for domain in SUPPRESSION_SEED_DOMAINS])
        normalized_hosts = connection.execute("""
            INSERT OR IGNORE INTO workflow_settings(key, value) VALUES ('suppression_idn_hosts', '1')
        """)
        company_domains = connection.execute("""
            INSERT OR IGNORE INTO workflow_settings(key, value) VALUES ('suppression_company_domains', '1')
        """)
        projection_changed = ensure_account_projection(connection)
        if projection_changed or metadata_changed or seeded.rowcount or normalized_hosts.rowcount or company_domains.rowcount:
            refresh_account_suppression(connection)


def seed_demo_users(connection):
    with transaction(connection):
        for name, email, role, zone in DEMO_USERS:
            if not connection.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                connection.execute(
                    "INSERT INTO users(name, email, role, timezone, password_hash) VALUES (?, ?, ?, ?, ?)",
                    (name, email, role, zone, generate_password_hash(DEMO_PASSWORD)),
                )


def account_rows(connection, today, account_id=None):
    if account_id is None:
        rows = connection.execute(ACCOUNT_QUERY + " ORDER BY a.id", (today,))
        return [_account_for_day(connection, row, today) for row in rows]
    row = connection.execute(ACCOUNT_QUERY + " AND a.id = ?", (today, account_id)).fetchone()
    return _account_for_day(connection, row, today) if row else None


def _account_for_day(connection, row, today):
    account = dict(row)
    if account.get("facts_as_of") == str(today):
        account["next_event"] = account["next_event_date"]
    elif account["next_event"] is not None and verified_event_date(account["next_event"]) is None:
        dates = connection.execute("""
            SELECT start_date_iso FROM cvent_read
            WHERE company_id = ? AND start_date_iso >= ? ORDER BY start_date_iso
        """, (account["id"], today))
        account["next_event"] = next((item[0] for item in dates if verified_event_date(item[0])), None)
    return account


def active_coverage(connection, account, now=None):
    at = timestamp(now)
    return connection.execute("""
        SELECT c.*, u.name AS rep_name FROM coverages c JOIN users u ON u.id = c.rep_id
        WHERE c.account_id = ? AND c.owner_id = ? AND c.cancelled_at IS NULL
          AND c.starts_at <= ? AND c.ends_at > ? AND u.active = 1
        ORDER BY c.starts_at LIMIT 1
    """, (account["id"], account["owner_id"], at, at)).fetchone()


def account_relation(connection, account, user, now=None):
    return relation_for_account(account, user, lambda row: active_coverage(connection, row, now))


def save_suppression_rule(connection, kind, value, reason, actor_id, observed_at, *,
                          source="manual", reason_note="", refresh=True):
    result = connection.execute("""
        INSERT INTO suppression(kind, value, reason, source, reason_note, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (kind, value) DO UPDATE SET reason = excluded.reason,
            source = excluded.source, reason_note = excluded.reason_note,
            created_by = excluded.created_by, created_at = excluded.created_at, removed_at = NULL
        WHERE suppression.removed_at IS NOT NULL
    """, (kind, value, reason, source, reason_note, actor_id, timestamp(observed_at)))
    if not result.rowcount:
        return None
    if refresh and kind in ("domain", "account"):
        refresh_account_suppression(connection)
    return connection.execute("SELECT id FROM suppression WHERE kind = ? AND value = ?", (kind, value)).fetchone()[0]


def suppression_reason(connection, account_id, person_id=None, *, candidate_email=None):
    account = connection.execute("SELECT domain FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        return None
    person = connection.execute(
        "SELECT email, do_not_contact FROM people WHERE id = ? AND account_id = ?", (person_id, account_id),
    ).fetchone() if person_id is not None else None
    if person and person["do_not_contact"]:
        return "Contact marked do not contact"
    emails = (person["email"] if person else "", candidate_email)
    aliases = account_aliases(connection, (account_id,)).get(account_id, ())
    rule = matching_rule(suppression_rules(connection), account_id, account["domain"], emails, aliases=aliases)
    return rule_reason_text(rule) if rule is not None else None


def is_blocked(connection, account_id, person_id=None, *, candidate_email=None):
    return bool(suppression_reason(connection, account_id, person_id, candidate_email=candidate_email))


def audit(connection, user_id, account_id, action, details, now=None):
    connection.execute("""
        INSERT INTO audit_events(actor_id, account_id, action, details, created_at) VALUES (?, ?, ?, ?, ?)
    """, (user_id, account_id, action, json.dumps(details, sort_keys=True), timestamp(now)))
