"""Import approved organization evidence from a read-only local Apollo cache."""

import argparse
import json
import re
import sqlite3
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

from werkzeug.security import check_password_hash

from account_store import SqlAccountNames, user_for_login
from crm_core.account_names import clean_name
from crm_core.apollo_organization_names import (
    APOLLO_SOURCE_NAME, APOLLO_SOURCE_SLUG, is_apollo_name_source, plan_saved_apollo_names,
)
from crm_store import connect, is_postgres
from organization_identity import normalize_dns_hostname, organization_host, text_value, web_url
from workflow_store import transaction


PROVIDER_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


def _cache_evidence(row):
    try:
        organization = json.loads(row["raw_json"])
        cached_at = datetime.fromisoformat(row["cached_at"])
    except (ValueError, TypeError):
        return None
    if not isinstance(organization, dict):
        return None

    org_id = text_value(row["org_id"])
    lookup = normalize_dns_hostname(text_value(row["domain"]))
    name = clean_name(organization.get("name"), domain=lookup, inferred=False)
    if not lookup or not PROVIDER_ID.fullmatch(org_id) or org_id != organization.get("id"):
        return None
    if not name or name != clean_name(row["name"], domain=lookup, inferred=False):
        return None

    primary, website = organization.get("primary_domain"), organization.get("website_url")
    primary_host = normalize_dns_hostname(text_value(primary))
    parsed_site = web_url(website) if isinstance(website, str) else None
    site_host = organization_host(website, strip_www=False) if parsed_site else ""
    if (primary and not primary_host) or (website and not site_host):
        return None

    observed = cached_at.replace(tzinfo=timezone.utc) if cached_at.tzinfo is None else cached_at.astimezone(timezone.utc)
    return {
        "external_id": org_id,
        "source_url": f"{parsed_site.scheme}://{site_host}" if site_host else "",
        "observed_at": observed.isoformat(timespec="microseconds"),
        "payload": {"provider_org_id": org_id, "name": name, "lookup_domain": lookup,
                    "primary_domain": primary_host, "website_host": site_host},
    }


def _save_evidence(connection, evidence):
    connection.execute("""
        INSERT INTO imported_source_evidence (source_slug, external_id, account_id, source_url, observed_at, payload_json)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (APOLLO_SOURCE_SLUG, evidence["external_id"], evidence["account_id"], evidence["source_url"],
          evidence["observed_at"], json.dumps(evidence["payload"], ensure_ascii=False, sort_keys=True)))


def import_saved_apollo_names(connection, cache_path, *, apply=False, admin=None):
    """Preview or write saved names for existing accounts without provider calls."""
    with closing(connect(Path(cache_path), read_only=True)) as cache:
        rows = cache.execute("""
            SELECT domain, org_id, name, raw_json, cached_at FROM apollo_org_cache ORDER BY domain
        """).fetchall()
    evidence_rows = [_cache_evidence(row) for row in rows]

    with transaction(connection) if apply else nullcontext():
        if apply and admin is not None:
            email, password = admin
            user = user_for_login(connection, email)
            if user is None or user["role"] != "admin" or not check_password_hash(user["password_hash"], password):
                raise PermissionError("An active Admin sign-in is required to apply Apollo name evidence.")
        source = connection.execute("SELECT name, adapter, key_hash FROM ingest_sources WHERE slug = ?",
                                    (APOLLO_SOURCE_SLUG,)).fetchone()
        if source is not None and not is_apollo_name_source(source["name"], source["adapter"], source["key_hash"]):
            raise ValueError("The reserved Apollo organization evidence source is already in use.")
        stored = connection.execute("""
            SELECT external_id, account_id, payload_json FROM imported_source_evidence WHERE source_slug = ?
        """, (APOLLO_SOURCE_SLUG,)).fetchall()
        previous = {row["external_id"]: dict(row, payload=json.loads(row["payload_json"])) for row in stored}
        counts, additions = plan_saved_apollo_names(evidence_rows, SqlAccountNames(connection).accounts(), previous)
        if apply and additions:
            if source is None:
                connection.execute("INSERT INTO ingest_sources (slug, name, adapter) VALUES (?, ?, 'upload')",
                                   (APOLLO_SOURCE_SLUG, APOLLO_SOURCE_NAME))
            for evidence in additions:
                _save_evidence(connection, evidence)
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Existing CRM SQLite file or PostgreSQL URL.")
    parser.add_argument("--cache", type=Path, required=True, help="Saved Apollo SQLite cache file.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print counts without writing (default).")
    mode.add_argument("--apply", action="store_true", help="Record eligible organization evidence as an active Admin.")
    parser.add_argument("--admin-email", help="Active CRM Admin email for apply.")
    options = parser.parse_args(argv)
    if options.apply and not (options.admin_email or "").strip():
        parser.error("--apply requires --admin-email")
    if not is_postgres(options.db) and not Path(options.db).is_file():
        parser.error("Choose an existing CRM database.")
    if not options.cache.is_file():
        parser.error("Choose an existing Apollo SQLite cache.")
    admin = (options.admin_email.strip(), getpass("CRM Admin password: ")) if options.apply else None
    try:
        with closing(connect(options.db, read_only=not options.apply)) as connection:
            counts = import_saved_apollo_names(connection, options.cache, apply=options.apply, admin=admin)
    except (PermissionError, ValueError) as error:
        parser.exit(2, f"{error}\n")
    except (OSError, sqlite3.Error):
        parser.exit(2, "The saved Apollo cache or CRM database could not be processed.\n")
    print(json.dumps({"mode": "apply" if options.apply else "dry_run", **counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
