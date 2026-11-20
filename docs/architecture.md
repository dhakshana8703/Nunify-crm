# Nunify Outbound Intelligence CRM - Architecture and Build Plan

**Purpose:** Turn the current Cvent Intelligence prototype into a multi-source, account-based outbound CRM: discover organizations that run events, qualify them, enrich the right people, work them through the SDR team, and hand positive conversations to AEs in HubSpot.

**Status:** Prototype live (Cvent source, Apollo enrichment, admin and SDR logins, account allocation). Sales workflow, multi-source model and HubSpot handoff not yet built.

---

## 1. Design principles

1. **One account model, many sources.** Every scraper is an entry point into the same Accounts table. Sources never get their own dashboard.
2. **Three dimensions per account, kept separate:**
   - **Type** - what the organization is (corporate, association, agency, organizer, university, other). Decides personas.
   - **Platform** - what event tech it uses (Cvent confirmed, other detected, unknown). Decides the play and message.
   - **Timing** - next known event date. Decides when to reach out.
3. **Qualify cheaply before spending credits.** Website crawl and scoring happen before Apollo. Credits are spent only on qualified accounts.
4. **Account-based ownership.** Accounts are assigned to one SDR; people inherit the owner.
5. **This tool owns pre-qualification; HubSpot owns the deal.** Status flows both ways.
6. **Do not build an email sender.** Integrate a sequencing tool and log activity back.
7. **Every fact has a source and a date.** Reps see why an account is on their list; stale data is visible.

---

## 2. Current state (prototype review, 16 Nov 2026)

### What works
- Cvent event discovery (Google Serper search + single-URL extractor) with a live engine console
- Extraction of event, organizer, website and organizer emails from event pages
- Company extraction from events (393 companies from 756 events)
- Apollo enrichment with P1 / P2 persona tiers, lock / unlock model and credit audit
- Admin and SDR logins, account allocation to SDRs (1 SDR, 50 accounts, 512 leads)
- Postgres (Neon) as the system of record, CSV exports

### Gaps and defects

| Area | Issue | Impact |
|---|---|---|
| Model | Everything is named and structured around Cvent ("Cvent Companies", "Cvent Event Creators" persona) | New sources cannot plug in cleanly |
| Model | No organization type; list already mixes corporates, associations (AARP, ABA, IIA, NFSA, AAMC, SIFMA), universities and agencies | Wrong personas, no segmentation |
| Model | No platform field; "is a Cvent customer" is implicit in the source | Directory-sourced Cvent users cannot be identified |
| Suppression | Cvent's own domains (cvent.com, cventevents.com) are a top account with 10 P1 leads, 3 unlocked | Credits spent on competitor staff |
| Suppression | No check against HubSpot customers or open deals | Risk of prospecting live customers |
| Dedupe | Subdomains not rolled up (national.alpfa.org, medc.cventevents.com) | Duplicate accounts |
| Data quality | Names derived from domains (Theiia, Tdsynnex, Esource) | Unprofessional in outreach |
| Data quality | Extraction errors (organizer = an email handle; event dated 2026 to 2031) | No confidence flag or review queue |
| Timing | Events list defaults to past dates; no "next event" per account | Timing signal unused |
| Workflow | Accounts have no owner, stage or score columns | Cannot filter "unassigned, high-fit" |
| Workflow | People status is only Locked / Unlocked (enrichment state) | No sales state, no activity log, no next action |
| Workflow | No campaigns, no HubSpot push | Pipeline stops at a contact list |
| Admin | Credits shown as 62 in header, 66 in card | Ledger inconsistency |

---

## 3. Target architecture

```
 SOURCES     Cvent events | Assoc. directory | Agencies | Trade shows | CSV / Excel upload
                 |
 DATA PIPELINE   v
   1 Ingest      raw pages + run log
   2 Extract     rule-based extraction to schema, validation flags
   3 Resolve     root domain, dedupe/merge, suppression   <--+
   4 Qualify     website crawl: events, platform, next      |
                 event date, members -> fit score           |
   5 Enrich      Apollo people by persona config,           |  HubSpot sync:
                 credit-gated                               |  customers and
                 |                                          |  open deals
 SALES WORK      v                                          |  (suppression)
   6 Assign      one owner per account                      |
   7 Engage      sequences, calls, LinkedIn, activity log   |
   8 Handoff     HubSpot company + contact + deal  -------->+
   9 Report      funnel by source, type, play, rep  <------ deal stages back
```

