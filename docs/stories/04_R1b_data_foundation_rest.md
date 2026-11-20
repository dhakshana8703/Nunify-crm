# Release 1b - Data Foundation, Remainder

## Context for the agent

SDRs are live (Releases 1a, 2a, 2b). This release completes the generalized, source-neutral model so new sources (association directories, agencies, trade show organizers) can plug in: review queue, source-neutral naming, type classification, sources and provenance, scheduled source runs in a background worker, event-organization roles, the full Accounts list and persona configuration by type. Target model is in `docs/architecture.md` sections 3-4. Must not disrupt the live SDR workspace; run migrations on a Neon branch first.

**Release goal:** every account is typed, every account knows its source(s), the Cvent scraper runs weekly on its own with health alerts, managers can filter and save views across type / platform / source / timing, and enrichment pulls the right personas for each type.

**Estimate:** 2-2.5 weeks. R1-12 first if association enrichment is needed urgently.

---

## R1-12 Persona configuration by type [migration]

**As an** admin, **I want** personas defined per account type, **so that** Apollo pulls the right people for associations vs corporates vs agencies.

**Acceptance criteria**

- [ ] Table `personas` (id, org_type, tier: P1 / P2, name, title_keywords[], seniorities[], departments[], active)
- [ ] Seeded from architecture doc section 4.3
- [ ] Apollo people search uses the personas matching the account's type; `unknown` type falls back to corporate personas
- [ ] Admin config screen to add / edit / deactivate personas
- [ ] Existing people keep their persona tag; new persona IDs mapped where obvious

**Touches:** personas, enrichment service, config screen.
**Depends on:** R1-06. Can be built first in this release; unblocks enrichment of association accounts.

---

## R1-04 Extraction confidence and review queue [migration]

**As an** admin, **I want** low-confidence extractions flagged for review, **so that** bad data does not reach reps.

**Acceptance criteria**

- [ ] Rule-based validation runs on every extracted record (no LLM). A record is flagged when any rule fires:
  - end date more than 14 days after start date, or end date before start date
  - event date more than 3 years in the future
  - organizer name looks like an email handle or slug: no spaces and over 15 characters, contains @, or matches the local part of an extracted email
  - organizer name is a generic word or the platform name (e.g. "Events", "Registration", "Cvent", "Home")
  - organizer name and website domain share no common token (possible mismatch)
  - missing website or missing organizer
- [ ] Each flag stores the rule name so the review queue shows why it was flagged
- [ ] Rules live in one validation module so new rules can be added without touching the scrapers
- [ ] Flagged records appear in an admin **Review queue** with the field, extracted value, source URL and suggested fix
- [ ] Admin actions: approve, edit and approve, reject (soft delete)
- [ ] Known bad records from the prototype (organizer "liachaptercommunications", event dated Sep 2026 -> Sep 2031) appear in the queue after backfill

**Touches:** events, accounts, review_queue, admin UI.
**Depends on:** R1-02.

---

## R1-05 Generalize naming

**As a** team, **we want** the app to be source-neutral, **so that** new sources plug in without confusion.

**Acceptance criteria**

- [ ] UI renamed: "Cvent Intelligence Pipeline" -> "Nunify Outbound"; "Companies" -> "Accounts"; "Dedicated Cvent Companies" -> "Accounts"; "Total Cvent Events" -> "Events"; "Scraper & Diagnostics" -> "Sources"
- [ ] Persona "Cvent Event Creators" renamed "Event page contact"
- [ ] Code / table renames done where cheap; where a table rename is risky, a view or alias is acceptable
- [ ] Existing Cvent scraper still works end to end after renames

**Touches:** UI copy, table names, routes.
**Depends on:** none (can run in parallel with R1-01).

---

## R1-07 Type classification backfill

**As a** manager, **I want** existing accounts classified by type, **so that** the right personas are pulled.

**Acceptance criteria**

