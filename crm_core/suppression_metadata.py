"""Canonical suppression reasons and interpretation of pre-R1-01 labels."""

REASON_LABELS = {
    "competitor": "Competitor",
    "customer": "Customer",
    "open_deal": "Open deal",
    "opt_out": "Opted out",
    "bounced": "Bounced",
    "manual": "Manual",
}
REASON_CODES = tuple(REASON_LABELS)
LEGACY_REASON_ALIASES = {
    "opted_out": "opt_out",
    "marked_do_not_contact": "opt_out",
}


def legacy_reason_metadata(reason):
    normalized = "_".join(reason.strip().lower().replace("-", " ").split())
    code = LEGACY_REASON_ALIASES.get(normalized, normalized)
    if code not in REASON_LABELS:
        code = "manual"
    return code, reason if reason != code else ""


def rule_reason_text(rule):
    note = rule["reason_note"] if "reason_note" in rule.keys() else ""
    return note if note and note.strip() else REASON_LABELS.get(rule["reason"], rule["reason"])
