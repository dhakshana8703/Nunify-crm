"""Decide how to transfer saved evidence while preserving destination sales work."""

from collections import defaultdict
from typing import Protocol

from crm_core.account_identity import corroborated_account
from crm_core.contacts import normalized_linkedin_profile
from crm_core.suppression import contact_has_restriction, saved_address_is_blocked
from organization_identity import root_domain


COUNTERS = (
    "accounts_added", "source_records_added", "review_records_added", "qualifications_added",
    "people_added", "saved_unlocks_added", "apollo_links_added", "contact_evidence_added", "contacts_held",
)


class AccountTransferStore(Protocol):
    contacts: "ContactTransferStore"
    roster: "CompanyRosterTransferStore"

    def accounts(self): ...
    def add_accounts(self, rows): ...
    def add_work(self, rows): ...
    def add_qualifications(self, rows): ...
    def evidence(self): ...
    def add_evidence(self, rows): ...

    def record_transfer(self, actor_id, counts): ...


class CompanyRosterTransferStore(Protocol):
    def evidence(self): ...
    def runs(self): ...
    def add_evidence(self, rows): ...
    def add_runs(self, runs): ...


class ContactTransferStore(Protocol):
    def people(self): ...
    def accounts(self): ...
    def apollo_links(self): ...
    def evidence_links(self): ...
    def rules(self): ...
    def add_person(self, account_id, person): ...
    def add_apollo_links(self, links): ...
    def add_contact_evidence(self, links): ...


class SeedSnapshotStore(Protocol):
    def ensure_account(self, identity, facts): ...

    def account_ids(self): ...

    def record_link(self, source, source_id): ...

    def write_record(self, record, account_id): ...


def seed_snapshot(store: SeedSnapshotStore, accounts, records):
    for identity, account in accounts.items():
        store.ensure_account(identity, account)
    account_ids = store.account_ids()
    changes = 0
    seen = set()
    for record in records:
        key = (record["source"], record["source_id"])
        if key in seen:
            raise ValueError(f"Duplicate input source record: {key}")
        seen.add(key)
        identity = record["identity_key"]
        account_id = account_ids[identity] if identity is not None else None
        current = store.record_link(*key)
        if current is not None and current["account_id"] != account_id:
            raise ValueError(f"Review required before changing the account link for {key}")
        changes += store.write_record(record, account_id)
    return changes


def _accounts_to_add(source_accounts, destination_rows):
    existing = {row["identity_key"]: row for row in destination_rows}
    by_root = defaultdict(list)
    for row in destination_rows:
        by_root[root_domain(row["domain"])].append(row)
    additions = []
    account_ids = {}
    seen_roots = set()
    for source in source_accounts:
        key = source["identity_key"]
        root = root_domain(source["domain"])
        if not root or root in seen_roots:
            raise ValueError(f"Local account {source['id']} has an ambiguous domain.")
        seen_roots.add(root)

        candidates = by_root[root]
        prior_key = existing.get(key)
        if prior_key is not None and root_domain(prior_key["domain"]) != root:
            raise ValueError(f"Local account {source['id']} conflicts with a destination account. Review the identity first.")
        target = corroborated_account(candidates, dict(source).get("name"), identity_key=key)
        if target is None and candidates:
            raise ValueError(f"Local account {source['id']} conflicts with a destination account. Review the identity first.")
        account_ids[source["id"]] = target.get("canonical_id", target["id"]) if target is not None else None
        if target is None:
            additions.append(dict(source, root_domain=root))
    return additions, account_ids


def transfer_accounts(store: AccountTransferStore, source_accounts, qualifications, counts, *, apply):
    additions, account_ids = _accounts_to_add(source_accounts, store.accounts())
    counts["accounts_added"] = len(additions)
    counts["qualifications_added"] = sum(row["id"] in qualifications for row in additions)
    if not (apply and additions):
        return account_ids
    ids = store.add_accounts(additions)
    for row in additions:
        account_ids[row["id"]] = ids[row["identity_key"]]
    store.add_work(
        (account_ids[row["id"]], row["stage"] if row["stage"] in {"qualified", "disqualified"} else "discovered")
        for row in additions
    )
    store.add_qualifications(
        (account_ids[row["id"]], qualifications[row["id"]]) for row in additions if row["id"] in qualifications
    )
    return account_ids


