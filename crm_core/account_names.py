"""Organization name evidence, review and stable provenance decisions."""

import hashlib
import json
import unicodedata
from collections import Counter, defaultdict
from typing import Protocol

import idna

from organization_identity import is_platform_domain, normalized_words, organization_host, root_domain, web_url


NAME_SOURCES = {
    "unreviewed": "Existing name", "apollo": "Apollo organization", "organizer": "Cvent organizer",
    "directory": "Directory of Associations", "website": "Organization website", "domain": "Domain-derived name",
    "manual": "Admin edit",
}
PRIORITY = {"apollo": 0, "organizer": 1, "directory": 2, "website": 3, "domain": 4}
GENERIC_NAMES = {
    "association", "cvent", "cvent events", "event", "event organizer", "events", "home", "homepage",
    "no name", "none", "not available", "organizer", "organization", "registration", "unknown", "untitled",
}


class AccountNameConflict(ValueError):
    pass


class AccountNameStore(Protocol):
    def account(self, account_id): ...

    def accounts(self, *, unreviewed=False): ...

    def evidence(self, account_id=None): ...

    def actor(self, actor_id): ...

    def record_source(self, account, match, actor_id, observed): ...

    def save_name(self, account_id, actor_id, before, state, source_url, reason, observed): ...


def clean_name(value, *, domain="", event_name="", inferred=True):
    if not isinstance(value, str) or any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        return ""
    name = " ".join(value.split())
    key = normalized_words(name)
    if (
        not 2 <= len(name) <= 200 or not any(char.isalpha() for char in name)
        or "@" in name or "://" in name or name.casefold().startswith("www.")
        or key in GENERIC_NAMES or key == normalized_words(domain) or key == normalized_words(event_name)
    ):
        return ""
    if inferred and " " not in name and len(name) > 15 and not name.isupper():
        return ""
    return name


def _public_url(value):
    return value if isinstance(value, str) and web_url(value) else ""