- [ ] Rule-based classifier (no LLM), applied in this order; first match wins:
  1. Manual override set by an admin (never overwritten)
  2. Domain lists: known agency domains, known trade show organizer domains (seed lists in config, admin-editable)
  3. TLD: `.edu` / `.ac.*` -> university; `.gov` / `.gov.*` / `.mil` -> government
  4. Name keywords -> association: Association, Society, Institute, Council, Federation, Chamber, League, Alliance, Coalition, Academy, College of (as in "American College of Cardiology"), Union, Guild, Foundation, Board of
  5. Name / website title keywords -> agency: Events, Meetings, Productions, Experiential, DMC, Destination Management, Incentives, Agency, Event Management, Conferences (when not also matching association keywords)
  6. Name / title keywords -> organizer: Expo, Exhibitions, Trade Show, Fairs, Media Group (with event listings)
  7. `.org` domain with no keyword match -> association (low confidence)
  8. Everything else -> corporate (low confidence if no Apollo industry data)
- [ ] Apollo organization industry used as a tie-breaker when available (e.g. "nonprofit organization management" -> association; "events services" -> agency)
- [ ] Each classification stores `type_rule` (which rule matched) and confidence (high for rules 1-4, medium for 5-6, low for 7-8)
- [ ] Low-confidence classifications go to the Review queue (R1-04) for a quick admin confirm
- [ ] Keyword lists and domain lists stored in config, editable by admin, with a "re-run classification" button
- [ ] Run on all existing accounts; summary count by type shown to admin after the run
- [ ] New accounts classified automatically on creation

**Touches:** accounts, classification module, config, review_queue.
**Depends on:** R1-04, R1-06.

---

## R1-09 Sources model and provenance [migration]

**As an** admin, **I want** to know which source(s) each account came from, **so that** we can measure source ROI.

**Acceptance criteria**

- [ ] Tables `sources` (id, name, kind, config JSON, active), `source_runs` (id, source_id, started_at, finished_at, status, pages_fetched, accounts_new, accounts_merged, errors), `account_sources` (account_id, source_id, source_record_url, first_seen, last_seen)
- [ ] Cvent scraper registered as a source; every run writes a `source_runs` row
- [ ] Existing accounts backfilled into `account_sources` with the Cvent source
- [ ] Raw page content stored in `raw_pages` (run_id, url, content, hash, fetched_at) so re-extraction does not require re-scraping
- [ ] **Sources** screen: one card per source with last run, status, yield (new / merged), errors, run / stop buttons. Existing live console and single-URL extractor move under the Cvent source card.

**Touches:** sources, source_runs, account_sources, raw_pages, Sources screen.
**Depends on:** R1-02, R1-05.

---

## R1-10 Event-organization roles [migration]

**As a** manager, **I want** events linked to accounts with a role, **so that** agencies and their clients are both captured.

**Acceptance criteria**

- [ ] Table `event_orgs` (event_id, account_id, role: host / client / producer / sponsor / venue)
- [ ] Existing event -> company links migrated as role = host
- [ ] Role detection by rules (no LLM):
  - phrases in page text: "produced by", "managed by", "organized by", "in partnership with", "hosted by", "sponsored by", "powered by" followed by an organization name or link
  - organizer email domain differs from the brand domain in the event title / logo link -> organizer email domain = producer, brand = client
  - organizer email or domain on the known agency list (R1-07) -> producer
  - venue taken from the structured location field -> venue (not created as an account)
- [ ] When no role phrase is found, the single organizer is stored as host (current behaviour)
- [ ] Account page lists events with role badges

**Touches:** event_orgs, extractor, account detail.
**Depends on:** R1-06.

---

## R1-11 Accounts list view

**As a** manager, **I want** a filterable Accounts list, **so that** I can find and assign the right accounts.

**Acceptance criteria**

