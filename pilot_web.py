"""Start a hosted PostgreSQL pilot with named test users and sample data."""

import os
from contextlib import closing

from werkzeug.security import generate_password_hash

from app import create_app
from crm_store import configured_database, connect, import_snapshot, is_postgres
from workflow_store import transaction


PILOT_USERS = (
    ("Pilot Admin", "admin@pilot.example", "admin", "CRM_ADMIN_PASSWORD"),
    ("Pilot Manager", "manager@pilot.example", "manager", "CRM_MANAGER_PASSWORD"),
    ("Pilot SDR", "sdr@pilot.example", "sdr", "CRM_SDR_PASSWORD"),
)


def create_pilot_app():
    database = configured_database()
    if not is_postgres(database):
        raise ValueError("Set DATABASE_URL to the pilot PostgreSQL database.")
    if not os.environ.get("CRM_SECRET_KEY"):
        raise ValueError("Set CRM_SECRET_KEY for the hosted pilot.")

    app = create_app(database)
    with closing(connect(database)) as connection:
        with transaction(connection):
            for name, email, role, variable in PILOT_USERS:
                if connection.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                    continue
                password = os.environ.get(variable)
                if not password:
                    raise ValueError(f"Set {variable} to provision {email}.")
                connection.execute("""
                    INSERT INTO users(name, email, role, timezone, password_hash, daily_credit_quota)
                    VALUES (?, ?, ?, 'Asia/Kolkata', ?, 0)
                """, (name, email, role, generate_password_hash(password)))
        has_accounts = connection.execute("SELECT 1 FROM accounts LIMIT 1").fetchone()

    if not has_accounts:
        import_snapshot(database, {
            "pilot-association": {
                "name": "Pilot Association", "domain": "pilot.example", "organization_type": "Association",
            },
        }, [])
    return app
