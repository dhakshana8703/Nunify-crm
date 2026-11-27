"""Qualification, outreach rules and credit-controlled enrichment use cases."""

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from crm_core.access import STAFF
from crm_core.credit_ledger import parse_credits
from crm_core.suppression import normalize_domain_rule
from crm_core.suppression_metadata import REASON_CODES
from organization_identity import is_platform_domain, root_domain

QUALIFICATION_SCOPES = {
    "all": "All saved-source accounts", "cvent": "Cvent",
    "directory": "Directory of Associations", "both": "Both sources",
}
QUALIFICATION_BATCH_SIZE = 50
ACCOUNT_STAGES = (
    "discovered", "qualified", "disqualified", "enriched", "assigned",
    "working", "engaged", "positive", "handed_off", "nurture",
)
FIT_STAGES = {"discovered", "qualified", "disqualified"}
DEFAULT_RULES = {
    "conference_terms": ["conference", "annual meeting", "summit", "convention", "expo", "symposium"],
    "platform_domains": {"cvent.com": "Cvent", "cventevents.com": "Cvent", "swoogo.com": "Swoogo",
                         "eventbrite.com": "Eventbrite", "whova.com": "Whova"},
    "qualified_at": 40,
    "auto_at": 60,
    "daily_auto_budget": 10,
    "auto_enabled": False,
    "persona_titles": {"association": "Director of Meetings", "corporate": "Head of Events",
                       "agency": "Head of Production", "organizer": "Event Director",
                       "university": "Director of Conferences", "other": "Event Lead"},
}
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class GrowthInvalid(ValueError):
    pass


class GrowthConflict(ValueError):
    pass


class GrowthRestricted(ValueError):
    pass


class GrowthMissing(ValueError):
    pass


class EnrichmentProvider(Protocol):
    def __call__(self, job: dict, *, idempotency_key: str) -> dict: ...


class GrowthStore(Protocol):
    def atomic(self): ...
    def rules(self): ...
    def save_rules(self, config): ...
    def evidence(self, account_id): ...
    def save_qualification(self, account_id, score, signals, observed): ...
    def work_state(self, account_id): ...
    def apply_stage(self, account_id, stage, expected_revision): ...
    def qualification(self, account_id): ...
    def active_qualification_job(self): ...
    def create_qualification_job(self, user_id, scope, config, observed, day, planning): ...
    def qualification_candidates(self): ...
    def save_qualification_targets(self, job_id, account_ids): ...
    def qualification_status(self, job_id): ...
    def requeue_qualification_job(self, job_id): ...
    def qualification_targets(self, job, limit): ...
    def finish_qualification_batch(self, job, **progress): ...
    def fail_qualification_batch(self, job, message): ...
    def account(self, account_id, day): ...
    def account_relation(self, account, actor, observed): ...
    def restriction_for(self, account_id, person_id=None, *, candidate_email=None): ...
    def person(self, account_id, person_id): ...
    def has_credit_job(self, account_id, action, person_id): ...
    def daily_quota(self, user_id): ...
    def replace_daily_quota(self, user_id, quota): ...
    def credit_usage(self, user_id, day): ...
    def auto_usage(self, day): ...
    def reserve_credit_job(self, job): ...
    def claimable_credit_jobs(self, observed, *, job_id=None): ...
    def hold_credit_job(self, job_id, reason, *, release=False): ...
    def claim_credit_job(self, job, token, expires_at, *, may_spend=False): ...
    def owns_credit_job(self, job_id, token): ...
    def charge_credit(self, job, observed, credits=1, *, confirmed=False): ...
    def save_unlocked_email(self, person_id, email, *, verified=True): ...
    def save_enriched_contact(self, account_id, name, title, *, apollo_person_id=None): ...
    def complete_credit_job(self, job_id, note=None): ...
    def fail_credit_job(self, job_id, token, reason): ...
    def credit_job(self, job_id): ...
    def retry_credit_job(self, job_id): ...
    def account_exists(self, account_id): ...
    def save_rule(self, kind, value, reason, actor_id, observed, *, source, reason_note): ...
    def remove_rule(self, rule_id, observed): ...
    def refresh_suppressed_accounts(self): ...
    def source_funnel(self): ...
    def audit(self, actor_id, account_id, action, details, observed): ...


def local_day(user, observed):
    return observed.astimezone(ZoneInfo(user["timezone"])).date()