### 3.1 Layers

| # | Layer | Responsibility | Notes |
|---|---|---|---|
| 1 | Ingest | Three entry paths into one staging area: built-in scheduled scrapers (run by a background worker), ingest API for external scripts, CSV / Excel upload. Raw content stored unchanged; every run logged | Re-extraction without re-scraping; incremental runs with change detection |
| 2 | Extract | Rule-based extraction (parsers, patterns) to a fixed schema per source; validation rules flag suspect fields | Flagged records go to a review queue |
| 3 | Resolve | Normalize to root domain; merge into existing account; apply suppression list | Merge, never duplicate. Keep all sources on the account |
| 4 | Qualify | Crawl the account website for: runs a conference/expo, registration platform, next event date, member count | Cheap; runs before Apollo |
| 5 | Enrich | Apollo people search by persona config for the account type; unlock emails within quota | Credit ledger per user, per account |
| 6 | Assign | Admin/manager assigns accounts; people inherit | Exclusive ownership |
| 7 | Engage | Push contacts to sequencer; log calls, emails, LinkedIn touches and outcomes | Sequencer webhooks auto-log email activity |
| 8 | Handoff | On "positive", create/update HubSpot company, contact, deal; assign AE | Two-way: deal stage syncs back |
| 9 | Report | Funnel by source, type, platform, play and rep; credits per positive reply | Decides which sources get investment |

### 3.2 Sources

| Source | Gives | Default type | Platform signal | Status |
|---|---|---|---|---|
| Cvent event pages | Events, organizer, organizer emails | Mixed (classify) | Cvent confirmed | Live; to run weekly in-platform (Release 1b) |
| Directory of Associations (~37,800 orgs) | Name, location, type, category, member band | Association | Unknown until crawl | Scraped by a separate script today; first load by file upload, then moved in-platform to run monthly (Release 4) |
| Event agency lists | Agencies | Agency | Unknown | Later |
| Trade show / organizer lists | Organizers, shows | Organizer | Unknown | Later |
| CSV / Excel upload | Anything (organizations, optionally people) | Set on upload | Set on upload | Release 4; any list, any future source |
| Ingest API | Records pushed by external scraper scripts | Set per source | Set per source | Release 4; links separately run scrapers |

**How sources enter the platform.** Recurring, proven scrapers live in the platform as source modules with a standard interface, run on a per-source schedule by a background worker separate from the web app, with incremental runs, change detection, run locking and health alerts. Scrapers still in development, or hosted elsewhere, push records through the ingest API. One-off lists come in as CSV / Excel uploads. All three paths share the same staging, dedupe, suppression and promotion logic. A new scraper typically starts external and is ported in once it is stable and needs to run regularly.

**Directory pre-filter (first pull):** International, National and State associations; 1,000+ members; professional, trade, medical and industry categories. Exclude clubs, alumni, fraternities, YMCA, local chapters, hobby, religious and support-group categories. The directory is scraped by a separate script outside the platform; all scraped rows are staged and only rows passing the filter are promoted to accounts, so the filter can change later without re-scraping.

---

## 4. Data model

### 4.1 Core tables

