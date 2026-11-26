"""Account profile facts and the audited organization-type decision."""

from typing import Protocol


ORGANIZATION_TYPES = (
    "Corporate", "Association", "Agency", "Organizer", "University", "Government", "Other", "Unknown",
)


class OrganizationTypeConflict(ValueError):
    pass


class OrganizationTypeStore(Protocol):
    def save_type(self, account_id, actor_id, previous, selected, observed): ...


def consistent_listing_value(listings, field):
    values = list(dict.fromkeys(
        value.strip() for listing in listings
        if isinstance(value := listing.get(field), str) and value.strip().casefold() not in {"", "not available", "n/a"}
    ))
    return values[0] if len(values) == 1 else None


def describe_profile(listings, qualification, *, cvent):
    platform = "Unknown"
    confidence = None
    if cvent:
        platform, confidence = "Cvent", "Confirmed by event evidence"
    elif qualification:
        prefix = "Registration platform - "
        platforms = {
            label.removeprefix(prefix) for item in qualification["signals"]
            if (label := str(item.get("label", ""))).startswith(prefix)
        }
        if len(platforms) == 1:
            platform, confidence = next(iter(platforms)), "Detected in saved website evidence"
    return {
        "platform": platform, "platform_confidence": confidence,
        "country": consistent_listing_value(listings, "country"),
        "members": consistent_listing_value(listings, "members"),
        "organization_types": ORGANIZATION_TYPES,
        "fit_score": qualification["score"] if qualification else None,
    }


def validate_organization_type(actor, selected):
    if actor["role"] != "admin":
        raise PermissionError("Organization type changes require an admin.")
    if selected not in ORGANIZATION_TYPES:
        raise ValueError("Choose an organization type from the list.")


def set_organization_type(store: OrganizationTypeStore, actor, account, expected, selected, observed):
    validate_organization_type(actor, selected)
    previous = account["organization_type"]
    if expected != previous:
        raise OrganizationTypeConflict("The account type changed. Reload the account before saving.")
    if previous != selected:
        store.save_type(account["id"], actor["id"], previous, selected, observed)
