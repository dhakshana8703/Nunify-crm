"""Sales records, account scope, and transaction helpers."""

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo

from werkzeug.security import generate_password_hash


SCHEMA = """
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
CREATE TABLE IF NOT EXISTS suppression (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('domain', 'email', 'account')),
    value TEXT NOT NULL COLLATE NOCASE,
    reason TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (kind, value)
);
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


def initialize(connection):
    connection.executescript(SCHEMA)
    with transaction(connection):
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
        seeded = connection.execute("""
            INSERT OR IGNORE INTO workflow_settings(key, value) VALUES ('suppression_seed', '1')
        """)
        if seeded.rowcount:
            connection.executemany("""
                INSERT OR IGNORE INTO suppression(kind, value, reason, created_at)
                VALUES ('domain', ?, 'Competitor', ?)
            """, [(domain, timestamp()) for domain in (
                "cvent.com", "cventevents.com", "cvent-production.com", "*.cventevents.com",
            )])


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
        return [dict(row) for row in connection.execute(ACCOUNT_QUERY + " ORDER BY a.id", (today,))]
    row = connection.execute(ACCOUNT_QUERY + " WHERE a.id = ?", (today, account_id)).fetchone()
    return dict(row) if row else None


def active_coverage(connection, account, now=None):
    at = timestamp(now)
    return connection.execute("""
        SELECT c.*, u.name AS rep_name FROM coverages c JOIN users u ON u.id = c.rep_id
        WHERE c.account_id = ? AND c.owner_id = ? AND c.cancelled_at IS NULL
          AND c.starts_at <= ? AND c.ends_at > ? AND u.active = 1
        ORDER BY c.starts_at LIMIT 1
    """, (account["id"], account["owner_id"], at, at)).fetchone()


def account_relation(connection, account, user, now=None):
    if account["owner_id"] == user["id"]:
        return "Owned"
    if user["role"] == "sdr" and account["owner_id"]:
        grant = active_coverage(connection, account, now)
        if grant and grant["rep_id"] == user["id"]:
            return "Coverage"
    return "Assigned" if account["owner_id"] else "Unassigned"


def matches_domain(host, rule):
    host = host.casefold().strip().rstrip(".")
    rule = rule.casefold().strip().rstrip(".").removeprefix("*.")
    return bool(rule) and (host == rule or host.endswith("." + rule))


def normalized_linkedin_profile(value):
    if not value:
        return ""
    try:
        url = urlsplit(value if "://" in value else "https://" + value)
        host = url.hostname or ""
        path = unquote(url.path, errors="strict")
        profile = re.fullmatch(r"/in/([\w-]+)/?", path)
        if (url.scheme in {"http", "https"} and not url.username and not url.password
                and url.port in {None, 80 if url.scheme == "http" else 443}
                and re.fullmatch(r"(?:www\.|[a-z]{2}\.)?linkedin\.com", host) and profile):
            return "https://www.linkedin.com/in/" + quote(profile[1].casefold(), safe="-")
    except (ValueError, UnicodeError):
        pass
    return None


def suppression_reason(connection, account_id, person_id=None):
    account = connection.execute("SELECT domain FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        return None
    person = connection.execute(
        "SELECT email, do_not_contact FROM people WHERE id = ? AND account_id = ?", (person_id, account_id),
    ).fetchone() if person_id is not None else None
    email = person["email"].strip().casefold() if person else ""
    if person and person["do_not_contact"]:
        return "Contact marked do not contact"
    for rule in connection.execute("SELECT kind, value, reason FROM suppression ORDER BY id"):
        value = rule["value"].casefold()
        if (
            (rule["kind"] == "account" and value == str(account_id))
            or (rule["kind"] == "email" and email and email == value)
            or (rule["kind"] == "domain" and (
                matches_domain(account["domain"], value) or matches_domain(email.rpartition("@")[2], value)
            ))
        ):
            return rule["reason"]
    return None


def is_blocked(connection, account_id, person_id=None):
    return bool(suppression_reason(connection, account_id, person_id))


def audit(connection, user_id, account_id, action, details, now=None):
    connection.execute("""
        INSERT INTO audit_events(actor_id, account_id, action, details, created_at) VALUES (?, ?, ?, ?, ?)
    """, (user_id, account_id, action, json.dumps(details, sort_keys=True), timestamp(now)))