def utc_day(observed):
    return observed.astimezone(timezone.utc).date().isoformat()


def _offset_months(day, count):
    year, month = divmod(day.year * 12 + day.month - 1 + count, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def fit_satisfies(qualification, config):
    return bool(qualification and (qualification["override"] if qualification["override"] is not None
                                   else qualification["score"] >= config["qualified_at"]))


def require_admin(user):
    if user["role"] != "admin":
        raise GrowthRestricted()


def account_credit_access(store: GrowthStore, user, qualification, config, *, observed):
    remaining = None
    if user["role"] == "sdr":
        day = local_day(user, observed).isoformat()
        remaining = max(0, store.daily_quota(user["id"]) - store.credit_usage(user["id"], day))
    return {
        "can_approve": user["role"] in STAFF,
        "can_enrich": fit_satisfies(qualification, config),
        "auto_at": config["auto_at"],
        "remaining": remaining,
    }


def _stage(store, account_id, fit):
    state = store.work_state(account_id)
    stage = _next_fit_stage(state, fit)
    if stage is not None:
        store.apply_stage(account_id, stage, state["revision"] if state is not None else None)


def _next_fit_stage(work, fit):
    target = "qualified" if fit else "disqualified"
    if work is None:
        return target
    if work["owner_id"] is None and work["stage"] in FIT_STAGES and work["stage"] != target:
        return target
    return None


def qualification_target(candidate, scope, config):
    if scope in {"cvent", "both"} and not candidate["has_cvent"]:
        return False
    if scope in {"directory", "both"} and not candidate["has_directory"]:
        return False
    return candidate["score"] is None or _next_fit_stage(candidate, fit_satisfies(candidate, config)) is not None


def score_qualification(store: GrowthStore, account, *, observed, day=None, config,
                        headings=(), links=(), page_url=None, planning=(4, 9), update_stage=True):
    account_id = account["id"]
    evidence = store.evidence(account_id)
    cvent = [row for row in evidence if row["source"] == "cvent"]
    directories = [row for row in evidence if row["source"] == "directory"]
    headings = [(row["details"].get("event_name") or "", row["source_url"]) for row in cvent] + [
        (heading, page_url) for heading in headings
    ]
    signals = []

    def add(label, points, url=None):
        signals.append({"label": label, "points": points, "url": url})

    platform = ("Cvent", cvent[0]["source_url"]) if cvent else None
    for link in links:
        if not link.startswith(("https://", "http://")):
            continue
        try:
            host = urlsplit(link).hostname or ""
        except ValueError:
            continue
        for domain, name in config["platform_domains"].items():
            if (host == domain or host.endswith("." + domain)) and (platform is None or name == "Cvent"):
                platform = (name, page_url)
    if platform:
        add(f"Registration platform - {platform[0]}", 30 if platform[0] == "Cvent" else 15, platform[1])
    for heading, link in headings:
        if any(re.search(r"\b" + re.escape(term) + r"\b", heading, re.I) for term in config["conference_terms"]):
            add(f"Event - {heading}", 20, link)
            break
    if cvent and directories:
        add("Appears in two sources", 10)
    for row in directories:
        value = str(row["details"].get("members") or "").replace(",", "")
        match = re.search(r"\d+", value)
        if match and int(match[0]) >= 1000:
            add("Directory membership", 15 if int(match[0]) >= 10000 else 8, row["source_url"])
            break
    day = day or observed.date()
    start, end = (_offset_months(day, months).isoformat() for months in planning)
    if account.get("next_event") and start <= account["next_event"] <= end:
        add("Next event in the planning window", 20)
    score = sum(signal["points"] for signal in signals)
    store.save_qualification(account_id, score, signals, observed)
    if update_stage:
        _stage(store, account_id, fit_satisfies(store.qualification(account_id), config))
    return score


def queue_qualification_job(store: GrowthStore, user, scope, observed, planning):
    if scope not in QUALIFICATION_SCOPES:
        raise GrowthInvalid("Choose a valid qualification source.")
    if store.active_qualification_job():
        return None
    config = store.rules()
    job_id = store.create_qualification_job(user["id"], scope, config, observed, local_day(user, observed), planning)
    account_ids = [candidate["account_id"] for candidate in store.qualification_candidates()
                   if qualification_target(candidate, scope, config)]
    store.save_qualification_targets(job_id, account_ids)
    count = len(account_ids)
    store.audit(user["id"], None, "qualification_job_queued", {"job_id": job_id, "scope": scope, "accounts": count}, observed)
    return job_id


def retry_qualification_job(store: GrowthStore, user, job_id, observed):
    job = store.qualification_status(job_id)
    if not job or job["state"] != "failed":
        raise GrowthConflict("Only a failed qualification job can be retried.")
    if store.active_qualification_job():
        raise GrowthConflict("Wait for the active qualification job to finish before retrying.")
    store.requeue_qualification_job(job_id)
    store.audit(user["id"], None, "qualification_job_retried", {"job_id": job_id}, observed)


def run_qualification_batch(store: GrowthStore, scorer, *, clock, limit):
    job = None
    try:
        with store.atomic():
            job = store.active_qualification_job()
            if job is None:
                return None
            targets = store.qualification_targets(job, limit)
            config = job["config"]
            observed = datetime.fromisoformat(job["observed_at"])
            day = date.fromisoformat(job["observed_day"])
            planning = (job["planning_start"], job["planning_end"])
            scored = qualified = suppressed = 0
            for account_id in targets:
                if store.restriction_for(account_id):
                    suppressed += 1
                    continue
                if store.qualification(account_id) is None:
                    scorer(store.account(account_id, job["observed_day"]), observed=observed, day=day, config=config,
                           planning=planning, update_stage=False)
                    scored += 1
                fit = fit_satisfies(store.qualification(account_id), config)
                _stage(store, account_id, fit)
                qualified += int(fit)
            processed = job["processed"] + len(targets)
            done = processed == job["total"]
            cursor = targets[-1] if targets else job["last_account_id"]
            store.finish_qualification_batch(job, status="done" if done else "running", processed=processed,
                                             cursor=cursor, scored=scored, qualified=qualified,
                                             suppressed=suppressed, finished_at=clock() if done else None)
    except Exception as error:
        if job is None:
            raise
        with store.atomic():
            store.fail_qualification_batch(job, f"Qualification failed ({type(error).__name__}). Retry to resume the unfinished batch.")
    return job["id"]


def _check_enrichment_restriction(store: GrowthStore, account_id, person_id):
    reason = store.restriction_for(account_id, person_id)
    if reason:
        action = "Email unlock" if person_id else "Enrichment"
        raise GrowthConflict(f"{action} is blocked by suppression ({reason}).")


def queue_enrichment(store: GrowthStore, account, person_id, user, *, observed, auto=False, manual_request=False):
    account_id = account["id"]
    _check_enrichment_restriction(store, account_id, person_id)
    config = store.rules()
    qualification = store.qualification(account_id)
    if (manual_request and user["role"] == "sdr" and not person_id
            and qualification and qualification["score"] < config["auto_at"]):
        raise GrowthRestricted("A manager must approve enrichment for this score.")
    if not fit_satisfies(qualification, config):
        raise GrowthConflict("Qualify the account before using enrichment credits.")
    if person_id:
        person = store.person(account_id, person_id)
        if not person or not (person["origin"] == "apollo_offline" or person["apollo_person_id"]):
            raise GrowthConflict("Choose a contact returned by the enrichment provider.")
        if person["unlocked"]:
            return False
    action = "email_unlock" if person_id else "people_search"
    if store.has_credit_job(account_id, action, person_id):
        return False
    day, budget_day = local_day(user, observed).isoformat(), utc_day(observed)
    if user["role"] == "sdr" and store.credit_usage(user["id"], day) >= store.daily_quota(user["id"]):
        raise GrowthConflict("Daily credit quota reached. Your quota resets at local midnight.")
    if auto and store.auto_usage(budget_day) >= config["daily_auto_budget"]:
        raise GrowthConflict("Daily automatic enrichment budget reached.")
    store.reserve_credit_job(dict(account_id=account_id, person_id=person_id, actor_id=user["id"], action=action,
                                  on_day=day, budget_day=budget_day, request_key=uuid4().hex, auto=auto,
                                  observed=observed))
    return True


def request_enrichment(store: GrowthStore, account, person_id, user, *, observed):
    return queue_enrichment(store, account, person_id, user, observed=observed, manual_request=True)


def manual_qualification(store: GrowthStore, account, user, scorer, *, clock):
    config = store.rules()
    observed = clock()
    score = scorer(account, observed=observed, day=local_day(user, observed), config=config)
    store.audit(user["id"], account["id"], "qualification", {"score": score}, clock())
    if config["auto_enabled"] and score >= config["auto_at"]:
        try:
            queue_enrichment(store, account, None, user, observed=clock(), auto=True)
        except GrowthConflict as error:
            return str(error)
    return None


def retry_enrichment(store: GrowthStore, user, job_id, observed):
    job = store.credit_job(job_id)
    if not job or job["state"] != "failed":
        raise GrowthConflict("Only a failed job can be retried.")
    if job["reconciliation_required"]:
        raise GrowthConflict("Apollo credit usage requires reconciliation before this job can be retried.")
    _check_enrichment_restriction(store, job["account_id"], job["person_id"])
    store.retry_credit_job(job_id)
    store.audit(user["id"], job["account_id"], "credit_retry", {"job_id": job_id}, observed)


def select_credit_job(store: GrowthStore, observed, *, live=False, job_id=None):
    config = store.rules()
    for row in store.claimable_credit_jobs(observed, job_id=job_id):
        job = dict(row)
        if not live and job["apollo_person_id"]:
            continue
        if job["attempts"] == 0:
            actor_day, budget_day = local_day(job, observed).isoformat(), utc_day(observed)
            if (job["role"] == "sdr" and actor_day != job["on_day"]
                    and store.credit_usage(job["actor_id"], actor_day) >= job["daily_credit_quota"]):
                continue
            if (job["auto"] and budget_day != job["budget_day"]
                    and store.auto_usage(budget_day) >= config["daily_auto_budget"]):
                continue
            job.update(on_day=actor_day, budget_day=budget_day)
        return job
    return None


def enrich_next(store: GrowthStore, provider: EnrichmentProvider, *, clock, job_id=None):
    live = getattr(provider, "requires_confirmation", False) is True
    with store.atomic():
        observed = clock()
        job = select_credit_job(store, observed, live=live, job_id=job_id)
        if not job:
            return None
        job_id, account_id, person_id = job["id"], job["account_id"], job["person_id"]
        account = store.account(account_id, observed.date().isoformat())
        actor = {"id": job["actor_id"], "role": job["role"]}
        lost_access = not job["active"] or job["role"] == "ae" or (
            job["role"] == "sdr" and store.account_relation(account, actor, observed) not in {"Owned", "Coverage"}
        )
        if lost_access:
            store.hold_credit_job(job_id, "The requester is no longer authorized to enrich this account.")
            return job_id
        restriction = store.restriction_for(account_id, person_id)
        if restriction:
            store.hold_credit_job(job_id, f"Suppression blocked provider delivery ({restriction}).")
            return job_id
        titles = store.rules()["persona_titles"]
        job["title"] = titles.get(job["organization_type"].casefold(), titles["other"])
        job["domain"] = account["root_domain"] or account["domain"]
        if live and (not root_domain(job["domain"]) or is_platform_domain(job["domain"])):
            store.hold_credit_job(job_id, "Apollo requires a verified organization domain.")
            return job_id
        if live and person_id and not job["apollo_person_id"]:
            store.hold_credit_job(job_id, "Email unlock requires a saved Apollo person ID.")
            return job_id
        token = uuid4().hex
        store.claim_credit_job(job, token, observed + timedelta(minutes=2), may_spend=live and bool(person_id))

    try:
        result = provider(job, idempotency_key=job["request_key"])
        if live:
            credits = parse_credits(result.get("credits"))
            if credits < 0 or (not person_id and credits):
                raise GrowthInvalid("The Apollo credit confirmation is invalid.")
            with store.atomic():
                if not store.owns_credit_job(job_id, token):
                    return job_id
                if credits:
                    store.charge_credit(job, clock(), credits, confirmed=True)
        with store.atomic():
            if not store.owns_credit_job(job_id, token):
                return job_id
            if not live:
                store.charge_credit(job, clock())
            restriction = store.restriction_for(account_id, person_id, candidate_email=result.get("email"))
            if restriction:
                store.hold_credit_job(job_id, f"Suppression blocked publishing the enrichment result ({restriction}).",
                                      release=True)
                return job_id
            note = None
            if person_id:
                if not live or result.get("email"):
                    store.save_unlocked_email(person_id, result["email"], verified=(
                        not live or result.get("email_verified") is True
                    ))
                else:
                    note = "Apollo returned no verified company email."
            elif not live or result.get("apollo_person_id"):
                name, title = result["name"], result["title"]
                if not isinstance(name, str) or not name.strip() or not isinstance(title, str):
                    raise GrowthInvalid("The enrichment provider returned an invalid contact.")
                store.save_enriched_contact(account_id, name, title, apollo_person_id=(
                    result.get("apollo_person_id") if live else None
                ))
                work = store.work_state(account_id)
                if work is None or work["stage"] in {"discovered", "qualified"}:
                    store.apply_stage(account_id, "enriched", work["revision"] if work is not None else None)
            else:
                note = "Apollo returned no matching contacts."
            store.complete_credit_job(job_id, note)
    except Exception as error:
        with store.atomic():
            if not live:
                message = "Provider result pending reconciliation"
            else:
                message = "Apollo credit usage or result needs reconciliation" if person_id else "Apollo search failed"
            store.fail_credit_job(job_id, token, f"{message} ({type(error).__name__}).")
    return job_id


def change_daily_quota(store: GrowthStore, admin, user_id, quota, observed):
    if quota is None or not 0 <= quota <= 100_000:
        raise GrowthInvalid("Enter a nonnegative quota.")
    previous = store.replace_daily_quota(user_id, quota)
    if previous is None:
        raise GrowthMissing()
    store.audit(admin["id"], None, "sdr_quota_updated", {"user_id": user_id, "previous": previous, "quota": quota}, observed)


def change_qualification_rules(store: GrowthStore, user, terms_text, domains_text, budget_text, enabled, observed):
    terms = [term.strip() for term in terms_text.split(",") if term.strip()]
    if not terms or len(terms) > 100 or any(len(term) > 70 for term in terms):
        raise GrowthInvalid("Enter the conference terms, separated by commas.")
    try:
        domains = {}
        for line in domains_text.splitlines():
            if line.strip():
                domain, value = (part.strip() for part in line.split("=", 1))
                if not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", domain) or not value:
                    raise ValueError
                domains[domain] = value
        budget = int(budget_text)
        if not 0 <= budget <= 100_000:
            raise ValueError
    except ValueError:
        raise GrowthInvalid("Enter valid platform domains and a nonnegative budget.") from None
    config = dict(store.rules(), conference_terms=terms, platform_domains=domains, daily_auto_budget=budget,
                  auto_enabled=enabled)
    store.save_rules(config)
    store.audit(user["id"], None, "qualification_settings", {"budget": budget, "enabled": enabled}, observed)


def add_suppression_rules(store: GrowthStore, user, entries, *, source, clock):
    refresh_accounts = False
    for input_kind, input_value, input_reason, input_note in entries:
        kind = (input_kind or "").strip().lower()
        value = (input_value or "").strip()
        value = normalize_domain_rule(value) if kind == "domain" else value.lower().rstrip(".")
        reason = (input_reason or "").strip().lower()
        note = (input_note or "").strip()
        valid = kind == "domain" and bool(value)
        valid |= kind == "email" and bool(EMAIL.fullmatch(value))
        valid |= kind == "account" and value.isdecimal() and len(value) < 18 and store.account_exists(int(value))
        if not valid or len(value) > 254:
            raise GrowthInvalid("Provide a valid domain, email address, or account ID.")
        if reason not in REASON_CODES:
            raise GrowthInvalid("Choose a suppression reason from the list or use a listed reason code in the CSV.")
        if len(note) > 180:
            raise GrowthInvalid("Use no more than 180 characters for a suppression note.")
        account_id = int(value) if kind == "account" else None
        if account_id is not None:
            value = str(account_id)
        observed = clock()
        rule_id = store.save_rule(kind, value, reason, user["id"], observed, source=source, reason_note=note)
        if rule_id is not None:
            refresh_accounts |= kind in {"domain", "account"}
            store.audit(user["id"], account_id, "suppression_add", {"rule_id": rule_id}, observed)
    if refresh_accounts:
        store.refresh_suppressed_accounts()


def remove_suppression_rule(store: GrowthStore, user, rule_id, observed):
    rule = store.remove_rule(rule_id, observed)
    if rule is not None:
        if rule["kind"] in {"domain", "account"}:
            store.refresh_suppressed_accounts()
        store.audit(user["id"], None, "suppression_remove", {"rule_id": rule_id, **dict(rule)}, observed)


def audited_source_report(store: GrowthStore, user, observed):
    rows = store.source_funnel()
    store.audit(user["id"], None, "source_report_export", {"rows": len(rows)}, observed)
    return rows
