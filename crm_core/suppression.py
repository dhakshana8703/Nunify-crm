"""Match account and contact facts against active outreach rules."""

from organization_identity import is_platform_domain, matches_domain, normalize_dns_hostname, root_domain


BLOCKED_CONTACT_STATUSES = frozenset({"opted_out", "bounced"})


def contact_has_restriction(person):
    return person["do_not_contact"] or person["status"] in BLOCKED_CONTACT_STATUSES


def normalize_domain_rule(value):
    host = normalize_dns_hostname(value.removeprefix("*."))
    return "*." + host if host and value.startswith("*.") else host


def saved_address_is_blocked(rules, suppressed_emails, account_id, domain, email, *, aliases=()):
    email_key = (email or "").strip().casefold()
    return email_key in suppressed_emails or matching_rule(
        rules, account_id, domain, (email,), aliases=aliases,
    ) is not None


def matches_organization_domain(host, rule):
    if matches_domain(host, rule):
        return True
    # Only a bare rule may expand from a host to its registrable organization.
    target = root_domain(normalize_dns_hostname(rule))
    return bool(target and target == root_domain(host) and not is_platform_domain(host))


def matching_rule(rules, account_id, account_domain, emails=(), *, aliases=()):
    contacts = {email.strip() for email in emails if email}
    contacts.discard("")
    addresses = {email.casefold() for email in contacts}
    identities = {str(account_id), *(str(alias_id) for alias_id, _ in aliases)}
    organization_hosts = {account_domain, *(host for _, host in aliases)} - {"", None}
    contact_hosts = {address.rpartition("@")[2] for address in contacts}
    for rule in rules:
        value = rule["value"].casefold()
        if (
            (rule["kind"] == "account" and value in identities)
            or (rule["kind"] == "email" and value in addresses)
            or (rule["kind"] == "domain" and (
                any(matches_organization_domain(host, rule["value"]) for host in organization_hosts)
                or any(matches_domain(host, rule["value"]) for host in contact_hosts)
            ))
        ):
            return rule
    return None


def account_suppression_changes(accounts, rules, aliases):
    account_rules = [rule for rule in rules if rule["kind"] in {"account", "domain"}]
    changes = []
    for account in accounts:
        rule = matching_rule(account_rules, account["id"], account["domain"], aliases=aliases.get(account["id"], ()))
        suppressed = rule is not None
        reason = rule["reason"] if suppressed else None
        if (suppressed, reason) != (account["suppressed"], account["suppress_reason"]):
            changes.append((suppressed, reason, account["id"]))
    return changes
