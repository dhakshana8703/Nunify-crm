"""Start a hosted PostgreSQL pilot with named test users and sample data."""

import os
from contextlib import closing

from werkzeug.security import check_password_hash, generate_password_hash

from app import create_app
from crm_store import configured_database, connect, import_snapshot, is_postgres
from workflow_store import transaction


PILOT_USERS = (
    ("Pilot Admin", "admin@nunify.com", "admin@pilot.example", "admin", "CRM_ADMIN_PASSWORD"),
    ("Pilot Manager", "manager@nunify.com", "manager@pilot.example", "manager", "CRM_MANAGER_PASSWORD"),
    ("Riya Rao", "riya@nunify.com", "sdr@pilot.example", "sdr", "CRM_SDR_PASSWORD"),
)


def configure_pilot_users(connection):
    with transaction(connection):
        for name, email, legacy_email, role, variable in PILOT_USERS:
            matches = connection.execute("""
                SELECT id, email, role, password_hash FROM users WHERE email IN (?, ?)
            """, (email, legacy_email)).fetchall()
            if len(matches) > 1 or (matches and matches[0]["role"] != role):
                raise ValueError(f"Resolve the conflicting pilot sign-in for {email}.")

            password = os.environ.get(variable)
            if not matches:
                if not password:
                    raise ValueError(f"Set {variable} to provision {email}.")
                connection.execute("""
                    INSERT INTO users(name, email, role, timezone, password_hash, daily_credit_quota)
                    VALUES (?, ?, ?, 'Asia/Kolkata', ?, 0)
                """, (name, email, role, generate_password_hash(password)))
                continue

            user = matches[0]
            password_hash = user["password_hash"]
            if password and not check_password_hash(password_hash, password):
                password_hash = generate_password_hash(password)
            if user["email"] != email or user["password_hash"] != password_hash:
                connection.execute("""
                    UPDATE users SET email = ?, password_hash = ? WHERE id = ?
                """, (email, password_hash, user["id"]))


def create_pilot_app():
    database = configured_database()
    if not is_postgres(database):
        raise ValueError("Set DATABASE_URL to the pilot PostgreSQL database.")
    if not os.environ.get("CRM_SECRET_KEY"):
        raise ValueError("Set CRM_SECRET_KEY for the hosted pilot.")

    app = create_app(database)
    with closing(connect(database)) as connection:
        configure_pilot_users(connection)
        has_accounts = connection.execute("SELECT 1 FROM accounts LIMIT 1").fetchone()

    if not has_accounts:
        import_snapshot(database, {
            "pilot-association": {
                "name": "Pilot Association", "domain": "pilot.example", "organization_type": "Association",
            },
        }, [])
    return app
