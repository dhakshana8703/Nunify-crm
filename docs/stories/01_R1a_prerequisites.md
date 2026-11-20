# Release 1a - Prerequisites for the SDR workspace

## Context for the agent

The app is currently a Cvent-only prototype: Events, People (Apollo-enriched), Companies, Scraper & Diagnostics, Team & Allocations. Postgres on Neon. SDRs need to start working accounts as soon as possible, so this short release covers only the data fixes and fields the SDR workspace (Release 2a) depends on. The rest of the data foundation (review queue, renaming, type classification, sources model, event roles, persona config, full Accounts list) comes later in Release 1b. Target model is in `docs/architecture.md` sections 3-4.

**Release goal:** no competitor staff in leads, no duplicate accounts, readable account names, owner / stage / next event on every account, and one credit number.

**Estimate:** 3-4 days.

---

## R1-01 Suppression list [migration]

**As an** admin, **I want** a suppression list of domains, emails and accounts, **so that** we never enrich or contact competitors, customers or opted-out people.

**Acceptance criteria**

- [ ] Table `suppression` (id, kind: domain / email / account, value, reason: competitor / customer / open_deal / opt_out / bounced / manual, source, created_by, created_at)
- [ ] Seeded with: cvent.com, cventevents.com, cvent-production.com, and the Cvent subdomain pattern `*.cventevents.com`
- [ ] Domain matching works on root domain and all subdomains
- [ ] Apollo enrichment and email unlock refuse suppressed accounts / people with a clear message
- [ ] Existing accounts matching suppression are flagged `suppressed = true` with `suppress_reason`; they are hidden from default views but visible with a "Show suppressed" filter
- [ ] Admin screen: list, add, remove suppression entries (CSV bulk add supported)

**Out of scope:** HubSpot-synced suppression (R3).
**Touches:** suppression, accounts, enrichment service, admin UI.
**Depends on:** none.

---

## R1-02 Root-domain normalization and merge [migration]

**As an** admin, **I want** accounts deduplicated by root domain, **so that** one organization is one record.

**Acceptance criteria**

- [ ] Function to normalize any URL / email domain to root domain (handles www, subdomains, country TLDs like .co.uk, .org.au)
- [ ] `accounts.root_domain` is unique
- [ ] One-time merge script: accounts sharing a root domain are merged into the oldest record; events, people, credits and allocations are re-pointed; merged-away IDs logged in `account_merges` (from_id, to_id, merged_at)
- [ ] Dry-run mode that outputs the list of proposed merges before executing
- [ ] Known cases resolved: national.alpfa.org -> alpfa.org; medc.cventevents.com suppressed via R1-01
- [ ] New records from any source go through normalize -> match -> merge-or-create
- [ ] Admin can manually merge two accounts from the UI

**Out of scope:** fuzzy name matching across different domains.
**Touches:** accounts, events link, people, account_merges.
**Depends on:** R1-01.

---

## R1-03 Clean account names

**As an** SDR, **I want** proper organization names, **so that** outreach looks professional.

**Acceptance criteria**

- [ ] Name resolution order: Apollo organization name > organizer name from event page > website title / og:site_name > domain-derived fallback
- [ ] `accounts.name_source` records which was used
- [ ] Backfill script run on all existing accounts (e.g. Theiia -> The Institute of Internal Auditors, Tdsynnex -> TD SYNNEX)
- [ ] Admin can edit the name inline; manual edits are never overwritten by backfills (`name_source = manual`)

**Touches:** accounts.
**Depends on:** R1-02.

---

## R1-06 Account type, platform, timing and stage fields [migration]

**As a** manager, **I want** each account to carry type, platform, next event date, fit score, stage and owner, **so that** we can segment and work accounts.

**Acceptance criteria**

- [ ] `accounts` gains: `type` (corporate / association / agency / organizer / university / government / other / unknown), `platform` (cvent / other_detected / none / unknown), `platform_detail` (text, e.g. "Swoogo"), `platform_confidence`, `next_event_date`, `events_per_year`, `members`, `fit_score`, `stage`, `owner_id`, `country`, `region` (India / US / Europe / Middle East / Other)
- [ ] `stage` values per architecture doc section 4.2 (Discovered ... Nurture), default Discovered
- [ ] `owner_id` populated from the existing Team allocations; allocations UI writes to `owner_id` from now on
- [ ] `next_event_date` computed as the earliest event with start date >= today, recomputed nightly and on event insert
- [ ] All Cvent-sourced accounts set to `platform = cvent`, `platform_confidence = confirmed`
- [ ] `type` defaults to `unknown`; populated later by the rule-based classifier in Release 1b (R1-07). Admin can set it manually in the meantime
- [ ] Columns Owner, Stage and Next event added to the existing Companies list, with filters for Owner (incl. Unassigned) and Stage

**Touches:** accounts, allocation logic.
**Depends on:** R1-02.

---

## R1-08 Single credit ledger [migration]

**As an** admin, **I want** one source of truth for Apollo credits, **so that** numbers match everywhere.

**Acceptance criteria**

- [ ] Table `credit_ledger` (id, user_id, account_id, person_id, action: org_match / people_search / email_unlock / phone_unlock, credits, at)
- [ ] Every Apollo call that spends credits writes exactly one ledger row
- [ ] Header badge, stats card and Team screen all read from the ledger (fixes the 62 vs 66 mismatch)
- [ ] Historical credits migrated; any unreconcilable difference recorded as a single "opening balance adjustment" row
- [ ] Per-user daily credit total available via API (used in R2 quotas)

**Touches:** credit_ledger, enrichment service, header, stats, team screen.
**Depends on:** none.

---

## Release 1a demo checklist

- [ ] No cvent.com / cventevents.com people visible in default People view; enrichment on them is blocked
- [ ] No two accounts share a root domain (national.alpfa.org merged into alpfa.org)
- [ ] Account names are human-readable for the top 50 accounts by events
- [ ] Every account has owner (or Unassigned), stage and next event date; existing allocations preserved
- [ ] Credits show the same number in header, stats card and Team screen
- [ ] Existing Cvent scraper, enrichment and SDR login still work
