"""Normalize the organization names and domains used to link source evidence."""

import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import idna

HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
EMAIL_WITH_DOMAIN = re.compile(r"[^@\s/\\?#:<>]+@([^@\s/\\?#:<>]+)\Z")
PUBLIC_SUFFIX_LIST = Path(__file__).with_name("public_suffix_list.dat")
PLATFORM_DOMAINS = (
    "cvent.com", "cventevents.com", "cvent-production.com", "eventbrite.com", "swoogo.com",
    "eventscloud.com", "eiseverywhere.com", "stova.io", "bizzabo.com", "whova.com", "zoom.us",
    "google.com", "facebook.com", "linkedin.com", "instagram.com", "youtube.com", "tinyurl.com", "bit.ly",
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
)


def matches_domain(host, rule):
    host = text_value(host)
    rule = text_value(rule).removeprefix("*.")
    # Keep the literal comparison available for any older stored values that fail DNS validation.
    host = normalize_dns_hostname(host) or host.casefold().rstrip(".")
    rule = normalize_dns_hostname(rule) or rule.casefold().rstrip(".")
    return bool(rule) and (host == rule or host.endswith("." + rule))


def domain_family(left, right):
    return bool(left and right and (matches_domain(left, right) or matches_domain(right, left)))


def text_value(value):
    return value.strip() if isinstance(value, str) else ""


def normalized_words(value):
    return " ".join(re.findall(r"[^\W_]+", text_value(value).casefold()))


def organization_name_keys(value):
    """Compare the full name and the verified expansion of a trailing acronym."""
    words = normalized_words(value)
    names = {words} if words else set()
    alias = re.fullmatch(r"(.+?)\s*\(([A-Z][A-Z0-9]{1,11})\)", text_value(value))
    if alias:
        base = normalized_words(alias[1])
        initials = "".join(word[0] for word in base.split() if word not in {"of", "the", "and", "for"})
        if initials == alias[2].lower():
            names.add(base)
    return names


def web_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return parsed
    except ValueError:
        pass
    return None


def _ascii_hostname(host):
    return idna.encode(host, uts46=True).decode("ascii").rstrip(".")


@lru_cache(maxsize=4096)
def normalize_dns_hostname(value: str) -> str:
    """Validate a bare DNS hostname and preserve every label in its ASCII spelling."""
    try:
        host = _ascii_hostname(value)
    except ValueError:
        return ""
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or labels[-1].isdigit():
        return ""
    return host if all(HOST_LABEL.fullmatch(label) for label in labels) else ""


def organization_host(value, *, strip_www=True):
    value = text_value(value)
    if not value:
        return ""
    parsed = web_url(value if "://" in value else f"https://{value}")
    if parsed is None:
        return ""
    try:
        if parsed.port == 0:
            return ""
    except ValueError:
        return ""
    host = normalize_dns_hostname(parsed.hostname)
    if strip_www and host.startswith("www.") and _registrable_domain(host) != host:
        return host.removeprefix("www.")
    return host


@lru_cache(maxsize=1)
def _public_suffix_rules():
    """Load the pinned ICANN and private suffix rules from the bundled snapshot."""
    rules = set()
    for line in PUBLIC_SUFFIX_LIST.read_text(encoding="utf-8").splitlines():
        rule = line.strip()
        if not rule or rule.startswith("//"):
            continue
        name = rule.removeprefix("!").removeprefix("*.")
        rules.add(rule.removesuffix(name) + _ascii_hostname(name))
    return frozenset(rules)


def _registrable_domain(host):
    rules = _public_suffix_rules()
    labels = host.split(".")
    # The implicit wildcard treats the final label as the suffix when no rule matches.
    suffix_start = len(labels) - 1
    for start in range(suffix_start, -1, -1):
        suffix = ".".join(labels[start:])
        if "!" + suffix in rules:
            return suffix
        if suffix in rules or "*." + ".".join(labels[start + 1:]) in rules:
            suffix_start = start
    return ".".join(labels[suffix_start - 1:]) if suffix_start else ""


def root_domain(value):
    """Return an ASCII registrable domain for a web URL, host, or email address."""
    value = text_value(value)
    if "://" not in value and "@" in value:
        email = EMAIL_WITH_DOMAIN.fullmatch(value)
        if not email:
            return ""
        value = email[1]
    host = organization_host(value, strip_www=False)
    return _registrable_domain(host) if host else ""


def is_platform_domain(host):
    return any(matches_domain(host, platform) for platform in PLATFORM_DOMAINS)
