# Outbound CRM - Development Stories

Build specs for turning the Cvent Intelligence prototype into Nunify's multi-source outbound CRM.

## Files

Files are numbered in build order. The order puts the SDR workspace first so the sales team can start using the tool within about 1.5 weeks; the rest of the data foundation follows once SDRs are live. This supersedes the phase order in `architecture.md` section 8.

| # | File | Release | Contents | Estimate |
|---|---|---|---|---|
| - | `architecture.md` | Reference | Full target architecture, data model, roles, screens | - |
| 1 | `01_R1a_prerequisites.md` | 1a | Suppression, dedupe, clean names, owner / stage / next event fields, credit ledger | 3-4 days |
| 2 | `02_R2a_sdr_core.md` | 2a | Permissions, outreach status, activity log, tasks, account + contact pages, Today queue, My Accounts. **SDRs go live after this** | 1 week |
| 3 | `03_R2b_sdr_completion.md` | 2b | Unlock quotas, Mark Positive (emailed to AE until HubSpot), assignment screen, audit log | 1 week |
| 4 | `04_R1b_data_foundation_rest.md` | 1b | Persona config by type, review queue, source-neutral naming, type classification, sources model, scheduled source runs + background worker (Cvent weekly), event roles, full Accounts list | 2-2.5 weeks |
| 5 | `05_R3_hubspot_handoff.md` | 3 | Positive -> HubSpot deal, AE routing, two-way sync | 1 week |
| 6 | `06_R4_associations_source.md` | 4 | CSV / Excel upload source, import of the scraped association directory, ingest API for external scrapers, website qualification crawl, scoring, directory scraper moved in-platform (monthly) | 2-2.5 weeks |
| 7 | `07_R5_campaigns_reporting.md` | 5 | Sequencer integration, plays, admin funnel, timing triggers | 1-2 weeks |

**Interim rule until Release 1b ships:** SDRs work accounts already enriched from the Cvent source. Do not enrich new association accounts until persona config by type (R1-12) is live, or Apollo will pull corporate titles.

Story IDs (R1-01, R2-07 etc.) are kept stable across files so references in `architecture.md` and between stories still match.

## How to work through these

1. Put `architecture.md` in the repo at `docs/architecture.md` and reference it in the Antigravity workspace rules, so every agent task builds toward the same target model.
2. Work one release at a time. Do not start the next release until the current one passes its release demo checklist.
3. One story = one agent task = one commit. Paste a single story (with the release context block at the top of the file) into the agent, review, test, commit.
4. Stories within a release are listed in build order. Dependencies are noted per story.
5. Before any story marked **[migration]**, create a Neon branch, run the migration there, verify, then apply to production.

## Ground rules for every story

- Permission checks are enforced server-side on every API route, never only by hiding UI.
- No hard deletes of accounts, people or activities; use soft delete with an audit log entry.
- All credit-spending calls go through the credit ledger (R1-08).
- Every new list view supports search, filters, sort and pagination (25 / 50 / 100).
- Keep existing working features working; if a story requires breaking one, say so before building.
- No LLM calls anywhere in the system for now. Extraction, classification and validation are rule-based code (parsers, keyword lists, regex patterns, domain lists), with keyword and domain lists kept in admin-editable config.
- Use hyphens, not em dashes, in UI copy.

## Story format

Each story has: ID, title, user story, acceptance criteria (checkable), out of scope, touches (tables / screens), depends on.