| Table | Key fields | Notes |
|---|---|---|
| `sources` | id, name, kind, config | One row per scraper |
| `source_runs` | id, source_id, started_at, finished_at, pages, new_accounts, errors | Run history and yield |
| `raw_pages` | id, run_id, url, content, fetched_at, hash | Immutable |
| `accounts` | id, name, root_domain (unique), type, platform, platform_confidence, country, region, members, events_per_year, next_event_date, fit_score, stage, owner_id, hubspot_company_id, apollo_org_id, suppressed, suppress_reason | The single organization table |
| `account_sources` | account_id, source_id, source_record_url, first_seen, last_seen | Many-to-many provenance |
| `account_signals` | id, account_id, signal (e.g. `runs_conference`, `platform`, `member_count`), value, evidence_url, observed_at, confidence | Every fact with source and date |
| `account_attributes` | account_id, key, value | Type-specific fields (directory category, association tier, agency clients) |
| `events` | id, name, start_date, end_date, location, format, url, platform, source_id | |
| `event_orgs` | event_id, account_id, role (host, client, producer/agency, sponsor, venue) | One event can surface several accounts |
| `people` | id, account_id, name, title, persona_id, email, email_status, linkedin_url, phone, origin (event page, Apollo, manual), apollo_person_id, unlocked, outreach_status, owner_id (inherited), do_not_contact | |
| `personas` | id, org_type, tier (P1/P2), title_keywords, seniority, departments | Config, editable by admin |
| `suppression` | id, kind (domain, email, account), value, reason (competitor, customer, open deal, opt-out, bounced), source | Checked before enrichment and before sequencing |
| `users` | id, name, email, role, region, daily_unlock_quota, active | |
| `assignments` | account_id, user_id, assigned_by, assigned_at, ended_at | History of ownership |
| `campaigns` | id, name, play, channel, sequencer_id, owner_id, status | |
| `campaign_members` | campaign_id, person_id, added_by, status, added_at | |
| `activities` | id, person_id, account_id, user_id, type (email, call, LinkedIn, meeting, note), direction, outcome, body, occurred_at, source (manual, sequencer webhook) | Full touch timeline |
| `tasks` | id, user_id, account_id, person_id, due_at, kind, status | Next actions |
| `handoffs` | id, account_id, person_id, user_id, ae_id, hubspot_deal_id, pushed_at, payload, deal_stage, deal_stage_synced_at | Attribution back to source |
| `credit_ledger` | id, user_id, account_id, person_id, credits, action, at | Single source for all credit numbers |
| `audit_log` | id, user_id, action, entity, entity_id, before, after, at | Exports, deletes, reassignments |

### 4.2 Status models

**Account stage**

| Stage | Meaning | Set by |
|---|---|---|
| Discovered | Created from a source | System |
| Qualified | Passed score threshold after website crawl | System / admin override |
| Disqualified | Below threshold, wrong type, or suppressed | System / admin |
| Enriched | P1/P2 people pulled | System |
| Assigned | Has an owner | Admin / manager |
| Working | At least one contact in sequence or touched | System |
| Engaged | Any reply or connected call | System / SDR |
| Positive | Meeting booked or explicit interest | SDR |
| Handed off | Pushed to HubSpot | System |
| Nurture | Worked, no response; recycle after 90 days | System / SDR |

**Person outreach status:** Not contacted, In sequence, Replied, Connected (call), Positive, Not interested, Wrong person, Bounced, Opted out, Handed off.

**Definition of Positive (for handoff):** meeting booked with a P1/P2 contact, or a written request for a demo or pricing. Anything else stays as Engaged.

### 4.3 Personas by organization type

| Type | P1 (decision) | P2 (operational) |
|---|---|---|
| Corporate | CMO, VP / Head of Events, VP Marketing, Head of Field Marketing | Event Manager, Event Marketing Manager, Marketing Ops |
| Association | Executive Director / CEO, VP / Director of Meetings and Events, VP Education / Conferences | Meetings Manager, Conference Manager, Registration Manager, Membership Director |
| Agency | Founder / MD, Head of Production, Client Services Director | Senior Producer, Event Technology Manager |
| Organizer | CEO, Event Director, Head of Operations | Registration / Ops Manager, Exhibitor Sales Manager |
| University | Director of Conferences / Events, Continuing Education Director | Event Coordinator |

### 4.4 Scoring (initial, tune after first 500 outcomes)

| Signal | Points |
|---|---|
| Platform = Cvent confirmed | +30 |
| Platform = other paid platform detected | +15 |
| Runs an annual conference or expo | +20 |
| Next event 4-9 months out | +20 |
| Next event 1-3 months out | +5 (too late for this cycle; schedule follow-up) |
| Events per year 5+ | +10 |
| Association members 10,000+ | +15; 1,000-9,999 +8 |
| Agency (multiplier account) | +10 |
| In 2+ sources | +10 |
| Suppressed | Excluded |

Qualified threshold: 40. Enrich automatically at 60+; 40-59 on admin approval.

### 4.5 Plays (saved views that drive campaigns)

| Play | Filter | Message angle |
|---|---|---|
| Displacement - associations | Type = association, platform = Cvent | Switch from Cvent; member pricing, cost, ease |
| Displacement - corporate | Type = corporate, platform = Cvent | Switch from Cvent; AI, experience, cost |
| Agency multiplier | Type = agency | One platform across client events |
| Greenfield - associations | Type = association, platform unknown/none, 10k+ members | Modernize registration and conference |
| Timing | Any, next event 4-9 months | Planning-window outreach |

