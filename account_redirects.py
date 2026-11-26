"""Access current identities while retaining former account IDs and domains."""

import json


def _links(connection):
    if not connection.execute("PRAGMA table_info(account_merges)").fetchone():
        return {}
    return dict(connection.execute("SELECT from_id, to_id FROM account_merges"))


def _survivor(links, account_id):
    while account_id in links:
        next_id = links[account_id]
        if next_id >= account_id:
            raise ValueError("Account merge history must point to a lower ID.")
        account_id = next_id
    return account_id


def canonical_account_id(connection, account_id):
    return _survivor(_links(connection), account_id)


def _add_prior_names(connection, accounts):
    if not connection.execute("PRAGMA table_info(audit_events)").fetchone():
        return
    by_id = {account["id"]: account for account in accounts}
    placeholders = ", ".join("?" for _ in by_id)
    for row in connection.execute(f"""
        SELECT account_id, details FROM audit_events
        WHERE action = 'account_name_changed' AND account_id IN ({placeholders})
    """, tuple(by_id)):
        try:
            details = json.loads(row["details"])
        except (TypeError, ValueError):
            continue
        before = details.get("before") if isinstance(details, dict) else None
        name = before.get("name") if isinstance(before, dict) else None
        if isinstance(name, str) and name.strip():
            by_id[row["account_id"]].setdefault("prior_names", []).append(name)


def account_identity_rows(connection, root=None):
    from organization_identity import root_domain

    links = _links(connection)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)")}
    key = "root_domain" if "root_domain" in columns else "NULL AS root_domain"
    filter_root = root is not None and "root_domain" in columns
    where = " WHERE root_domain = ? OR root_domain IS NULL" if filter_root else ""
    params = (root,) if filter_root else ()
    result = []
    for account in connection.execute(
        f"SELECT id, identity_key, name, domain, {key} FROM accounts{where} ORDER BY id", params,
    ):
        if root is None or (account["root_domain"] or root_domain(account["domain"])) == root:
            result.append(dict(account, canonical_id=_survivor(links, account["id"])))
    if root and len({row["canonical_id"] for row in result}) == 1:
        _add_prior_names(connection, result)
    return result


def account_aliases(connection, account_ids):
    requested = set(account_ids)
    links = _links(connection)
    aliases = {}
    if requested and links:
        for row in connection.execute("""
            SELECT m.from_id, a.domain FROM account_merges m JOIN accounts a ON a.id = m.from_id ORDER BY m.from_id
        """):
            winner = _survivor(links, row["from_id"])
            if winner in requested:
                aliases.setdefault(winner, []).append((row["from_id"], row["domain"]))
    return aliases
