# Release 4 - File Upload and Associations Source (Phase 4)

## Context for the agent

The model is now source-neutral (Release 1b). This release adds two things:

1. A **generic file upload source** (CSV / Excel), so any list - a purchased list, an event attendee list, a list a manager builds by hand, or the output of a scraper run outside the app - can be imported into the same Accounts pipeline. More sources will be added this way over time.
2. The **Directory of Associations** data (directoryofassociations.com, ~37,800 organizations with location, type, category and member band). This is **already scraped by a separate script** that runs outside this platform. This release imports that script's output; it does not build a new scraper or move the script into the app.
3. An **ingest API** so scrapers that run outside the platform (new or experimental ones) can push records straight into staging instead of exporting and uploading files.
4. **Moving the directory scraper into the platform** as a monthly scheduled source, using the scheduler and worker from R1-13.

**How sources enter the platform (all three paths share the same staging, dedupe, suppression and promotion logic):**

| Path | Use for | Example |
|---|---|---|
| Built-in scheduled source | Recurring, proven scrapers | Cvent weekly, directory monthly |
| Ingest API | Scrapers in development or run elsewhere | A new agency scraper in its first weeks |
| File upload | One-off lists | A purchased list, a conference attendee sheet |

A new scraper typically starts external and pushes through the API; once stable and recurring, it is ported into the platform as a scheduled source.

Unlike Cvent, these sources give organizations directly, not events. Each imported account is qualified by a light rule-based crawl of its own website to detect whether it runs a conference, which registration platform it uses, and when its next event is. Only qualified accounts are enriched via Apollo. See `docs/architecture.md` sections 3.2 and 4.4.

**Release goal:** any CSV / Excel list can be uploaded, previewed, deduplicated and imported; the scraped directory data flows through to qualified, scored, enriched association accounts, with associations already using Cvent flagged as displacement targets.

**Note:** R4-01 (file upload) depends only on R1-02 and R1-09 and can be pulled forward if a list needs importing earlier.

---

## R4-01 File upload source (CSV / Excel) [migration]

**As an** admin, **I want** to upload a CSV or Excel file of organizations (and optionally people), **so that** any list can enter the same pipeline as the scrapers.

**Acceptance criteria**

- [ ] Upload accepts .csv, .xlsx and .xls up to 50,000 rows; for multi-sheet Excel files, admin picks the sheet
- [ ] Each upload is attached to a source: pick an existing source or create a new one (name, kind = file_upload). Each upload creates a `source_runs` row with the file name, uploader and row counts
- [ ] Column mapping screen: map file columns to account fields (name, website / domain, type, country, state / region, city, members, phone, LinkedIn company URL) and optional people fields (first name, last name, full name, title, email, phone, LinkedIn URL)
- [ ] Auto-suggests mappings from header names (e.g. "Website", "URL", "Domain" -> website); unmapped columns can be kept and are stored in `account_attributes` under their header name
- [ ] Mappings saved as a template per source, so the next upload from the same source maps automatically
- [ ] Upload-level defaults: type (e.g. association), platform (e.g. unknown or cvent), region, tags; applied where the row has no value
- [ ] All rows first land in a staging table `raw_records` (run_id, row_number, data JSON, status); nothing touches accounts until the admin confirms
- [ ] **Preview before import** shows counts and a sample of each: new accounts, will merge into existing accounts, suppressed, invalid (no name and no domain, malformed domain), duplicate rows within the file
- [ ] Optional row filters on the preview screen (e.g. column "Members" >= 1000, column "Type" in [...]) so only a subset is promoted; filtered-out rows stay in staging and can be promoted later
- [ ] On confirm: rows go through normalize -> suppression -> match / merge (R1-02) -> classification (R1-07 unless type is mapped or defaulted); `account_sources` records the upload source and row
- [ ] People rows are created with `origin = upload`, email status `unverified`, linked to their account; duplicate email within the account is skipped
- [ ] Re-uploading the same file (same hash) warns before importing; re-importing updates existing records rather than duplicating
- [ ] Downloadable error report (row number, reason) for invalid and skipped rows
- [ ] Admin only (managers optional via permission); every upload written to the audit log

**Out of scope:** scheduled imports from Google Sheets or URLs.
**Touches:** sources, source_runs, raw_records, mapping templates, accounts, people, account_attributes, Sources screen.
**Depends on:** R1-02, R1-09.

---

## R4-02 Import the scraped Directory of Associations data

**As an** admin, **I want** the directory data we have already scraped loaded into the CRM with sensible filters, **so that** relevant associations enter the pipeline without importing all 37,800 listings as accounts.

**Acceptance criteria**