Type picks the persona, platform picks the message, next event date picks the timing.

---

## 5. Roles and permissions

### 5.1 Roles

- **Admin** - runs sources, configures personas and scoring, manages users, owns suppression and credits.
- **Sales Manager** (optional, can be merged with Admin initially) - assigns accounts, approves enrichment for mid-score accounts, sees team performance.
- **SDR** - works assigned accounts and contacts, logs activity, flags positives.
- **AE** - no login needed; works in HubSpot. Optional read-only view of the account history.

### 5.2 Permission matrix

| Capability | Admin | Manager | SDR | AE |
|---|---|---|---|---|
| Run / stop scrapers, add sources | Yes | No | No | No |
| Review extraction queue, merge accounts | Yes | Yes | No | No |
| Edit personas, scoring, suppression | Yes | No | No | No |
| View all accounts | Yes | Yes | Own + unassigned pool (read-only) | Handed-off accounts (read-only) |
| Assign / reassign accounts | Yes | Yes | No (can request from pool) | No |
| Trigger Apollo enrichment for an account | Yes | Yes | Own accounts, within daily quota | No |
| Unlock individual emails | Yes | Yes | Own accounts, within daily quota | No |
| Add contact manually | Yes | Yes | Own accounts | No |
| Edit contact (title, email, persona) | Yes | Yes | Own accounts | No |
| Mark wrong person / do not contact | Yes | Yes | Own accounts | No |
| Add contacts to a campaign | Yes | Yes | Own accounts, existing campaigns | No |
| Create campaigns | Yes | Yes | No | No |
| Log call / email / LinkedIn / note | Yes | Yes | Own accounts | No |
| Mark Positive (triggers HubSpot handoff) | Yes | Yes | Own accounts | No |
| Undo / reroute a handoff | Yes | Yes | No | No |
| Delete records | Yes (soft delete, audited) | No | No | No |
| Export CSV | Yes (audited) | Own team, audited | No | No |
| See credit spend | All | Team | Own | No |
| Manage users and passwords | Yes | No | No | No |

**SDR export is off by design.** The account list is the asset; exports are the main leakage path.

---

## 6. Screens

### 6.1 SDR view

| Screen | Contents | Actions |
|---|---|---|
| **Today (home)** | Task queue: follow-ups due, new replies, accounts with events entering the 4-9 month window, newly assigned accounts | Open account, complete task, snooze |
| **My Accounts** | Owned accounts with type, platform, next event, score, stage, contacts unlocked / total, last touch | Filter, sort, open, request more accounts |
| **Account page** | Header (type, platform, members, next event, sources); evidence (signals with links and dates); events list; contacts by persona; activity timeline | Enrich, unlock, add contact, add to campaign, log activity, mark positive, move to nurture |
| **Contact page** | Details, email status, LinkedIn, persona, outreach status, timeline | Log call / email / LinkedIn, change status, add to campaign, mark positive |
| **Log activity (modal)** | Type, outcome, note, next action date | Under 10 seconds to complete |
| **Mark Positive (modal)** | Required: meeting date or interest type, contact, context note, suggested AE | Submits handoff to HubSpot |
| **My stats** | Touches, replies, positives, handoffs, credits used this week | View only |

### 6.2 Admin / Manager view

| Screen | Contents | Actions |
|---|---|---|
| **Overview** | Funnel by source x type x play (discovered, qualified, enriched, assigned, working, engaged, positive, handed off, won); credits per positive; rep leaderboard | Drill down |
| **Accounts** | All accounts, all filters, saved plays | Bulk assign, bulk enrich, bulk disqualify, merge |
| **Assignment** | Unassigned qualified pool; rep capacity (accounts, open tasks) | Assign by rules (region, type) or manually; reassign |
| **Sources** | One card per source: last run, yield, errors, new accounts; run / stop; config | Run, schedule, re-extract |
| **Review queue** | Low-confidence extractions, possible duplicates, type classification conflicts | Approve, fix, merge |
| **Suppression** | Competitors, customers and open deals (synced from HubSpot), opt-outs, bounces | Add, remove, re-sync |
| **Config** | Personas per type, scoring weights, thresholds, quotas, Positive definition | Edit |
| **Campaigns** | Campaigns, plays, members, sequencer sync status, reply rates | Create, pause, add members |
| **Team** | Users, roles, quotas, credits | Add, deactivate, reset password |
| **Credits** | Ledger by user, account, source | Export |

