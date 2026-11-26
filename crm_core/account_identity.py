"""Corroboration policy shared by account writers."""

from organization_identity import normalized_words, organization_name_keys


def corroborated_account(candidates, name, *, identity_key=None):
    """Require one canonical account and a known identity key or corroborated name."""
    candidates = list(candidates)
    if len({row.get("canonical_id", row["id"]) for row in candidates}) != 1:
        return None
    if identity_key is not None:
        existing = next((row for row in candidates if row["identity_key"] == identity_key), None)
        if existing is not None:
            return existing
    names = organization_name_keys(name)
    current = next((row for row in candidates if names & organization_name_keys(row.get("name"))), None)
    if current is not None:
        return current
    exact = normalized_words(name)
    return next((row for row in candidates if exact and any(
        exact == normalized_words(previous) for previous in row.get("prior_names", ())
    )), None)
