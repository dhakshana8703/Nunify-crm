"""Account scope and visibility independent of session handling and storage."""

from typing import Protocol


STAFF = frozenset({"admin", "manager"})
SDR_ACCOUNT_SCOPE = frozenset({"Owned", "Coverage"})


class AccountNotFound(LookupError):
    pass


class AccountUnassigned(ValueError):
    pass


class SalesQueueDenied(PermissionError):
    pass


def require_sales_queue(user):
    if user["role"] == "ae":
        raise SalesQueueDenied()


def uses_unassigned_pool(user, selected):
    return selected == "pool" and user["role"] == "sdr"


class AccountAccessStore(Protocol):
    def account(self, account_id, today): ...

    def accounts(self, today): ...

    def coverage(self, account, observed): ...

    def restriction(self, account_id): ...


def account_relation(account, user, coverage_for):
    if account["owner_id"] == user["id"]:
        return "Owned"
    if user["role"] == "sdr" and account["owner_id"]:
        grant = coverage_for(account)
        if grant and grant["rep_id"] == user["id"]:
            return "Coverage"
    return "Assigned" if account["owner_id"] else "Unassigned"


def require_account(store: AccountAccessStore, user, account_id, today, observed, *, write=False):
    account = store.account(account_id, today)
    if account is None:
        raise AccountNotFound(account_id)
    if user["role"] not in STAFF and (
        user["role"] != "sdr"
        or account_relation(account, user, lambda row: store.coverage(row, observed)) not in SDR_ACCOUNT_SCOPE
    ):
        raise PermissionError("Account access is restricted.")
    if write and not account["owner_id"]:
        raise AccountUnassigned("Assign this account before recording work.")
    return account


def visible_accounts(store: AccountAccessStore, user, today, observed, *, include_suppressed=False):
    if not user or user["role"] == "ae":
        return []
    visible = []
    for account in store.accounts(today):
        if account["suppressed"] and not include_suppressed:
            continue
        relation = account_relation(account, user, lambda row: store.coverage(row, observed))
        if user["role"] not in STAFF and relation not in SDR_ACCOUNT_SCOPE:
            continue
        blocked = store.restriction(account["id"]) if account["suppressed"] else None
        visible.append(dict(account, relation=relation, blocked=blocked))
    return visible
