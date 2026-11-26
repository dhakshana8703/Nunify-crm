"""SQL readers and writers for account access, name evidence and profile facts."""

import json

from crm_core.account_names import name_evidence
from workflow_store import account_rows, active_coverage, audit, suppression_reason, transaction


def active_session_user(connection, user_id):
    row = connection.execute("""
        SELECT id, name, role, timezone FROM users WHERE id = ? AND active = 1
    """, (user_id,)).fetchone()
    return dict(row) if row else None


def user_for_login(connection, email):
    return connection.execute("SELECT * FROM users WHERE email = ? AND active = 1", (email,)).fetchone()


def initialize_names(connection):
    with transaction(connection):
        columns = {row[1] for row in connection.execute("PRAGMA table_info(accounts)")}
        if "name_source" not in columns:
            connection.execute("ALTER TABLE accounts ADD COLUMN name_source TEXT NOT NULL DEFAULT 'unreviewed'")
        if "name_source_ref" not in columns:
            connection.execute("ALTER TABLE accounts ADD COLUMN name_source_ref TEXT")


class SqlAccountAccess:
    def __init__(self, connection):
        self.connection = connection

    def account(self, account_id, today):
        return account_rows(self.connection, today, account_id)

    def accounts(self, today):
        return account_rows(self.connection, today)

    def coverage(self, account, observed):
        return active_coverage(self.connection, account, observed)

    def restriction(self, account_id):
        return suppression_reason(self.connection, account_id)


class SqlAccountNames:
    def __init__(self, connection):
        self.connection = connection

    def actor(self, actor_id):
        return self.connection.execute("SELECT role, active FROM users WHERE id = ?", (actor_id,)).fetchone()

    def account(self, account_id):
        return self.connection.execute("""
            SELECT id, name, domain, name_source, name_source_ref FROM accounts a WHERE id = ?
            AND NOT EXISTS (SELECT 1 FROM account_merges WHERE from_id = a.id)
        """, (account_id,)).fetchone()

    def accounts(self, *, unreviewed=False):
        unreviewed_filter = " AND name_source = 'unreviewed'" if unreviewed else ""
        return self.connection.execute(f"""
            SELECT id, name, domain, name_source, name_source_ref FROM accounts a
            WHERE NOT EXISTS (SELECT 1 FROM account_merges WHERE from_id = a.id){unreviewed_filter} ORDER BY id
        """).fetchall()

    def evidence(self, account_id=None):
        from crm_core.apollo_organization_names import APOLLO_SOURCE_SLUG, is_apollo_name_source

        params = () if account_id is None else (account_id,)
        linked = "" if account_id is None else "AND a.id = ?"
        saved = self.connection.execute(f"""
            SELECT a.id, a.domain, r.source, r.source_id, r.source_url, r.details_json
            FROM source_records r JOIN accounts a ON a.id = r.account_id
            WHERE r.review_reason IS NULL {linked} ORDER BY r.id
        """, params).fetchall()
        imported = self.connection.execute(f"""
            SELECT a.id, a.domain, e.source_slug, e.external_id, e.source_url, e.payload_json,
                   s.name AS source_name, s.adapter AS source_adapter, s.key_hash
            FROM imported_source_evidence e JOIN accounts a ON a.id = e.account_id
            JOIN ingest_sources s ON s.slug = e.source_slug
            WHERE 1 = 1 {linked} ORDER BY e.id
        """, params).fetchall()
        imported_rows = []
        apollo_rows = []
        for row in imported:
            payload = json.loads(row["payload_json"])
            if row["source_slug"] != APOLLO_SOURCE_SLUG:
                imported_rows.append(dict(row, payload=payload))
            elif (is_apollo_name_source(row["source_name"], row["source_adapter"], row["key_hash"])
                  and isinstance(payload, dict)
                  and payload.get("provider_org_id") == row["external_id"]):
                apollo_rows.append(dict(row, name=payload.get("name")))
        return name_evidence(
            (dict(row, details=json.loads(row["details_json"])) for row in saved),
            imported_rows, apollo_rows,
        )

    def record_source(self, account, match, actor_id, observed):
        self.connection.execute("""
            UPDATE accounts SET name_source = ?, name_source_ref = ? WHERE id = ?
        """, (match["source"], match["reference"], account["id"]))
        audit(self.connection, actor_id, account["id"], "account_name_source_recorded", {
            "name": account["name"], "source": match["source"], "reference": match["reference"],
            "source_url": match["url"],
        }, observed)

    def save_name(self, account_id, actor_id, before, state, source_url, reason, observed):
        self.connection.execute("""
            UPDATE accounts SET name = ?, name_source = ?, name_source_ref = ? WHERE id = ?
        """, (*state, account_id))
        fields = ("name", "source", "reference")
        audit(self.connection, actor_id, account_id, "account_name_changed", {
            "before": dict(zip(fields, before)), "after": dict(zip(fields, state)),
            "source_url": source_url, "reason": reason,
        }, observed)


class SqlAccountProfile:
    def __init__(self, connection):
        self.connection = connection

    def evidence(self, account_id):
        qualification = self.connection.execute("""
            SELECT score, signals FROM qualifications WHERE account_id = ?
        """, (account_id,)).fetchone()
        cvent = self.connection.execute("""
            SELECT 1 FROM source_records WHERE account_id = ? AND source = 'cvent' LIMIT 1
        """, (account_id,)).fetchone()
        if qualification:
            qualification = dict(qualification, signals=json.loads(qualification["signals"]))
        return qualification, cvent is not None

    def save_type(self, account_id, actor_id, previous, selected, observed):
        self.connection.execute("UPDATE accounts SET organization_type = ? WHERE id = ?", (selected, account_id))
        audit(self.connection, actor_id, account_id, "organization_type_changed",
              {"previous": previous, "type": selected}, observed)


class SqlPilotUsers:
    def __init__(self, connection):
        self.connection = connection

    def find(self, email, legacy_email):
        return self.connection.execute("""
            SELECT id, email, role, password_hash FROM users WHERE email IN (?, ?)
        """, (email, legacy_email)).fetchall()

    def add(self, sign_in, password_hash, timezone, quota):
        self.connection.execute("""
            INSERT INTO users(name, email, role, timezone, password_hash, daily_credit_quota)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (sign_in.name, sign_in.email, sign_in.role, timezone, password_hash, quota))

    def update(self, user_id, email, password_hash):
        self.connection.execute("UPDATE users SET email = ?, password_hash = ? WHERE id = ?",
                                (email, password_hash, user_id))