- [ ] Registered as a source "Directory of Associations" (kind = directory)
- [ ] First load: the external script's existing output (CSV / Excel) is uploaded through R4-01 with a saved mapping template for this source. From R4-08 onward the scraper runs inside the platform monthly; both land in the same staging and promotion flow
- [ ] Full dataset loaded into `raw_records`, keeping all scraped fields: name, website, city, state / country, association type, category, member band, listing URL, last updated
- [ ] Promotion filter applied when moving staging rows to accounts (admin-editable, saved on the source):
  - types: International, National, State
  - members: 1,000+
  - categories included: professional, trade, medical, industry, business, technology, finance, legal, engineering, education (admin-editable list)
  - categories excluded: clubs, alumni, fraternity / sorority, YMCA / YWCA, local, hobby, religious, support groups
- [ ] Promoted rows: `type = association`, `platform = unknown`; directory fields stored in `account_attributes` (directory_category, directory_type, member_band, listing_url); member band lower bound copied to `members` when no better figure exists
- [ ] Rows that do not pass the filter stay in staging and can be promoted later by changing the filter, without re-scraping
- [ ] Merge with existing Cvent accounts by root domain (e.g. AARP, ABA, IIA) adds the directory source and attributes without overwriting Cvent data
- [ ] Sources card shows: total staged, promoted, merged with existing, suppressed, not promoted
- [ ] If the directory is re-scraped later, a new load updates existing records and adds new listings only

**Touches:** sources, raw_records, accounts, account_attributes, account_sources.
**Depends on:** R4-01.

---

## R4-03 Website qualification crawl

**As a** manager, **I want** each association's website checked for events and platform, **so that** we qualify before spending Apollo credits.

**Acceptance criteria**

- [ ] For each account with a website and no crawl in the last 90 days, fetch: home page, and up to 5 pages whose URL or link text matches conference / annual meeting / convention / expo / events / register / summit
- [ ] Extract with rules and pattern matching (no LLM):
  - `runs_conference`: yes if any fetched page title, heading or nav link contains annual conference / annual meeting / convention / summit / expo / congress / symposium / forum; event name taken from that heading
  - next event start date and location: date patterns (e.g. "March 3-5, 2027", "3-5 March 2027", "2027-03-03") found near the event name or on the event page; take the earliest date >= today; location from text matching "City, State" or a venue line near the date. If no date parses, store `runs_conference = yes` with `next_event_date` empty
  - registration platform, detected from registration link domains and page source: cvent.com / cventevents.com -> cvent; also detect swoogo, eventbrite, a2zinc, mapyourshow, momentus, eventsair, bizzabo, hopin / ringcentral events, whova, accelevents, memberclicks, yourmembership, personify, fonteva, impexium, nimble ams, eshow, eventscribe; others recorded as other_detected with the domain
  - member count if stated: patterns like "over 25,000 members", "representing 4,000+ professionals", "membership of 12,000"; take the largest number adjacent to member / members / professionals / companies
  - exhibitor / sponsor program (yes / no): links or headings containing exhibit / exhibitor / sponsor / sponsorship / prospectus / booth
- [ ] Each finding saved to `account_signals` with value, evidence URL, observed date, confidence (high: platform from link domain; medium: keyword or pattern match; low: inferred)
- [ ] Patterns and keyword lists live in one config-driven module; a test suite of 20+ saved association pages checks extraction results so pattern changes do not regress
- [ ] Account fields updated: platform, platform_detail, platform_confidence, next_event_date, members, events_per_year (if derivable)
- [ ] Discovered events saved to `events` and linked via `event_orgs` (role = host)
- [ ] Crawl runs as a queued background job with concurrency limit; failures logged per account
- [ ] Also runnable on Cvent-sourced accounts to fill members / next event

**Touches:** crawler service, account_signals, accounts, events, event_orgs.
**Depends on:** R4-02 (works on any imported account).

---

## R4-04 Fit scoring

**As a** manager, **I want** accounts scored automatically, **so that** the best ones are enriched and assigned first.

**Acceptance criteria**

- [ ] Scoring weights stored in config (editable by admin), seeded from architecture doc section 4.4
- [ ] Score recomputed when signals change and nightly (timing points change as dates approach)
- [ ] Score breakdown visible on the account page (which signals contributed how many points)
- [ ] Stage set automatically: score >= 40 -> Qualified; < 40 -> Disqualified (reason: low fit), unless a manager has overridden

**Touches:** scoring service, config, accounts, account page.
**Depends on:** R4-03.

---

## R4-05 Credit-gated auto-enrichment

**As an** admin, **I want** qualified accounts enriched automatically within a budget, **so that** credits go only to good accounts.

