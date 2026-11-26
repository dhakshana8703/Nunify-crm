"""Derive account facts from corroborated source evidence."""

import re
from calendar import monthrange
from datetime import date
from functools import lru_cache
from importlib.resources import files
from zoneinfo import ZoneInfo


COUNTRIES_BY_REGION = {
    "India": {
        "India": ("IN", "IND", "Republic of India"),
    },
    "US": {
        "United States": ("US", "USA", "United States of America"),
    },
    "Europe": {
        "Albania": ("AL", "ALB"), "Andorra": ("AD", "AND"), "Austria": ("AT", "AUT"),
        "Belarus": ("BY", "BLR"), "Belgium": ("BE", "BEL"),
        "Bosnia and Herzegovina": ("BA", "BIH"), "Bulgaria": ("BG", "BGR"),
        "Croatia": ("HR", "HRV"), "Cyprus": ("CY", "CYP"),
        "Czechia": ("CZ", "CZE", "Czech Republic"), "Denmark": ("DK", "DNK"),
        "Estonia": ("EE", "EST"), "Finland": ("FI", "FIN"), "France": ("FR", "FRA"),
        "Germany": ("DE", "DEU"), "Greece": ("GR", "GRC"), "Hungary": ("HU", "HUN"),
        "Iceland": ("IS", "ISL"), "Ireland": ("IE", "IRL"), "Italy": ("IT", "ITA"),
        "Latvia": ("LV", "LVA"), "Liechtenstein": ("LI", "LIE"), "Lithuania": ("LT", "LTU"),
        "Luxembourg": ("LU", "LUX"), "Malta": ("MT", "MLT"),
        "Moldova": ("MD", "MDA", "Republic of Moldova"), "Monaco": ("MC", "MCO"),
        "Montenegro": ("ME", "MNE"), "Netherlands": ("NL", "NLD", "The Netherlands"),
        "North Macedonia": ("MK", "MKD"), "Norway": ("NO", "NOR"), "Poland": ("PL", "POL"),
        "Portugal": ("PT", "PRT"), "Romania": ("RO", "ROU"), "Russia": ("RU", "RUS", "Russian Federation"),
        "San Marino": ("SM", "SMR"), "Serbia": ("RS", "SRB"), "Slovakia": ("SK", "SVK"),
        "Slovenia": ("SI", "SVN"), "Spain": ("ES", "ESP"), "Sweden": ("SE", "SWE"),
        "Switzerland": ("CH", "CHE"), "Ukraine": ("UA", "UKR"), "Vatican City": ("VA", "VAT", "Holy See"),
        "United Kingdom": ("GB", "GBR", "UK", "Great Britain", "England", "Scotland", "Wales", "Northern Ireland"),
    },
    "Middle East": {
        "Bahrain": ("BH", "BHR"), "Egypt": ("EG", "EGY"), "Iran": ("IR", "IRN"),
        "Iraq": ("IQ", "IRQ"), "Israel": ("IL", "ISR"), "Jordan": ("JO", "JOR"),
        "Kuwait": ("KW", "KWT"), "Lebanon": ("LB", "LBN"), "Oman": ("OM", "OMN"),
        "Palestine": ("PS", "PSE", "State of Palestine"), "Qatar": ("QA", "QAT"),
        "Saudi Arabia": ("SA", "SAU", "KSA"), "Syria": ("SY", "SYR", "Syrian Arab Republic"),
        "Türkiye": ("TR", "TUR", "Turkey"), "United Arab Emirates": ("AE", "ARE", "UAE"),
        "Yemen": ("YE", "YEM"),
    },
    "Other": {
        "Argentina": ("AR", "ARG"), "Australia": ("AU", "AUS"), "Brazil": ("BR", "BRA"),
        "Canada": ("CA", "CAN"), "China": ("CN", "CHN", "PRC"), "Colombia": ("CO", "COL"),
        "Hong Kong": ("HK", "HKG"), "Indonesia": ("ID", "IDN"), "Japan": ("JP", "JPN"),
        "Kenya": ("KE", "KEN"), "Malaysia": ("MY", "MYS"), "Mexico": ("MX", "MEX"),
        "New Zealand": ("NZ", "NZL"), "Nigeria": ("NG", "NGA"), "Pakistan": ("PK", "PAK"),
        "Philippines": ("PH", "PHL"), "Singapore": ("SG", "SGP"), "South Africa": ("ZA", "ZAF"),
        "South Korea": ("KR", "KOR", "Republic of Korea"), "Taiwan": ("TW", "TWN"),
        "Thailand": ("TH", "THA"), "Vietnam": ("VN", "VNM", "Viet Nam"),
    },
}
UNKNOWN_VALUES = {"", "na", "none", "unknown", "unspecified", "notspecified", "notavailable", "notapplicable",
                  "notprovided", "other"}
