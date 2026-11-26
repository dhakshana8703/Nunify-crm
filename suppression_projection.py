"""SQL maintenance for the stored account suppression flag and reason."""

import json

from account_redirects import account_aliases
from crm_core.suppression import account_suppression_changes


ACCOUNT_COLUMNS = {
    "suppressed": "BOOLEAN NOT NULL DEFAULT FALSE CHECK (suppressed IN (FALSE, TRUE))",
    "suppress_reason": "TEXT",
}


def account_projection_available(connection):
    return ACCOUNT_COLUMNS.keys() <= {row[1] for row in connection.execute("PRAGMA table_info(accounts)")}


def ensure_account_projection(connection):
    existing = {row[1] for row in connection.execute("PRAGMA table_info(accounts)")}
    missing = ACCOUNT_COLUMNS.keys() - existing
    for column, definition in ACCOUNT_COLUMNS.items():
        if column in missing:
            connection.execute(f"ALTER TABLE accounts ADD COLUMN {column} {definition}")
    return bool(missing)


def suppression_rules(connection):
    # The transfer and saved-contact previews also read pre-migration databases.
    return [rule for rule in connection.execute("SELECT * FROM suppression ORDER BY id")
            if "removed_at" not in rule.keys() or rule["removed_at"] is None]


def refresh_account_suppression(connection, account_ids=None):
    query = "SELECT id, domain, suppressed, suppress_reason FROM accounts"
    params = ()
    if account_ids is not None:
        ids = list(account_ids)
        if not ids:
            return
        query += " WHERE id IN (SELECT value FROM json_each(?))"
        params = (json.dumps(ids),)
    accounts = list(connection.execute(query + " ORDER BY id", params))
    aliases = account_aliases(connection, (account["id"] for account in accounts))
    updates = account_suppression_changes(accounts, suppression_rules(connection), aliases)
    if updates:
        connection.executemany("UPDATE accounts SET suppressed = ?, suppress_reason = ? WHERE id = ?", updates)
