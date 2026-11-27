"""Start a hosted PostgreSQL pilot with named test users and sample data."""

import os
from contextlib import closing

from werkzeug.security import check_password_hash, generate_password_hash

from account_store import SqlPilotUsers
from apollo_provider import ApolloProvider
from app import create_app
from crm_core.pilot import PilotSignIn, provision_pilot_users
from crm_store import configured_database, connect, has_accounts, import_snapshot, is_postgres
from workflow_store import transaction


PILOT_USERS = (
    ("Pilot Admin", "admin@nunify.com", "admin@pilot.example", "admin", "CRM_ADMIN_PASSWORD"),
    ("Pilot Manager", "manager@nunify.com", "manager@pilot.example", "manager", "CRM_MANAGER_PASSWORD"),
    ("Riya Rao", "riya@nunify.com", "sdr@pilot.example", "sdr", "CRM_SDR_PASSWORD"),
)
ADDITIONAL_PILOT_SDRS = (
    ("Arjun Mehta", "arjun@nunify.com", "CRM_ARJUN_PASSWORD"),
    ("Neha Shah", "neha@nunify.com", "CRM_NEHA_PASSWORD"),
    ("Kavya Nair", "kavya@nunify.com", "CRM_KAVYA_PASSWORD"),
)


def configure_pilot_users(connection):
    users = list(PILOT_USERS)
    for name, email, variable in ADDITIONAL_PILOT_SDRS:
        if os.environ.get(variable):
            users.append((name, email, None, "sdr", variable))

    sign_ins = (PilotSignIn(name, email, legacy_email, role, variable, os.environ.get(variable))
                for name, email, legacy_email, role, variable in users)
    with transaction(connection):
        provision_pilot_users(SqlPilotUsers(connection), sign_ins, check_password_hash, generate_password_hash)


def create_pilot_app():
    database = configured_database()
    if not is_postgres(database):
        raise ValueError("Set DATABASE_URL to the pilot PostgreSQL database.")
    if not os.environ.get("CRM_SECRET_KEY"):
        raise ValueError("Set CRM_SECRET_KEY for the hosted pilot.")

    app = create_app(database)
    apollo_key = os.environ.get("APOLLO_API_KEY", "").strip()
    app.config.update(
        APOLLO_WEB_ENABLED=True,
        APOLLO_PROVIDER=ApolloProvider(apollo_key) if apollo_key else None,
        APOLLO_ALLOW_PAID=os.environ.get("CRM_APOLLO_ALLOW_PAID") == "1",
    )
    with closing(connect(database)) as connection:
        configure_pilot_users(connection)
        existing_accounts = has_accounts(connection)

    if not existing_accounts:
        import_snapshot(database, {
            "pilot-association": {
                "name": "Pilot Association", "domain": "pilot.example", "organization_type": "Association",
            },
        }, [])
    return app