def transfer_evidence(store: AccountTransferStore, source_records, account_ids, counts, *, apply):
    existing = {(row["source"], row["source_id"]): row for row in store.evidence()}
    evidence_ids = {}
    additions = []
    for source in source_records:
        key = (source["source"], source["source_id"])
        source_account = source["account_id"]
        target_account = account_ids[source_account] if source_account is not None else None
        target = existing.get(key)
        if target is not None:
            if source_account is not None and (target_account is None or target["account_id"] != target_account):
                raise ValueError(f"Local source record {source['id']} conflicts with destination evidence. Review its account link first.")
            evidence_ids[source["id"]] = target["id"]
            continue
        additions.append((source, target_account))
        evidence_ids[source["id"]] = None
    counts["source_records_added"] = len(additions)
    counts["review_records_added"] = sum(row["account_id"] is None for row, _ in additions)
    if apply and additions:
        ids = store.add_evidence(additions)
        for row, _ in additions:
            evidence_ids[row["id"]] = ids[row["source"], row["source_id"]]
    return evidence_ids


def _roster_rows(rows):
    return [(row["row_number"], row["payload"], row["result"], row["detail"]) for row in rows]


def transfer_company_roster(store: CompanyRosterTransferStore, roster, account_ids, counts, *, apply):
    evidence, runs = roster
    existing = {row["external_id"]: row for row in store.evidence()}
    evidence_to_add = []
    for row in evidence:
        account_id = account_ids[row["account_id"]]
        prior = existing.get(row["external_id"])
        if prior is None:
            evidence_to_add.append((row, account_id))
        elif (prior["account_id"] != account_id or prior["source_url"] != row["source_url"]
              or prior["payload"] != row["payload"]):
            raise ValueError(f"Saved Cvent company {row['external_id']} conflicts with destination company evidence.")
    counts["company_evidence_added"] = len(evidence_to_add)

    existing_runs = {run["request_key"]: (run, rows) for run, rows in store.runs()}
    runs_to_add = []
    for run, rows in runs:
        if not run["request_key"] or not run["request_hash"]:
            raise ValueError("A saved Cvent roster run is missing its transfer identity.")
        prior = existing_runs.get(run["request_key"])
        if prior is None:
            runs_to_add.append((run, rows))
        elif (prior[0]["request_hash"] != run["request_hash"] or prior[0]["state"] != run["state"]
              or _roster_rows(prior[1]) != _roster_rows(rows)):
            raise ValueError("A saved Cvent roster run conflicts with its destination copy.")
    counts["roster_rows_added"] = sum(len(rows) for _, rows in runs_to_add)
    counts["roster_runs_added"] = len(runs_to_add)
    if apply:
        if evidence_to_add:
            store.add_evidence(evidence_to_add)
        if runs_to_add:
            store.add_runs(runs_to_add)


def transfer_snapshot(store: AccountTransferStore, accounts, evidence, qualifications, contacts, roster=None, *,
                      apply, actor_id):
    counts = dict.fromkeys(COUNTERS, 0)
    account_ids = transfer_accounts(store, accounts, qualifications, counts, apply=apply)
    evidence_ids = transfer_evidence(store, evidence, account_ids, counts, apply=apply)
    if contacts is not None:
        transfer_contact_data(store.contacts, contacts, account_ids, evidence_ids, counts, apply=apply)
    if roster is not None:
        transfer_company_roster(store.roster, roster, account_ids, counts, apply=apply)
    if actor_id is not None and any(count for key, count in counts.items() if key != "contacts_held"):
        store.record_transfer(actor_id, counts)
    return counts


def _blocked_emails(people):
    return {row["email"].strip().casefold() for row in people if row["email"] and contact_has_restriction(row)}


def _email(person):
    return person["email"].strip().casefold()


def _linkedin(person):
    value = person["linkedin_url"].strip()
    return normalized_linkedin_profile(value) or value.casefold()


def _name(person):
    return " ".join(person["name"].casefold().split())


def _channels(person, account_id):
    return [(account_id, field, value) for field, value in (
        ("email", _email(person)), ("linkedin", _linkedin(person)),
    ) if value]


def select_source_contacts(people, apollo, evidence, rules, aliases):
    blocked_emails = _blocked_emails(people)
    selected = []
    claimed_channels = set()
    blocked = set()
    for person in people:
        person_id = person["id"]
        if person["origin"] == "demo" or (person_id not in apollo and person_id not in evidence):
            continue
        if any(row["account_id"] != person["account_id"] or row["source"] != "cvent"
               for row in evidence.get(person_id, ())):
            raise ValueError(f"Contact {person_id} has conflicting source event evidence.")
        channels = _channels(person, person["account_id"])
        if claimed_channels.intersection(channels):
            raise ValueError(f"Contact {person_id} has a conflicting source channel.")
        claimed_channels.update(channels)
        if contact_has_restriction(person) or saved_address_is_blocked(
            rules, blocked_emails, person["account_id"], person["account_domain"], person["email"],
            aliases=aliases.get(person["account_id"], ()),
        ):
            blocked.add(person_id)
        selected.append(person)
    return {"people": selected, "apollo": apollo, "evidence": evidence, "blocked": blocked}


