"""Portable account-root migration and retained merge identifiers."""

from collections import Counter

from organization_identity import root_domain


def initialize_account_identity(connection):
    """Upgrade account identity storage in the caller's writer transaction."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)")}
    if "root_domain" not in columns:
        connection.execute("ALTER TABLE accounts ADD COLUMN root_domain TEXT COLLATE NOCASE")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS account_merges (
            from_id INTEGER PRIMARY KEY REFERENCES accounts(id),
            to_id INTEGER NOT NULL REFERENCES accounts(id),
            merged_at TEXT NOT NULL,
            CHECK (from_id > to_id)
        )
    """)
    return refresh_account_roots(connection)


def refresh_account_roots(connection):
    """Store singleton roots and report active collisions in the writer transaction."""
    accounts = connection.execute("""
        SELECT a.id, a.domain, a.root_domain, m.from_id AS merged_from_id
        FROM accounts a LEFT JOIN account_merges m ON m.from_id = a.id
        ORDER BY a.id
    """).fetchall()
    roots = {
        account["id"]: root_domain(account["domain"]) or None
        for account in accounts if account["merged_from_id"] is None
    }
    counts = Counter(root for root in roots.values() if root)
    changes = []
    for account in accounts:
        root = roots.get(account["id"])
        wanted = root if counts[root] == 1 else None
        if wanted != account["root_domain"]:
            changes.append((wanted, account["id"]))

    if changes:
        # Clear changed keys before filling the new ones to avoid intermediate collisions.
        connection.executemany("""
            UPDATE accounts SET root_domain = NULL WHERE id = ? AND root_domain IS NOT NULL
        """, [(account_id,) for _, account_id in changes])
        connection.executemany("""
            UPDATE accounts SET root_domain = ? WHERE id = ?
        """, [(root, account_id) for root, account_id in changes if root])

    collisions = {root: count for root, count in counts.items() if count > 1}
    if not collisions:
        connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS accounts_root_domain_unique ON accounts(root_domain)
        """)
    return collisions
