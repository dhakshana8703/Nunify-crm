"""Validate saved Apollo organization evidence against established CRM identities."""

from collections import Counter, defaultdict

from organization_identity import is_platform_domain, organization_host, root_domain


APOLLO_SOURCE_SLUG = "apollo-organization"
APOLLO_SOURCE_NAME = "Apollo organization cache"


def is_apollo_name_source(name, adapter, key_hash):
    return name == APOLLO_SOURCE_NAME and adapter == "upload" and key_hash is None


def _organization_root(host):
    return root_domain(host) if host and not is_platform_domain(host) else ""


def _verified_root(evidence):
    if evidence is None:
        return None
    payload = evidence["payload"]
    root = _organization_root(payload["lookup_domain"])
    declared_hosts = [host for host in (payload["primary_domain"], payload["website_host"]) if host]
    if not root or not declared_hosts or any(_organization_root(host) != root for host in declared_hosts):
        return None
    return dict(evidence, root=root)


def plan_saved_apollo_names(normalized_rows, accounts, previous):
    accounts_by_root = defaultdict(list)
    for account in accounts:
        root = _organization_root(organization_host(account["domain"]))
        if root:
            accounts_by_root[root].append(account["id"])

    evidence_rows = [_verified_root(row) for row in normalized_rows]
    identities_by_provider = defaultdict(set)
    for evidence in evidence_rows:
        if evidence is not None:
            identities_by_provider[evidence["external_id"]].add((evidence["root"], evidence["payload"]["name"]))

    counts = Counter(imported=0, unchanged=0, review=0)
    additions = []
    previous = dict(previous)
    for evidence in evidence_rows:
        if evidence is None:
            counts["review"] += 1
            continue
        matches = accounts_by_root[evidence["root"]]
        org_id = evidence["external_id"]
        if len(matches) != 1 or len(identities_by_provider[org_id]) != 1:
            counts["review"] += 1
            continue
        evidence["account_id"] = matches[0]
        prior = previous.get(org_id)
        if prior is not None:
            same_identity = (prior["account_id"] == evidence["account_id"]
                             and root_domain(prior["payload"].get("lookup_domain")) == evidence["root"]
                             and prior["payload"].get("name") == evidence["payload"]["name"])
            counts["unchanged" if same_identity else "review"] += 1
            continue
        counts["imported"] += 1
        additions.append(evidence)
        previous[org_id] = evidence
    return dict(counts), additions