**Acceptance criteria**

- [ ] Score >= 60: queued for Apollo enrichment automatically using association personas
- [ ] Score 40-59: appear in an "Approve enrichment" list for manager / admin with bulk approve
- [ ] Admin sets a daily auto-enrichment credit budget; queue pauses when reached and resumes next day
- [ ] Enrichment respects suppression and writes to the credit ledger
- [ ] Accounts with zero persona matches in Apollo flagged "no contacts found" for manual research

**Touches:** enrichment queue, config, accounts.
**Depends on:** R4-04, R1-12.

---

## R4-06 Cross-source merge visibility

**As an** SDR, **I want** to see when an account appears in multiple sources, **so that** I know it is a stronger target.

**Acceptance criteria**

- [ ] Account page header lists all sources with first-seen dates
- [ ] Accounts list "Source" filter supports "2+ sources"
- [ ] Merge of a directory record into an existing Cvent account keeps Cvent platform = confirmed and adds directory attributes (members, category)

**Touches:** account page, Accounts list.
**Depends on:** R4-02.

---

## R4-07 Ingest API for external scrapers [migration]

**As an** admin, **I want** external scraper scripts to push records into the CRM through an API, **so that** new sources can be built and run separately and linked without manual file uploads.

**Acceptance criteria**

- [ ] Per-source API key, created and revoked by admin from the Sources screen; keys stored hashed
- [ ] `POST /api/ingest/{source_id}/runs` starts a run and returns a run_id; `POST /api/ingest/{source_id}/runs/{run_id}/records` accepts batches of up to 1,000 records (JSON, same field names as the R4-01 mapping); `POST .../complete` closes the run
- [ ] Records land in `raw_records` staging; promotion uses the source's saved filter and defaults (same logic as R4-01 / R4-02), either automatically on run complete (setting per source) or after admin review
- [ ] Idempotent: a record with the same source-side ID or root domain updates the staged record instead of duplicating
- [ ] Validation errors returned per record in the response; run summary visible on the Sources card
- [ ] Rate limit per key; all calls logged
- [ ] Short integration doc in `docs/ingest-api.md` with an example payload, so any script (Python, Node) can be linked in under an hour

**Out of scope:** running external scripts from inside the platform.
**Touches:** API routes, sources, source_runs, raw_records, api_keys, Sources screen, docs.
**Depends on:** R4-01.

---

## R4-08 Directory of Associations as a scheduled built-in source

**As an** admin, **I want** the directory scraper moved into the platform and run monthly, **so that** new and updated associations flow in automatically like the Cvent source.

**Acceptance criteria**

- [ ] The existing external directory script is ported into a source module implementing the standard interface (R1-13); same fields as today's output
- [ ] Scheduled **monthly** (the directory publishes new listings monthly); incremental where possible using the directory's "recently updated" listings, with a full refresh available manually
- [ ] Rate limited (configurable delay, default 2-5 seconds random) and resumable from the last page if interrupted
- [ ] Writes to the same "Directory of Associations" source and staging used by R4-02, so earlier uploaded data and new runs merge cleanly (no duplicates)
- [ ] Promotion filter from R4-02 applied automatically on each run; newly promoted accounts are queued for the website qualification crawl (R4-03)
- [ ] Health alerts from R1-13 apply
- [ ] Once this is live, the external script is retired; the ingest API (R4-07) remains for future scrapers that start outside the platform

**Touches:** source module, sources config, raw_records, accounts.
**Depends on:** R1-13, R4-02.

---

## Release 4 demo checklist

- [ ] Upload a sample Excel file with a new source: mapping auto-suggested, preview shows new / merge / suppressed / invalid counts, import creates accounts and people, error report downloads
- [ ] Re-upload the same file: warning shown, no duplicates created
- [ ] Load the scraped directory data; Sources card shows staged vs promoted counts with the default filter; changing the filter promotes more rows without re-scraping
- [ ] Associations already in the Cvent data (e.g. AARP, ABA) merged, not duplicated, and show both sources
- [ ] Qualification crawl run on the batch; at least some associations detected with platform = cvent from their registration links
- [ ] Account page shows evidence links for conference, platform and next event
- [ ] Score breakdown visible; high scorers auto-enriched with association personas; mid scorers waiting for approval
- [ ] Daily auto-enrichment budget stops the queue when reached
- [ ] A test script posts 50 records to the ingest API; they appear in staging, promote according to the source filter, and re-posting the same records creates no duplicates
- [ ] Directory source scheduled monthly inside the platform; a manual run merges with the previously uploaded data without duplicates and queues new associations for the qualification crawl