def _fingerprint(*values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()


def name_proposal(source, name, reference, url, *, domain, event_name=""):
    name = clean_name(name, domain=domain, event_name=event_name, inferred=source in {"organizer", "directory"})
    if not name:
        return None
    evidence = {"source": source, "source_label": NAME_SOURCES[source], "name": name,
                "reference": reference, "url": _public_url(url)}
    return dict(evidence, key=_fingerprint(evidence))


def name_evidence(saved_rows, imported_rows, apollo_rows=()):
    proposals = defaultdict(list)
    for row in apollo_rows:
        proposal = name_proposal("apollo", row["name"], f"apollo:{row['external_id']}", row["source_url"],
                                 domain=row["domain"])
        if proposal:
            proposals[row["id"]].append(proposal)

    for row in saved_rows:
        details = row["details"]
        if row["source"] == "cvent":
            source, name = "organizer", details.get("organizer_company")
        else:
            source, name = "directory", details.get("name")
        proposal = name_proposal(source, name, f"{row['source']}:{row['source_id']}", row["source_url"],
                                 domain=row["domain"], event_name=details.get("event_name") or "")
        if proposal:
            proposals[row["id"]].append(proposal)

    for row in imported_rows:
        url = _public_url(row["source_url"])
        host = organization_host(url)
        if not host or is_platform_domain(host) or host not in {
            organization_host(row["domain"]), root_domain(row["domain"]),
        }:
            continue
        payload = row["payload"]
        for key in ("og:site_name", "og_site_name", "website_title"):
            proposal = name_proposal("website", payload.get(key), f"{row['source_slug']}:{row['external_id']}",
                                     url, domain=row["domain"])
            if proposal:
                if row["source_slug"] != "upload":
                    proposal["review_only"] = True
                proposals[row["id"]].append(proposal)
                break
    return proposals


def _domain_proposal(account):
    root = root_domain(account["domain"])
    if not root or is_platform_domain(root):
        return None
    name = idna.decode(root.split(".", 1)[0]).replace("-", " ").title()
    if normalized_words(name) == normalized_words(account["name"]):
        name = account["name"]
    return name_proposal("domain", name, f"root-domain:{root}", "", domain=account["domain"])


def account_naming(account, evidence):
    candidates = {}
    for proposal in evidence:
        candidates.setdefault((proposal["source"], normalized_words(proposal["name"]), proposal.get("review_only")),
                              proposal)
    candidates = sorted(candidates.values(), key=lambda item: (PRIORITY[item["source"]], item["name"].casefold()))
    eligible = [proposal for proposal in candidates if not proposal.get("review_only")]
    if not eligible:
        fallback = _domain_proposal(account)
        if fallback:
            candidates.append(fallback)
            eligible.append(fallback)
    preferred = eligible[0] if eligible else None
    source = account["name_source"]
    if source == "manual":
        state = "manual"
    elif source in PRIORITY and (preferred is None or PRIORITY[preferred["source"]] > PRIORITY[source]):
        state = "approved"
    elif preferred is None:
        state = "missing"
    elif len(eligible) > 1 and eligible[1]["source"] == preferred["source"]:
        state = "conflicting"
    elif (
        preferred["source"] == "domain" and " " in clean_name(account["name"], inferred=False)
        and normalized_words(preferred["name"]) != normalized_words(account["name"])
    ):
        state = "review_domain"
    elif source == preferred["source"] and account["name"] == preferred["name"]:
        state = "approved"
    elif normalized_words(preferred["name"]) == normalized_words(account["name"]):
        state = "matched"
    else:
        state = "different"
    return {
        "id": account["id"], "name": account["name"], "domain": account["domain"], "state": state,
        "source": account["name_source"], "source_label": NAME_SOURCES.get(account["name_source"], "Source evidence"),
        "reference": account["name_source_ref"], "candidates": candidates,
        "preferred": None if state in {"conflicting", "review_domain"} else preferred,
        "version": _fingerprint(account["name"], account["domain"], account["name_source"],
                                account["name_source_ref"], candidates),
    }


def is_exact_match(record):
    return (
        record["source"] == "unreviewed" and record["state"] == "matched"
        and record["preferred"]["source"] != "domain" and record["preferred"]["name"] == record["name"]
    )


def plan_name_backfill(store: AccountNameStore):
    evidence = store.evidence()
    accounts = []
    for account in store.accounts():
        record = account_naming(account, evidence.get(account["id"], ()))
        before = {"name": account["name"], "source": account["name_source"], "reference": account["name_source_ref"]}
        preferred = record["preferred"]
        after = before
        if record["state"] in {"manual", "conflicting", "missing", "review_domain"}:
            action = record["state"]
        elif preferred is None or PRIORITY[preferred["source"]] > PRIORITY.get(before["source"], len(PRIORITY)):
            action = "unchanged"
        else:
            after = {key: preferred[key] for key in before}
            action = "change" if after != before else "unchanged"
        row = {"id": account["id"], "domain": account["domain"], "action": action, "before": before,
               "after": after, "version": record["version"]}
        if action == "change":
            row["source_url"] = preferred["url"]
        if action in {"conflicting", "review_domain"}:
            row["candidates"] = record["candidates"]
        accounts.append(row)
    return {"accounts": accounts, "counts": dict(Counter(row["action"] for row in accounts)),
            "fingerprint": _fingerprint(accounts)}


def apply_name_backfill(store: AccountNameStore, *, expected, actor_id, observed):
    actor = store.actor(actor_id)
    if actor is None or actor["role"] != "admin" or not actor["active"]:
        raise PermissionError("Account name backfill requires an active admin.")
    plan = plan_name_backfill(store)
    if expected != plan["fingerprint"]:
        raise AccountNameConflict("The name backfill plan changed. Review a new dry run before applying it.")
    fields = ("name", "source", "reference")
    for row in plan["accounts"]:
        if row["action"] == "change":
            before, after = (tuple(row[key][field] for field in fields) for key in ("before", "after"))
            store.save_name(row["id"], actor_id, before, after, row["source_url"],
                            "Applied the reviewed account name backfill.", observed)
    return plan


def backfill_name_provenance(store: AccountNameStore, actor_id, observed):
    actor = store.actor(actor_id)
    if actor is None or actor["role"] != "admin" or not actor["active"]:
        raise PermissionError("Account name provenance requires an active admin.")
    sources = Counter()
    evidence = store.evidence()
    for account in store.accounts(unreviewed=True):
        record = account_naming(account, evidence.get(account["id"], ()))
        if is_exact_match(record):
            match = record["preferred"]
            store.record_source(account, match, actor_id, observed)
            sources[match["source"]] += 1
    return {"recorded": sum(sources.values()), "sources": dict(sources)}


def apply_name(store: AccountNameStore, actor, account_id, observed, *, decision, version, candidate=None,
               name="", reason=""):
    if actor["role"] != "admin":
        raise PermissionError("Account name changes require an admin.")
    account = store.account(account_id)
    if account is None:
        raise LookupError(account_id)
    current = account_naming(account, store.evidence(account_id).get(account_id, ()))
    if version != current["version"]:
        raise AccountNameConflict("The account name or its evidence changed. Reload the account and review it again.")
    if decision == "manual":
        name, reason = name.strip(), reason.strip()
        if not name or len(name) > 200:
            raise ValueError("Enter a valid name.")
        name = clean_name(name, inferred=False)
        if not name:
            raise ValueError("Enter a recognizable organization name.")
        if not reason or len(reason) > 500:
            raise ValueError("Enter a valid reason.")
        updates = {"name": name, "source": "manual", "reference": None, "url": ""}
    elif decision == "approve":
        if current["source"] == "manual":
            raise AccountNameConflict("This name was entered by an admin. Use the manual edit form to change it.")
        updates = next((item for item in current["candidates"] if item["key"] == candidate), None)
        if updates is None:
            raise ValueError("Choose a name from the current source evidence.")
        reason = "Approved linked organization name evidence."
    else:
        raise ValueError("Choose how to save the account name.")
    state = (updates["name"], updates["source"], updates["reference"])
    before = (account["name"], account["name_source"], account["name_source_ref"])
    if before != state:
        store.save_name(account_id, actor["id"], before, state, updates["url"], reason, observed)
