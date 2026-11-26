"""Persist facts derived from linked evidence on both CRM databases."""

import json
import os
from collections import defaultdict
from datetime import date, datetime, timezone

from crm_core.account_facts import business_day, derive_account_facts


FACT_COLUMNS = {
    "platform": "TEXT NOT NULL DEFAULT 'unknown' CHECK (platform IN ('cvent', 'other_detected', 'none', 'unknown'))",
    "platform_detail": "TEXT",
    "platform_confidence": "TEXT NOT NULL DEFAULT 'unknown' CHECK (platform_confidence IN ('confirmed', 'detected', 'unknown'))",
    "next_event_date": "TEXT",
    "events_per_year": "INTEGER CHECK (events_per_year >= 0)",
    "members": "BIGINT CHECK (members >= 0)",
    "country": "TEXT",
    "region": "TEXT CHECK (region IN ('India', 'US', 'Europe', 'Middle East', 'Other'))",
    "facts_as_of": "TEXT",
}
DAILY_KEY = "account_facts_business_date"


def _day(observed):
    return business_day(observed or datetime.now(timezone.utc), os.environ.get("CRM_BUSINESS_TIMEZONE", "Asia/Kolkata"))


def _columns(connection):
    return {row[1] for row in connection.execute("PRAGMA table_info(accounts)")}


def _decode(value, default):
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return default
    return decoded if isinstance(decoded, type(default)) else default


def _evidence(connection, account_ids):
    records = connection.execute("""
        WITH selected(id) AS (SELECT value FROM json_each(?))
        SELECT r.account_id, r.source AS kind, r.source_id, r.details_json AS payload
        FROM source_records r JOIN selected ON selected.id = r.account_id
        UNION ALL
        SELECT e.account_id, 'cvent_roster', e.external_id, NULL
        FROM imported_source_evidence e JOIN selected ON selected.id = e.account_id
        WHERE e.source_slug = 'cvent' AND e.external_id LIKE ?
        UNION ALL
        SELECT q.account_id, 'qualification', NULL, q.signals
        FROM qualifications q JOIN selected ON selected.id = q.account_id
    """, (json.dumps(account_ids), "company:%"))
    events, listings, qualifications = defaultdict(list), defaultdict(list), {}
    roster = set()
    for record in records:
        account_id, kind = record["account_id"], record["kind"]
        if kind == "cvent":
            detail = _decode(record["payload"], {})
            events[account_id].append((record["source_id"], detail.get("start_date_iso")))
        elif kind == "directory":
            listings[account_id].append(_decode(record["payload"], {}))
        elif kind == "cvent_roster":
            roster.add(account_id)
        else:
            qualifications[account_id] = {"signals": _decode(record["payload"], [])}
    return events, listings, qualifications, roster


def _save_facts(connection, updates):
    fields = ("id", *FACT_COLUMNS)
    column_list = ", ".join(fields)
    replacements = ", ".join(f"{name} = refreshed.{name}" for name in FACT_COLUMNS)
    numbers = {"id", "members", "events_per_year"}
    placeholder = "(" + ", ".join(f"CAST(? AS {'BIGINT' if field in numbers else 'TEXT'})" for field in fields) + ")"
    # Keep batched writes within SQLite's older 999-variable limit.
    for start in range(0, len(updates), 90):
        batch = updates[start:start + 90]
        values = ", ".join(placeholder for _ in batch)
        connection.execute(f"""
            WITH refreshed ({column_list}) AS (VALUES {values})
            UPDATE accounts SET {replacements}
            FROM refreshed WHERE accounts.id = refreshed.id
        """, tuple(item for update in batch for item in update))


def _refresh(connection, day, account_ids):
    params = ()
    selected = ""
    if account_ids is not None:
        ids = sorted({account_id for account_id in account_ids if account_id is not None})
        if not ids:
            return 0
        selected = "AND a.id IN (SELECT value FROM json_each(?))"
        params = (json.dumps(ids),)
    accounts = connection.execute(f"""
        SELECT a.id, {", ".join(f"a.{name}" for name in FACT_COLUMNS)}
        FROM accounts a WHERE NOT EXISTS (SELECT 1 FROM account_merges WHERE from_id = a.id)
        {selected} ORDER BY a.id
    """, params).fetchall()
    if not accounts:
        return 0
    events, listings, qualifications, roster = _evidence(connection, [row["id"] for row in accounts])
    updates = []
    for account in accounts:
        account_id = account["id"]
        previous_day = account["facts_as_of"]
        as_of = date.fromisoformat(previous_day) if previous_day and previous_day > day.isoformat() else day
        values = derive_account_facts(
            observed_on=as_of, events=events[account_id], listings=listings[account_id],
            qualification=qualifications.get(account_id), cvent_roster=account_id in roster,
        )
        values["facts_as_of"] = as_of.isoformat()
        if any(account[name] != values[name] for name in FACT_COLUMNS):
            updates.append((account_id, *(values[name] for name in FACT_COLUMNS)))
    _save_facts(connection, updates)
    return len(updates)


def refresh_account_facts(connection, account_ids=None, *, observed=None):
    """Refresh within the caller's writer transaction after changing linked evidence."""
    if "facts_as_of" not in _columns(connection):
        return 0
    return _refresh(connection, _day(observed), account_ids)


def _daily_is_current(connection, day):
    row = connection.execute("SELECT value FROM workflow_settings WHERE key = ?", (DAILY_KEY,)).fetchone()
    return row is not None and row[0] >= day.isoformat()


def _refresh_daily(connection, day, *, force=False):
    if not force and _daily_is_current(connection, day):
        return 0
    count = _refresh(connection, day, None)
    connection.execute("""
        INSERT INTO workflow_settings(key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value
    """, (DAILY_KEY, day.isoformat()))
    return count


def initialize_account_facts(connection, *, observed=None):
    from workflow_store import transaction

    with transaction(connection):
        existing = _columns(connection)
        for name, definition in FACT_COLUMNS.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE accounts ADD COLUMN {name} {definition}")
        changed = not FACT_COLUMNS.keys() <= existing
        if changed and getattr(connection, "dialect", "sqlite") == "postgresql":
            connection.execute("ALTER TABLE accounts ALTER COLUMN organization_type SET DEFAULT 'Unknown'")
        return _refresh_daily(connection, _day(observed), force=changed)


def refresh_daily_account_facts(connection, observed):
    from workflow_store import transaction

    day = _day(observed)
    if _daily_is_current(connection, day) or "facts_as_of" not in _columns(connection):
        return 0
    with transaction(connection):
        return _refresh_daily(connection, day)