EXACT_MEMBER_COUNT = re.compile(r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)")
PLATFORM_PREFIX = "Registration platform - "


def _key(value):
    return "".join(character for character in value.casefold() if character.isalnum())


COUNTRY_ALIASES = {
    _key(alias): (country, region)
    for region, countries in COUNTRIES_BY_REGION.items()
    for country, aliases in countries.items()
    for alias in (country, *aliases)
}


@lru_cache(maxsize=1)
def _iso_countries():
    countries = {}
    for line in files("tzdata.zoneinfo").joinpath("iso3166.tab").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        code, name = line.split("\t")
        country = COUNTRY_ALIASES.get(_key(code), (name, "Other"))
        countries[_key(code)] = country
        countries[_key(name)] = country
    return countries


def business_day(observed, timezone_name):
    return observed.astimezone(ZoneInfo(timezone_name)).date()


def verified_event_date(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _event_facts(events, observed_on):
    by_event = {}
    for source_id, start_date in events:
        if not isinstance(source_id, str) or not source_id.strip() or source_id.startswith("company:"):
            continue
        dates = by_event.setdefault(source_id, set())
        if (valid_date := verified_event_date(start_date)) is not None:
            dates.add(valid_date)
    verified_dates = [next(iter(dates)) for dates in by_event.values() if len(dates) == 1]
    next_event = min((day for day in verified_dates if day >= observed_on), default=None)
    if not verified_dates:
        return bool(by_event), next_event, None
    previous_year = observed_on.year - 1
    last_day = monthrange(previous_year, observed_on.month)[1]
    cutoff = observed_on.replace(year=previous_year, day=min(observed_on.day, last_day))
    return bool(by_event), next_event, sum(cutoff < day <= observed_on for day in verified_dates)


def _platform(qualification, *, cvent):
    if cvent:
        return "cvent", None, "confirmed"
    platforms = {}
    for signal in (qualification or {}).get("signals") or ():
        label = signal.get("label") if isinstance(signal, dict) else None
        if isinstance(label, str) and label.startswith(PLATFORM_PREFIX):
            name = label.removeprefix(PLATFORM_PREFIX).strip()
            key = _key(name)
            if key == "none" or key not in UNKNOWN_VALUES:
                platforms[key] = min(name, platforms.get(key, name))
    if len(platforms) != 1:
        return "unknown", None, "unknown"
    key, detail = next(iter(platforms.items()))
    if key == "none":
        return "none", None, "confirmed"
    if key == "cvent":
        return "cvent", None, "detected"
    return "other_detected", detail, "detected"


def _location(listings):
    countries = {}
    for listing in listings:
        value = listing.get("country")
        if not isinstance(value, str):
            continue
        key = _key(value)
        if key in UNKNOWN_VALUES:
            continue
        if (known := COUNTRY_ALIASES.get(key) or _iso_countries().get(key)) is not None:
            countries[_key(known[0])] = known
    return next(iter(countries.values())) if len(countries) == 1 else (None, None)


def _members(listings):
    counts = set()
    for listing in listings:
        value = listing.get("members")
        if isinstance(value, str) and EXACT_MEMBER_COUNT.fullmatch(value.strip()):
            digits = value.strip().replace(",", "")
            value = int(digits) if len(digits) <= 10 else None
        if type(value) is int and 0 <= value <= 2_147_483_647:
            counts.add(value)
    return next(iter(counts)) if len(counts) == 1 else None


def derive_account_facts(*, observed_on, events, listings, qualification=None, cvent_roster=False):
    listings = tuple(listings)
    cvent_event, next_event, frequency = _event_facts(events, observed_on)
    platform, detail, confidence = _platform(qualification, cvent=cvent_event or cvent_roster)
    country, region = _location(listings)
    return {
        "platform": platform,
        "platform_detail": detail,
        "platform_confidence": confidence,
        "next_event_date": next_event.isoformat() if next_event else None,
        "events_per_year": frequency,
        "members": _members(listings),
        "country": country,
        "region": region,
    }