def _retained_contact(source, target):
    channel_pairs = ((_email(source), _email(target)), (_linkedin(source), _linkedin(target)))
    if any(old and new and old != new for old, new in channel_pairs):
        raise ValueError(f"Contact {source['id']} has conflicting saved channels.")
    if source["unlocked"] and (
        not target["unlocked"] or any(old and old != new for old, new in channel_pairs)
    ):
        raise ValueError(f"Contact {source['id']} has a saved unlock that needs reconciliation.")
    if source["email_verified"] and (not target["email_verified"] or channel_pairs[0][0] != channel_pairs[0][1]):
        raise ValueError(f"Contact {source['id']} has a verified channel that needs reconciliation.")


def transfer_contact_data(store: ContactTransferStore, data, account_ids, evidence_ids, counts, *, apply):
    target_people = {row["id"]: row for row in store.people()}
    target_apollo = store.apollo_links()
    apollo_by_key = {row["apollo_id"]: row for row in target_apollo.values()}
    target_channels = defaultdict(set)
    for row in target_people.values():
        for channel in _channels(row, row["account_id"]):
            target_channels[channel].add(row["id"])
    event_names = defaultdict(set)
    linked_evidence = set()
    for person_id, source_id in store.evidence_links():
        linked_evidence.add((person_id, source_id))
        event_names[source_id, _name(target_people[person_id])].add(person_id)
    rules = store.rules()
    destination_accounts = defaultdict(list)
    for account in store.accounts():
        destination_accounts[account["canonical_id"]].append((account["id"], account["domain"]))
    blocked_emails = _blocked_emails(target_people.values())
    apollo_inserts = []
    evidence_inserts = []
    claimed_targets = set()
    for person in data["people"]:
        source_person_id = person["id"]
        if person["account_id"] not in account_ids:
            raise ValueError(f"Contact {source_person_id} has no account in the transfer.")
        account_id = account_ids[person["account_id"]]
        linked = data["apollo"].get(source_person_id)
        matches = set()
        if linked and (mapped := apollo_by_key.get(linked["apollo_id"])):
            if any(mapped[field] != linked[field] for field in ("legacy_company_id", "company_domain")):
                raise ValueError(f"Contact {source_person_id} has a conflicting Apollo company.")
            matches.add(mapped["person_id"])
        if account_id is not None:
            for channel in _channels(person, account_id):
                matches.update(target_channels[channel])
        evidence = []
        for row in data["evidence"].get(source_person_id, ()):
            if row["source_record_id"] not in evidence_ids:
                raise ValueError(f"Contact {source_person_id} has no mapped source event.")
            evidence_id = evidence_ids[row["source_record_id"]]
            evidence.append((evidence_id, row["observed_at"]))
            matches.update(event_names[evidence_id, _name(person)])

        if len(matches) > 1 or matches & claimed_targets:
            raise ValueError(f"Contact {source_person_id} matches conflicting destination contacts.")
        target_id = next(iter(matches), None)
        target = target_people.get(target_id)
        if target is not None and target["account_id"] != account_id:
            raise ValueError(f"Contact {source_person_id} matches a different destination account.")
        existing_link = target_apollo.get(target_id)
        if linked and existing_link and linked["apollo_id"] != existing_link["apollo_id"]:
            raise ValueError(f"Contact {source_person_id} has a conflicting Apollo identity.")

        if saved_address_is_blocked(
            rules, blocked_emails, account_id, person["account_domain"], person["email"],
            aliases=destination_accounts.get(account_id, ()),
        ) or (
            target is not None and contact_has_restriction(target)
        ):
            counts["contacts_held"] = counts.get("contacts_held", 0) + 1
            continue
        if source_person_id in data["blocked"]:
            if target is not None:
                raise ValueError(f"Contact {source_person_id} has a source restriction that needs reconciliation.")
            counts["contacts_held"] = counts.get("contacts_held", 0) + 1
            continue

        if target is not None:
            _retained_contact(person, target)
            claimed_targets.add(target_id)
        else:
            counts["people_added"] += 1
            counts["saved_unlocks_added"] += bool(person["unlocked"])
            if apply:
                target_id = store.add_person(account_id, person)
        if linked and existing_link is None:
            apollo_inserts.append((target_id, linked))
        for evidence_id, observed_at in evidence:
            if (target_id, evidence_id) not in linked_evidence:
                evidence_inserts.append((target_id, evidence_id, observed_at))

    counts["apollo_links_added"] += len(apollo_inserts)
    counts["contact_evidence_added"] += len(evidence_inserts)
    if apply and apollo_inserts:
        store.add_apollo_links(apollo_inserts)
    if apply and evidence_inserts:
        store.add_contact_evidence(evidence_inserts)