---

## 7. Integrations

| System | Direction | What moves | When |
|---|---|---|---|
| Apollo | Out / in | Org match, people search by persona, email unlock | On enrichment action |
| Sequencer (Apollo Sequences, Instantly, lemlist or similar) | Out / in | Contacts into sequences; sends, opens, replies, bounces back as activities | On add to campaign; webhooks |
| LinkedIn | Manual | Touches logged by SDR | On action |
| HubSpot | Out | Company (match on domain), contact, deal in pipeline stage "SQL", owner = AE, note with account evidence and touch history, source and play | On Mark Positive |
| HubSpot | In | Customers, open deals (suppression); deal stage and amount (attribution) | Daily sync |
| Search / scraping (Serper, crawler) | In | Discovery and website crawl | Scheduled per source |

**AE routing on handoff:** by region first (India, US, Europe, Middle East), then round robin within region. Manager can override.

---

## 8. Build plan

### Phase 0 - Fix the data (1 week)
- Suppression table; seed with cvent.com, cventevents.com and other competitor domains; exclude from enrichment
- Root-domain normalization and merge of existing duplicates
- Company name cleanup (page title or Apollo name over domain)
- Extraction confidence flag and review queue for dates and organizer names
- Single credit ledger; one number everywhere
- **Done when:** no competitor staff in leads, no subdomain duplicates, credit numbers match

### Phase 1 - Generalize the model (1-2 weeks)
- Rename Cvent-specific entities: Accounts, People, Events, Sources
- Add `type`, `platform`, `next_event_date`, `fit_score`, `stage`, `owner_id` to accounts
- `account_sources`, `account_signals`, `event_orgs` with roles
- Classify the existing 393 accounts by type (rules + review)
- Set platform = Cvent for all Cvent-sourced accounts
- Persona config per type
- Background worker and per-source schedules; Cvent scraper runs weekly with incremental runs and health alerts
- **Done when:** Accounts list filters by type, platform, source, next event, owner and stage, and the Cvent source runs weekly on its own

### Phase 2 - SDR workflow (2 weeks)
- Today queue, My Accounts, Account page with evidence and timeline
- Person outreach status, activity logging, tasks / next actions
- Permission enforcement per section 5
- Daily unlock quota per SDR
- **Done when:** an SDR can work a full day without leaving the tool or using a spreadsheet

### Phase 3 - HubSpot handoff (1 week)
- Mark Positive modal; push company, contact, deal, note; AE routing
- Daily inbound sync: customers and open deals into suppression; deal stages into handoffs
- **Done when:** a positive creates a correctly owned HubSpot deal in under a minute, and its stage shows back in the tool

### Phase 4 - Associations source (1-2 weeks)
- CSV / Excel upload source with column mapping, preview and staging
- Import of the externally scraped directory data with promotion filter
- Ingest API for external scraper scripts
- Directory scraper moved into the platform as a monthly scheduled source
- Website qualification crawl: conference yes/no, registration platform detection, next event date, member count
- Scoring and auto-enrichment thresholds
- **Done when:** a filtered directory batch flows to qualified, scored, enriched accounts, with Cvent users flagged

### Phase 5 - Campaigns and reporting (1-2 weeks)
- Sequencer integration with webhook activity logging
- Plays as saved views; campaign creation from a play
- Admin funnel by source x type x play; credits per positive; rep stats
- **Done when:** source ROI is visible and campaign replies log automatically

### Later
- Agency and trade show organizer sources
- Event-date triggers (auto-task when an account enters the 4-9 month window)
- Job-change tracking for P1 contacts
- AE read-only view

---

## 9. Decisions needed

1. Sequencer choice (Apollo Sequences vs a dedicated tool) - determines Phase 5 scope.
2. Whether a Manager role exists now or Admin covers it.
3. SDR daily unlock quota (suggested starting point: 25 per day).
4. Whether SDRs can self-claim accounts from the unassigned pool or only request them.
5. HubSpot pipeline and stage for handoffs, and AE routing rules by region.
6. Who can upload files (admin only, or managers too).
7. Recycle rule: 90 days to nurture, then reassign or re-sequence.