- [ ] Columns: Name, Domain, Type, Platform, Source(s), Next event, Events/yr, Members, Fit score, Owner, Stage, Leads unlocked / total
- [ ] Filters: Type, Platform, Source, Next event window (0-3 / 4-9 / 10-18 months / none), Owner (incl. Unassigned), Stage, Region, Fit score range, Show suppressed
- [ ] Default sort: fit score desc, then next event asc
- [ ] Bulk select: assign owner, change stage, disqualify, enrich (enrich respects suppression)
- [ ] Saved views: admin can save the current filter set with a name (used as "plays" in R5)
- [ ] Events list defaults to upcoming events (start date >= today); past events available via filter

**Touches:** Accounts screen, Events screen.
**Depends on:** R1-07, R1-09. Replaces the interim Companies list columns added in R1-06; the SDR My Accounts view (R2-06) gains the extra filters.

---

## R1-13 Scheduled source runs and background worker [migration]

**As an** admin, **I want** recurring scrapers to run automatically on a schedule inside the platform, **so that** new listings (e.g. new Cvent events each week) flow in without anyone running scripts by hand.

**Acceptance criteria**

- [ ] **Standard source interface:** every built-in scraper is a module implementing `run(source_config, cursor) -> {records, new_cursor, stats}`; records are written to `raw_records` staging and go through the same promotion path as uploads and the ingest API (normalize -> suppression -> match / merge -> classify). Adding a new scraper means adding one module, nothing else
- [ ] **Separate worker process:** scraping and crawling run in a background worker (separate Render background worker or cron job service on the same Neon database), never inside the web app process, so the SDR dashboard is never slowed by a run
- [ ] **Job queue** in Postgres (no extra infrastructure); jobs survive restarts; failed jobs retry up to 3 times with backoff
- [ ] **Schedule per source** set on the Sources screen: cron-style schedule (presets: daily, weekly on a chosen day, monthly), enabled / disabled toggle, "Run now" button, next run time shown
- [ ] **No overlapping runs:** a lock per source; a scheduled run is skipped (and logged) if the previous run is still going
- [ ] **Incremental runs:** each source stores a cursor (last seen date / page / ID); runs fetch only new or changed listings where the target site allows it; a full refresh can be triggered manually
- [ ] **Change detection:** each staged record is marked new / changed / unchanged by comparing a content hash with the last version; for events, changed start date or location updates the event and the account's `next_event_date`; listings not seen for 3 consecutive runs are marked `gone` (not deleted)
- [ ] **Health alerts** to admins (email and in-app): run failed after retries; run yield below 30% of the average of the last 5 runs; zero records returned. These usually mean the target site changed layout and the scraper needs fixing
- [ ] **Downstream effects:** new events on an existing account in the 4-9 month window create a task for the account owner (if assigned); new accounts enter at stage Discovered; run summary (new, changed, gone, merged, suppressed, errors) recorded in `source_runs` and shown on the Sources card
- [ ] Cvent scraper converted to the standard interface and scheduled **weekly**; existing single-URL extractor and live console keep working

**Out of scope:** running externally hosted scripts (those use the ingest API, R4-07).
**Touches:** worker service, job queue tables, sources (schedule, cursor, lock), raw_records, source_runs, Sources screen, notifications, deployment config.
**Depends on:** R1-09.

---

## Release 1b demo checklist

- [ ] Review queue shows the known bad records (organizer "liachaptercommunications", event Sep 2026 -> Sep 2031) and can fix them
- [ ] UI is source-neutral (Accounts, Sources; no "Cvent Companies")
- [ ] Accounts list filters by Type = association and Platform = cvent and returns AARP, ABA, IIA, NFSA, SIFMA etc.
- [ ] Sources screen shows Cvent with run history and yield
- [ ] An agency-run event shows both the agency (producer) and the client (host) on the event
- [ ] Enriching an association pulls Executive Director / Meetings personas, not corporate CMO personas
- [ ] Cvent source scheduled weekly; "Run now" triggers a run in the worker while the SDR dashboard stays responsive; a second "Run now" during a run is refused
- [ ] A rerun picks up only new / changed listings; a changed event date updates the account's next event
- [ ] Forcing a run with zero results sends a health alert to the admin
- [ ] SDR workspace unaffected: Today, My Accounts, logging all still work
