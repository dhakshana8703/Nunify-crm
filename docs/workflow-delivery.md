# Workflow delivery

The runnable build uses the Flask application and an independent offline worker. The focused workflow checks ran against disposable SQLite databases loaded from the existing saved exports or synthetic fixtures. The original architecture and eight story files match the handoff copies byte for byte.

## Saved exports in the local workspace

The running SQLite workspace has 752 organization accounts and a saved-evidence fit score for each account. Eighteen meet the current qualification threshold. Bulk scoring recorded zero enrichment credits.

| Source | Result |
| --- | --- |
| Directory of Associations | Staged 36,145 listings from `all_associations.json`. Promoted 719 new listings, preserved one existing link, and kept 296 in review and 35,129 in staging. There are 721 linked directory records including the earlier source-only example. |
| Cvent | Staged 542 events from `master_cvent_events.json`. Linked 91 new events using organizer evidence and kept three existing links. 447 await organizer review and one has an invalid source identifier. The importer uses organization names and domains from the company breakdown and reviewed source mapping for corroboration. |

Managers can filter by source, sort by fit score and assign a selected batch. Transfers require explicit approval. A stale ownership revision aborts the batch. The local schema migration preserved the existing Directory run metadata. Real-file replay on a disposable copy preserved all linked source identities and created zero duplicates. A separate manager and SDR test on a disposable copy used two imported accounts and verified source evidence, permission denial for a peer and a follow-up in Tomorrow. The full suite passed 30 tests.

The exported Apollo files were inspected read-only. The candidates file contains 1,504 unique person IDs, including 34 marked unlocked and 20 with saved email data. The usage file contains 491 historical entries reporting 63 credits. Its 43 unlock entries cover 37 distinct targets. Only 241 candidates and 24 of the 34 unlock flags had one exact normalized domain match in the local workspace. Use those matches as reconciliation candidates and validate the original company, contact, user and debit identities against a consistent deployed Neon and Apollo cache snapshot before posting legacy unlocks or credits.

| Area | Verification |
| --- | --- |
| Shared account, R1-02 and R1-09 | Verified one shared ID for linked Cvent and directory evidence, source-only accounts, and ambiguous parent and chapter listings in staging. Replays and source corrections preserved workflow owners, paid flags and activity rows. |
| Manager to rep, R2-01 through R2-08 and R2-11 | Edge verification with separate manager and rep sessions covered assignment, source evidence, a no-answer call, a follow-up in Tomorrow, and peer access denial. Tests covered a local midnight rollover, duplicate submission, simultaneous assignments, and stale forms. |
| Reply, leave and transfer, R2-02/03/04/11 | Live local forms captured a demo request, showed it as an unanswered inbound item, and carried open tasks to the new owner. A separate rep logged a reply during active coverage. Tests verified exact expiry, attribution, and continued ownership of commitments. |
| Handoffs, R2-10 and R3-02 | Local AE delivery showed the saved context and promised step in the receiving AE's inbox. Tests covered queued, failed and delivered state, retry after a provider accepted then timed out, duplicate prevention, and suppression applied before delivery. |
| Ingestion and operations, R1-13 and R4 | Verified CSV/XLSX preview, contact dedupe, API key scope and replay, scheduled full saved input previews, overlapping run rejection, expired lease and retry after a missing source input. |
| Qualification and credits, R1-01/08/12 and R4-03/04/05 | Fit comes from saved source evidence and optional saved HTML. Offline tests covered suppression before a provider call, manager approval, an SDR's local quota reset and credit dedupe after a failed database write. |
| Campaigns, R5-02/03 | Verified manager saved views and campaign membership, server-side contact eligibility, signed event dedupe, reply surfacing, bounce and opt-out handling with the local adapter. |

## QA pass - 20 November 2026

Verification used the current `main` working tree based on `639e910`. The running application and original SQLite workspace were read-only for QA. Actions that create or change data ran on disposable SQLite backups or in memory with saved exports and offline providers.

`python -B -m unittest discover -s tests -v` passed all 59 tests in 258 seconds. `python -B -m pip check` found no requirement conflicts in the installed environment. `node --check static/workflow.js` and `git diff --check` passed.

| Check | Verified outcome |
| --- | --- |
| Edge browser, R2-05/R2-11/R4-06 | On a disposable copy, Directory + Unassigned + Discovered showed 720 with no Apply click. At 375px, mixed-case keyboard search found AAPC, kept the filters and fit the viewport. In the browser, manager assignment of a saved Cvent account led to a visible, single Tomorrow follow-up for the SDR on 21 November in Asia/Kolkata. A separate SDR received 403 and could not see the task. |
| Assignment and SDR work, R2-01/03/04/06/07/11 | The manager assigned an imported Cvent account. The assigned SDR saw the saved evidence, logged one call and found its task in Tomorrow in Asia/Kolkata. Repeating the save produced one activity and task. A changed replay and stale assignment were rejected. |
| Access and leave coverage, R2-01/11 and the engineering brief | Separate sign-ins checked account and saved-listing access, a peer's denial, role restrictions on Sources and Source review, anonymous API denial and form CSRF. Temporary coverage let the authorized SDR log a reply, and expiry and transfer preserved the correct owner, actor and tasks. |
| AE handoff and recovery, R2-10/R3-02 | A saved demo request reached the local AE inbox after a simulated lost acknowledgement. A manager retry produced one delivery receipt. The SDR retry returned 403 and the AE saw the handoff after delivery. |
| Saved exports and recovery, R1-09/R1-13/R4-02 | Both files staged and promoted in memory. An interruption in the second Directory batch rolled back that batch, kept the 500-row checkpoint, and resumed. Replay kept 719 Directory links and 94 Cvent links with identical stored evidence. A missing Cvent file produced a failed run displayed with Retry. The same run recovered all 542 rows and preserved the existing assignments and credits. |
| Workspace integrity | All 819 stored source rows matched the original source IDs, URLs and retained values. The workspace contained 752 accounts and one existing assignment. SQLite integrity and foreign-key checks passed. |

### Reproduced defects

| Priority | Story | Reproduction and observed result |
| --- | --- | --- |
| High | [R4-04](stories/06_R4_associations_source.md#r4-04-fit-scoring) | Sign in as manager and select **Qualified and unassigned**. The workspace has 17 unassigned accounts that meet the threshold of 40, and the filter returns zero. Queue qualification for **All sources** and run the offline worker. The job finishes after processing zero because it skips already-scored rows. |
| High | [R4-06](stories/06_R4_associations_source.md#r4-06-cross-source-merge-visibility) | Stage an eligible Directory listing named `Independent Financial Association` and a Cvent event from `Independent Financial Association (IFA)` with the same corroborated organization domain. Directory-first yields one account with both links. Cvent-first leaves the Directory row in review against the Cvent account. Each importer uses a different organization-name comparison. |
| Medium | [R2-05](stories/02_R2a_sdr_core.md#r2-05-today-queue-sdr-home) | Log an inbound reply and then log an outbound email to the same person. **Unanswered reply** remains in Today until the rep selects **Mark reviewed**. The story calls for no outbound activity since the reply. |
| Medium | [R2-05](stories/02_R2a_sdr_core.md#r2-05-today-queue-sdr-home) | Assign a saved account and log its first call on the same day. Today still shows **Newly assigned**, although the story specifies accounts with no activity. |

The standard Directory loader stages 36,145 US listings. The other two supplied exports contain 1,364 additional listings outside that loader. Local tests exercise the saved-file pipeline, scoped users, and offline adapters. Production acceptance still requires the Neon rehearsal, historical ownership and credit reconciliation, and sandbox tests of live collection and provider connections.

The Edge checks cover the recorded manager and SDR paths and narrow-screen search. The additional sales and operations checks submitted rendered Flask forms with separate authenticated sessions.

### Fix verification - 20 November 2026

`python -B -m unittest discover -s tests -v` passed all 62 tests in 128 seconds after the fixes. The suite now covers saved-score stage application, both source import orders, and the two Today queue cases.

| Defect | Retest result |
| --- | --- |
| R4-04 qualification shortlist | Ran the normal manager bulk action on a workspace copy. The shortlist went from 0 to 17. The job applied 751 saved scores without rewriting their values or signals, and repeating it selected 0 records. Focused tests covered manual overrides, stale assignment forms and protection for owned or advanced stages. After a recovery backup, the local web app and offline worker applied the same job to the working database. It now has 17 qualified and 734 disqualified unassigned accounts. Existing ownership, sales activity, source records and credits matched the pre-change snapshot. |
| R4-06 inconsistent link by import order | Reproduced the failure, then ran both import orders through the normal Sources preview and promotion screens and the offline worker on disposable databases. The manager's **Both** filter showed one account with both source records in either order. Directory and Cvent now use the same validated full-name and acronym comparison for a unique same-host match. Different organizations with the same initials and invalid acronyms stayed in review. |
| R2-05 answered reply still queued | Logged an inbound reply and a later outbound to the same contact through the normal forms. Today cleared the reply. Earlier outbound activity, replies from other contacts, and replies without a contact retained their proper review status. |
| R2-05 newly assigned after first call | Assigned a saved account, logged a no-answer call and checked Today and Tomorrow in separate SDR sessions on a workspace copy. **Newly assigned** cleared and Tomorrow still contained exactly one follow-up. Previously worked accounts stayed out of the new-assignment list after transfer. |

### SDR contacts and standalone follow-ups - 20 November 2026

- **R2-08 contact work.** Reps can open a contact from People or the account, inspect that person's activity and open tasks, record details and status, or open the activity form with the contact selected. Manual contacts accept phone and LinkedIn fields. Account scope, suppression, duplicate detection, and stale detail and status checks apply on the server. Imported and verified email identity stays tied to its existing record.
- **R2-04 standalone work.** Reps can add a follow-up to an assigned account or contact from its own page. The due date is interpreted in the permanent owner's timezone. Coverage can create work for the owner and records the actual creator. Retried submissions keep one task. Transfer retains existing follow-up ownership behavior.
- **Saved-data exercise.** On a disposable copy, Riya added and edited contacts on a Directory-only account and on a Cvent-only account. Account-level and contact-level tasks each appeared once in Tomorrow. Exact replays were idempotent. Changed replays and stale forms returned 409, peer access and writes returned 403, and a mismatched account/contact route returned 404. Existing activities, the activity-linked task, source evidence, and fit scores matched the baseline. The legacy schema upgrade was rehearsed twice with existing data intact and no foreign-key violations.

`python -B -m unittest discover -s tests -v` passed all 67 tests in 172 seconds. Both local apps and their offline workers were restarted after backups. Authenticated screen checks passed on Riya's Directory account in the working app and her Cvent contact in the trial. The upgraded databases matched the original rows and passed integrity and foreign-key checks.

The engineering priority for this increment is the daily SDR workflow across both inputs. Fit policy is deferred for product discussion. [Manual SDR cases](manual-sdr-trial.md)

## Before connecting live systems

- **Data and deployment.** Confirm the deployed Cvent revision and take consistent snapshots of Neon and the live Apollo cache. Map legacy users, password hashes, allocations, contacts, unlocks and credits. Reconcile links and disputed charges, then rehearse the migration and recovery on a designated Neon branch. This build was verified locally on SQLite.
- **HubSpot and email.** Confirm the private app and approved credentials, pipeline, initial deal stage, and regional AE owner mapping. Implement and exercise provider-specific matching, deal upsert, routing and inbound sync against a designated test account. The AE inbox and retry state are implemented locally. Live delivery and customer communications require separate authorization.
- **Apollo.** Integrate the existing Cvent Apollo client with the credit-guarded worker using the approved account and cost rules. Reconcile actual usage and verify legacy grants against the deployed snapshot. Demo credits and contacts are synthetic.
- **Sources.** The scheduled Cvent and directory jobs stage the full saved exports for approval through Sources. Confirm live collection and storage arrangements before enabling live discovery and directory collection. Select the upload-role policy. Admin-only upload is the local default. Qualification uses saved evidence and accepts saved HTML.
- **Sequencer.** Select the vendor and provide approved credentials, sequence mappings and event signing configuration. The current callback uses `CRM_SEQ_WEBHOOK_SECRET` and the `X-Nunify-Signature: sha256=<digest>` header over the raw request body. Test the vendor's enrollment, pause and inbound event transport with sandbox contacts before enabling it.

## Goutham's checklist audit - 22 November 2026

Goutham's seven story files contain 294 boxes across 44 numbered stories and their release demo checks. The originals had 0 ticked boxes. This audit records their current status against the code, local trials, and saved-data test evidence.

A tick requires evidence for the full original item. An unticked item is **Partial** when a narrower implementation exists or some proof is missing, **Open** when the required flow is pending, and **Superseded** when the active engineering brief retires the original criterion. Live integration acceptance requires a live test. The current brief waives former demo timers and release gates. [Active engineering brief](../NUNIFY_CRM_CODEX_BUILD.md) and [Goutham's story index](stories/00_README.md)

The current local suite passed 69 tests using `python -B -m unittest discover -s tests -v`. Nine authenticated HTTP checks on port 5002 verified manager, admin, SDR, and AE access, including two expected denials, shared-source AARP, saved Apollo unlocks, Directory evidence, Operations, Sources, and the AE inbox. The trial had 752 accounts, 199 imported Apollo leads with 14 saved unlocks, zero assigned accounts, zero open tasks, and zero active schedules when checked. SQLite quick and foreign-key checks passed.

### Totals by release

| Original section | Ticked | Partial | Open | Superseded | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| [R1a prerequisites](stories/01_R1a_prerequisites.md) | 9 | 23 | 3 | 0 | 35 |
| [R2a SDR core](stories/02_R2a_sdr_core.md) | 24 | 20 | 4 | 2 | 50 |
| [R2b SDR completion](stories/03_R2b_sdr_completion.md) | 8 | 15 | 1 | 0 | 24 |
| [R1b data foundation](stories/04_R1b_data_foundation_rest.md) | 5 | 42 | 11 | 0 | 58 |
| [R3 HubSpot handoff](stories/05_R3_hubspot_handoff.md) | 0 | 8 | 18 | 0 | 26 |
| [R4 Association and ingestion](stories/06_R4_associations_source.md) | 12 | 46 | 8 | 1 | 67 |
| [R5 Campaigns and reporting](stories/07_R5_campaigns_reporting.md) | 7 | 20 | 7 | 0 | 34 |
| **Total** | **65** | **174** | **52** | **3** | **294** |

The contact management story **R2-08** is fully ticked (4/4).

### Decisions and live evidence pending

- HubSpot needs an approved sandbox, pipeline, initial stage, and AE routing. The local AE inbox and retry tests cover CRM handoff behavior.
- The sequencer vendor and test credentials are pending. Saved campaign membership, signed callbacks, and local activity can be evaluated in the trial.
- Concurrent Neon and Render Apollo snapshots, an approved company and event crosswalk, provider usage evidence, and a designated migration branch are needed for the full Cvent and credit migration.
- Manager roles and admin-only file uploads are the current local settings. The local default daily SDR quota is 25. SDRs can consult the unassigned pool and request assignment from a manager. Product decisions for uploads, recycling, and final paid quotas remain available for review.
- Configurable schedules and health operate against saved source files. Live recurring collection requires the source connectors, incremental checkpoints, and a tested disconnect flow.

### Individual checkboxes

#### R1a prerequisites

[Original story file](stories/01_R1a_prerequisites.md)

##### R1-01 Suppression list [migration]

- [x] Table `suppression` (id, kind: domain / email / account, value, reason: competitor / customer / open_deal / opt_out / bounced / manual, source, created_by, created_at)

  **Verified locally.** SQLite and PostgreSQL store source and enforce the six reason codes. Repeat migration preserves rule IDs, historical notes and removals. See [R1-01.1 verification](r1-01-suppression.md). (Original line 19.)

- [x] Seeded with: cvent.com, cventevents.com, cvent-production.com, and the Cvent subdomain pattern `*.cventevents.com`

  **Verified locally.** Both databases seed all four Cvent rules as competitors. Restarting respects a previously removed seed. (Original line 20.)

- [x] Domain matching works on root domain and all subdomains

  **Verified locally.** Root domains and nested subdomains match whole labels. A bare `national.alpfa.org` rule also holds ALPFA at `alpfa.org`. An unrelated organization's registration URL does not establish its identity. (Original line 21.)

- [x] Apollo enrichment and email unlock refuse suppressed accounts / people with a clear message

  **Verified locally with offline providers.** Enrichment, unlock and outreach refuse every linked contact on a held company, including contacts on other email domains. Individual email rules remain scoped to that address. Queued requests recheck suppression before the provider and before using the result. (Original line 22.)

- [x] Existing accounts matching suppression are flagged `suppressed = true` with `suppress_reason`; they are hidden from default views but visible with a "Show suppressed" filter

  **Verified locally.** SQLite and PostgreSQL backfill the stored flag and reason. Rule changes and account imports keep them in sync. Managers and SDRs can use Show suppressed within their account scope. (Original line 23.)

- [x] Admin screen: list, add, remove suppression entries (CSV bulk add supported)

  **Verified locally.** Admin can list, add, bulk upload and remove rules. Invalid CSV batches leave no changes. Removal records the rule details and Admin actor while keeping rule history. (Original line 24.)

##### R1-02 Root-domain normalization and merge [migration]

[Written acceptance checks, baseline and changed files](r1-02-root-domain-merge.md)

The local acceptance checks and the 50-test PostgreSQL fixture rehearsal on the designated `r1-02-rehearsal` Neon branch passed. The local saved-data copy held 769 accounts. The Neon branch's existing account count was one before and after its test run.

- [x] Function to normalize any URL / email domain to root domain (handles www, subdomains, country TLDs like .co.uk, .org.au)

  **Verified locally.** The offline suffix snapshot handles multi-label suffixes, internationalized domains and shared hosting tenants. (Original line 38.)

- [x] `accounts.root_domain` is unique

  **Verified locally.** SQLite and PostgreSQL backfill roots and enforce case-insensitive uniqueness after legacy duplicate groups are resolved. Parallel source admissions yielded one account. (Original line 39.)

- [x] One-time merge script: confirmed duplicates keep the lowest account ID in the database; events, people, credits and allocations follow the survivor; retired IDs are recorded in `account_merges` (from_id, to_id, merged_at)

  **Verified locally.** Both databases kept history and rolled back a forced late error. An attempted enrichment request still blocks retirement of its account after Retry requeues it. Dhakshana selected the lowest ID as the rule for all merges. (Original line 40.)

- [x] Dry-run mode that outputs the list of proposed merges before executing

  **Verified locally.** CLI preview reports the survivor, duplicate records, linked record IDs, and conflicts without writes. Repeating apply after a completed merge reports zero groups. (Original line 41.)

- [x] Known cases resolved: national.alpfa.org -> alpfa.org; medc.cventevents.com suppressed via R1-01

  **Verified locally.** The ALPFA fixture merged on a disposable copy of the saved CRM and preserved all 752 original account IDs. A separate fixture verified the `national.alpfa.org` rule holds ALPFA and its contacts across the merge and later imports. `medc.cventevents.com` stays suppressed, and an unrelated organizer's Cvent registration link stays eligible. (Original line 42.)

- [x] New records from any source go through normalize -> match -> merge-or-create

  **Verified locally.** The original three-source probe now creates one company and retains all three source records and their IDs. CSV, API, replay, seed and prepared transfer checks passed. (Original line 43.)

- [x] Admin can manually merge two accounts from the UI

  **Verified locally.** Admins and Managers can preview and confirm through the web UI. SDRs and AEs receive 403. The CLI applies as Admin. Stale or unauthorized submissions make no writes, and old links respect survivor permissions. (Original line 44.)

##### R1-03 Clean account names

[Active acceptance checks and saved-data rehearsal](r1-03-account-names.md)

- [x] Name resolution order: Apollo organization name > organizer name from event page > website title / og:site_name > domain-derived fallback

  **Verified with offline evidence.** The resolver follows the specified order and preserves the Directory tier in this two-source CRM. Shared registration pages and unverified external feed metadata cannot supply automatic website names. (Original line 58.)

- [x] `accounts.name_source` records which was used

  **Verified locally.** The backfill stores the selected source and reference alongside the name and audits each change. (Original line 59.)

- [ ] Backfill script run on all existing accounts (e.g. Theiia -> The Institute of Internal Auditors, Tdsynnex -> TD SYNNEX)

  **Rehearsed locally.** The disposable 769-account copy recorded 17 updates, including one display-name correction. A repeat made zero changes. SQLite and PostgreSQL fixtures verify both named examples. Their real Cvent company rows are still in identity review. Backfill on the hosted CRM remains separate. (Original line 60.)

- [x] Admin can edit the name inline; manual edits are never overwritten by backfills (`name_source = manual`)

  **Verified locally.** Separate Admin, Manager and SDR sessions exercised the account screens. An audited Admin edit survived source refresh and the backfill. (Original line 61.)

##### R1-06 Account type, platform, timing and stage fields [migration]

[Active acceptance spec and data decisions](r1-06-account-fields.md)

- [x] `accounts` gains: `type` (corporate / association / agency / organizer / university / government / other / unknown), `platform` (cvent / other_detected / none / unknown), `platform_detail` (text, e.g. "Swoogo"), `platform_confidence`, `next_event_date`, `events_per_year`, `members`, `fit_score`, `stage`, `owner_id`, `country`, `region` (India / US / Europe / Middle East / Other)

  **Verified locally.** New derived facts are persisted. The account screen and filters use the existing organization type, assignment, stage and qualification score records. (Original line 74.)

- [x] `stage` values per architecture doc section 4.2 (Discovered ... Nurture), default Discovered

  **Verified locally.** New stage values are validated, with Discovered as the default. Discovery advances eligible accounts to Enriched while existing later stages, owner changes and merged historical stages survive. Live HubSpot delivery stays under R3. (Original line 75.)

- [ ] `owner_id` populated from the existing Team allocations; allocations UI writes to `owner_id` from now on

  **Fixtures passed.** Assignments and the audited Cvent import share the ownership workflow. Previewing the 50 setup rows without an approved mapping found zero ready assignments. Applying verified legacy ownership remains pending. (Original line 76.)

- [x] `next_event_date` computed as the earliest event with start date >= today, recomputed nightly and on event insert

  **Verified locally.** The saved date refreshes with accepted event evidence and on the worker's first pass of the new business day. Replay, rollback, merge and midnight regressions pass. (Original line 77.)

- [x] All Cvent-sourced accounts set to `platform = cvent`, `platform_confidence = confirmed`

  **Verified locally.** The saved data rehearsal confirmed all 52 accounts tied to approved Cvent evidence, including 20 with company roster evidence and zero linked Cvent events. (Original line 78.)

- [x] `type` defaults to `unknown`; populated later by the rule-based classifier in Release 1b (R1-07). Admin can set it manually in the meantime

  **Verified locally.** New accounts with no supported explicit type default to Unknown. A verified Directory listing supplies Association. Admins can change the type with a recorded audit, and replay preserves the saved choice. (Original line 79.)

- [x] Columns Owner, Stage and Next event added to the existing Companies list, with filters for Owner (incl. Unassigned) and Stage

  **Verified locally.** Assignments shows owner, stage and next event with owner and stage filters. Scoped account access and current follow-up behavior passed the regression checks. (Original line 80.)

##### R1-08 Single credit ledger [migration]

- [x] Table `credit_ledger` (id, user_id, account_id, person_id, action: org_match / people_search / email_unlock / phone_unlock, credits, at)

  **Verified on disposable SQLite and PostgreSQL.** The upgrade preserves ledger IDs and job references. It supports all four action codes, decimal charges and one signed reconciliation adjustment. (Original line 93.)

- [ ] Every Apollo call that spends credits writes exactly one ledger row

  **Verified with provider doubles.** An explicit live adapter and the worker record a confirmed deduction once, including when publication is suppressed or fails. Uncertain paid jobs stay held across restart. Live Apollo response verification remains pending. (Original line 94.)

- [x] Header badge, stats card and Team screen all read from the ledger (fixes the 62 vs 66 mismatch)

  **Verified with saved fixtures.** The signed-in header, Operations and Team queue share one confirmed total and show unattributed credits. The historically correct hosted balance depends on the reviewed import. (Original line 95.)

- [ ] Historical credits migrated; any unreconcilable difference recorded as a single "opening balance adjustment" row

  **Importer verified on both engines.** Admin preview and apply preserve source references, enforce one signed adjustment, and reject stale or conflicting inputs. The actual historical migration depends on a verified charge export and approved statement total and cutoff. (Original line 96.)

- [x] Per-user daily credit total available via API (used in R2 quotas)

  **Verified with saved fixtures.** The role-scoped daily endpoint returns confirmed credits for the user's recorded local date. Quota reservations remain separate from confirmed spend. (Original line 97.)

##### Release 1a demo checklist

- [x] No cvent.com / cventevents.com people visible in default People view; enrichment on them is blocked

  **Verified locally.** Default People hides competitor contacts and the local enrichment guard blocks them. Saved evidence and history remain visible to authorized staff as tested in tests/test_suppression_visibility.py. (Original line 106.)

- [ ] No two accounts share a root domain (national.alpfa.org merged into alpfa.org)

  **Partial.** Exact hosts are deduplicated locally. The root-domain and ALPFA merge demonstration is still open. (Original line 107.)

- [ ] Account names are human-readable for the top 50 accounts by events

  **Partial.** Saved names and provenance were improved, with the top 50 by events and the remaining legacy names unverified. (Original line 108.)

- [ ] Every account has owner (or Unassigned), stage and next event date; existing allocations preserved

  **Partial.** Owner or Unassigned, stage and calculated next event are visible. Legacy allocations are pending and accounts with no valid future event have no date. (Original line 109.)

- [ ] Credits show the same number in header, stats card and Team screen

  **Partial.** One local ledger is in use. The original three-screen reconciliation has no completed proof. (Original line 110.)

- [ ] Existing Cvent scraper, enrichment and SDR login still work

  **Partial.** Local SDR login and saved Cvent imports work. Standalone live collector and live Apollo continuity are unverified in this CRM. (Original line 111.)

#### R2a SDR core

[Original story file](stories/02_R2a_sdr_core.md)

##### R2-01 Roles and server-side permissions [migration]

- [x] `users.role` supports: admin, manager, sdr, ae

  **Verified locally.** SQLite enforces all four roles and role-specific sign-ins were exercised (workflow_store.py, tests/test_workflow.py, tests/test_sales_extensions.py). (Original line 19.)

- [ ] Middleware checks role and ownership on every route; UI hiding alone is not acceptable

  **Partial.** Central login and CSRF checks plus account and coverage scope protect tested routes. The complete route-by-route permission matrix has not been exercised (workflow_web.py, tests/test_workflow.py). (Original line 20.)

- [ ] Permission matrix from architecture doc section 5.2 implemented; key rules:
  - SDR reads / writes only accounts where `owner_id = self` and their people
  - SDR can view the unassigned pool read-only (name, type, platform, next event, score - no contact details)
  - SDR cannot export, delete, edit config, run sources or reassign
  - Manager has admin rights except sources, config, suppression and user management

  **Partial.** Core SDR, staff and AE scope is enforced. The read-only SDR pool still lacks platform and fit score fields, and the full matrix is incomplete (workflow_web.py, templates/my_accounts.html). (Original line 21.)

- [ ] Automated tests: an SDR token calling another SDR's account, export, delete and config endpoints gets 403

  **Partial.** The account and events API return 403 for a peer. Staff export and config are separate protected endpoints, and the specified delete route and complete four-way test are absent (tests/test_workflow.py, growth_ops.py). (Original line 26.)

- [ ] Admin can change a user's role from the Team screen

  **Open.** No Team screen role editor. Active local users and roles are seeded or managed outside the app. (Original line 27.)

##### R2-02 Person outreach status [migration]

- [ ] `people.outreach_status`: not_contacted / in_sequence / replied / connected / positive / not_interested / wrong_person / bounced / opted_out / handed_off (default not_contacted)

  **Partial.** people.status defaults to Not contacted and supports the daily workflow states. Campaign membership has separate state and in_sequence is not applied to the person (workflow_store.py, campaigns.py). (Original line 40.)

- [x] `people.do_not_contact` boolean; set automatically for opted_out and bounced; such people cannot be added to campaigns or unlocked again

  **Verified locally.** Bounce and opt-out recording sets do_not_contact, and campaign enrollment and the unlock worker enforce suppression (workflow_web.py, campaigns.py, tests/test_campaigns.py, tests/test_suppression_visibility.py). (Original line 41.)

- [x] Opted-out emails are added to the suppression list (R1-01)

  **Verified locally.** Manual opt-out and signed unsubscribe insert the email into suppression (workflow_web.py, campaigns.py, tests/test_campaigns.py). (Original line 42.)

- [x] Lock / unlock remains a separate field (`unlocked`)

  **Verified locally.** unlocked is stored separately from sales status and verified email. Saved, LinkedIn-only and locked Apollo states were verified in the 5002 trial (workflow_store.py, working.md). (Original line 43.)

- [ ] Status shown as a badge in People list and contact page; filterable

  **Partial.** People and contact pages display status badges. People has search and sort but lacks an outreach-status filter (templates/people.html, templates/contact.html). (Original line 44.)

##### R2-03 Activity log [migration]

- [ ] Table `activities` (id, person_id, account_id, user_id, type: email / call / linkedin / meeting / note, direction: outbound / inbound, outcome, body, occurred_at, source: manual / webhook)

  **Partial.** The local activity record has account, person, actor, channel, direction, outcome, note and timestamp. It lacks an explicit manual/webhook source column (workflow_store.py, campaigns.py). (Original line 57.)

- [x] Outcomes per type:
  - call: no answer / voicemail / connected / wrong number / not interested / meeting booked
  - email: sent / replied / bounced
  - linkedin: connection sent / accepted / message sent / replied

  **Verified locally.** The activity form and server accept every listed call, email and LinkedIn outcome (workflow_web.py, templates/workflow_account.html). (Original line 58.)

- [ ] **Log activity modal** opens from contact page, account page and Today queue; required fields: type, outcome; optional: note, next action date

  **Partial.** Account and contact open the inline activity form with the requested fields. Today links to the account and lacks a direct form action (templates/workflow_account.html, templates/contact.html, templates/today.html). (Original line 62.)

- [ ] Completing the modal takes under 10 seconds for a no-answer call (keyboard-friendly, outcome buttons not dropdowns)

  **Superseded.** The active engineering brief removed the 10-second demo gate. The current flow has a keyboard-usable form and outcome selector. (Original line 63.)

- [x] Logging certain outcomes updates person status automatically (replied -> replied; connected -> connected; bounced -> bounced; not interested -> not_interested; meeting booked -> prompts Mark Positive)

  **Verified locally.** Activity applies reply, connected, bounce and not-interested statuses and a booked meeting prompts Prepare AE handoff (workflow_web.py, templates/workflow_account.html). (Original line 64.)

- [x] Setting a next action date creates a task (R2-04)

  **Verified locally.** Saving an activity and a next-action date creates the linked follow-up once, including repeat submission (tests/test_workflow.py). (Original line 65.)

- [x] Account `stage` moves to Working on first logged outbound activity, Engaged on first reply / connected call

  **Verified locally.** The first outbound touch moves an eligible account to Working. Reply or connected moves it to Engaged (workflow_web.py, tests/test_workflow.py). (Original line 66.)

##### R2-04 Tasks and next actions [migration]

- [ ] Table `tasks` (id, user_id, account_id, person_id, kind: call / email / linkedin / research / other, due_at, status: open / done / snoozed, note, created_at, completed_at)

  **Partial.** Task records have owner, account, optional contact, title, due date and state. The requested kind, separate note, creation and completion fields are absent (workflow_store.py). (Original line 79.)

- [x] Created from Log activity (next action date) or manually from account / contact page

  **Verified locally.** Activity next-action dates and standalone account/contact forms all create scoped follow-ups, verified with saved data (tests/test_workflow.py, docs/workflow-delivery.md). (Original line 80.)

- [ ] Complete, snooze (1 day / 3 days / 1 week / custom), reassign (manager only)

  **Partial.** Complete, tomorrow/default snooze and custom due date work. Dedicated 3-day and 1-week shortcuts and individual manager task reassignment are missing (workflow_web.py, templates/today.html). (Original line 81.)

- [x] Overdue tasks highlighted

  **Verified locally.** Today prioritizes overdue tasks and labels their reason Overdue (workflow_web.py, templates/today.html). (Original line 82.)

- [x] When an account is reassigned, its open tasks move to the new owner

  **Verified locally.** Manager account transfer moves open follow-ups atomically to the new owner while preserving history (tests/test_workflow.py, tests/test_bulk_assignment.py). (Original line 83.)

##### R2-07 Account page

- [ ] Header: name, domain link, type, platform (+ detail), region, members, next event, fit score, stage, owner, sources

  **Partial.** Name, domain text, type, platform, members, fit, owner, sources, next event and stage show across account content. A domain link, full region, and the single requested header layout are incomplete (templates/account.html, templates/workflow_account.html). (Original line 96.)

- [ ] **Why this account** panel: for now, built from Cvent events (event name, date, Cvent page link) and "Uses Cvent - confirmed". Designed so richer signals from `account_signals` (Release 4) can be added later

  **Partial.** Why this account summarizes saved Cvent and Directory evidence and links to evidence. Individual event names, dates and URLs sit in the separate source section (templates/workflow_account.html, templates/account.html). (Original line 97.)

- [ ] Events list (upcoming first); role badges added later with R1-10

  **Partial.** The account shows dated Cvent events. The date sort is ascending without a dedicated upcoming-first partition (app.py, cvent_reads.py). (Original line 98.)

- [ ] Contacts grouped by persona tier (P1, P2, other) with status badge, email status, LinkedIn link, last touch

  **Partial.** Status, available email and LinkedIn, Apollo flags and source evidence are visible. P1/P2/other grouping and last-touch display are still missing (templates/workflow_account.html). (Original line 99.)

- [x] Account activity timeline (all people, newest first)

  **Verified locally.** The account shows the account-wide, newest-first activity list with contact, actor, outcome and timestamp (workflow_web.py, templates/workflow_account.html, docs/workflow-delivery.md). (Original line 100.)

- [ ] Actions: enrich (existing persona logic until R1-12; quota enforced from R2-09), unlock email, add contact manually, log activity, create task, change stage (Nurture / Disqualified with reason), Mark Positive

  **Partial.** Contacts, logging, follow-up and local positive handoff work. Enrich/unlock are offline and general disqualification with required reason is missing (templates/workflow_account.html, growth_ops.py). (Original line 101.)

- [ ] Stage change to Disqualified requires a reason (wrong fit / no events / already customer / competitor / other)

  **Open.** No reason-required manual Disqualified action. The local qualification workflow can apply a score-based stage. (Original line 102.)

##### R2-08 Contact page and manual contacts

- [x] Contact page: name, title, persona, email + status, phone, LinkedIn, account link, outreach status, activity timeline, open tasks

  **Verified locally.** The contact profile renders available details, account link, scoped timeline and open follow-ups. Tests cover phone, LinkedIn, persona and contact-specific history (tests/test_contact_workflow.py). (Original line 115.)

- [x] Actions: log activity, change status, mark wrong person, mark do not contact, edit details, Mark Positive

  **Verified locally.** Scoped contact actions include logging, status changes including wrong person and do not contact, editing, and a local Mark Positive link when eligible (templates/contact.html, tests/test_contact_workflow.py). (Original line 116.)

- [x] Add contact manually from account page: name, title, email, LinkedIn, phone, persona; `origin = manual`

  **Verified locally.** Manual entry accepts all requested fields and defaults origin to manual. Separate acting and peer users were tested (workflow_web.py, workflow_store.py, tests/test_contact_workflow.py). (Original line 117.)

- [x] Duplicate check on email and LinkedIn URL within the account

  **Verified locally.** Email and canonical LinkedIn duplicates are rejected within the account while other account records remain scoped (tests/test_contact_workflow.py). (Original line 118.)

##### R2-05 Today queue (SDR home)

- [x] Default landing page for SDR role

  **Verified locally.** A signed-in SDR lands on Today, verified with separate rep sessions (workflow_web.py, tests/test_workflow.py). (Original line 131.)

- [ ] Sections in order:
  1. Replies and inbound needing response (person status = replied, no outbound activity since)
  2. Tasks due today and overdue
  3. Accounts entering the timing window (next event 4-9 months out, no activity in last 30 days)
  4. Newly assigned accounts with no activity

  **Partial.** Replies with no later outbound, due/overdue tasks, new assignments and event-window items exist. Today has one sorted list, shows newly assigned before timing, and does not enforce the 30-day inactivity condition for events (workflow_web.py). (Original line 132.)

- [ ] Each row: account, person (if any), reason, due date, and quick actions (log activity, open, snooze, done)

  **Partial.** Today shows account, reason and due date plus open, reschedule and complete actions. It lacks consistent person attribution and a direct Log Activity action (templates/today.html). (Original line 137.)

- [ ] Counters at the top: touches today, replies this week, positives this month

  **Open.** Today's top-level touch, reply and positive counters are missing. Workload counts are available in staff views. (Original line 138.)

- [x] Empty state message when nothing is due

  **Verified locally.** Today and Tomorrow render separate empty states (templates/today.html, live SDR trial). (Original line 139.)

##### R2-06 My Accounts

- [ ] Columns: Name, Domain, Type, Platform, Next event, Events, Stage, Leads unlocked / total, Last touch date, Open tasks; scoped to `owner_id = self`

  **Partial.** My Accounts shows scoped name, domain, type, source, stage, owner and next event. Platform, event and unlocked counts, last touch and open task counts are incomplete (templates/my_accounts.html). (Original line 152.)

- [ ] Filters: Stage, Next event window (0-3 / 4-9 / 10-18 months / none), Type, search by name / domain

  **Partial.** My Accounts has name/domain search and ownership/coverage filters. Stage, date-window and organization-type filtering remain incomplete (workflow_web.py, templates/my_accounts.html). (Original line 153.)

- [ ] Default sort: next event ascending (upcoming first), accounts with no activity on top within ties

  **Partial.** Sorting by Next event is selectable. The default is Name, and no-activity tie-break is missing (workflow_web.py, templates/my_accounts.html). (Original line 154.)

- [ ] "Request more accounts" button sends a notification to managers / admin with an optional note

  **Open.** The unassigned pool advises asking a manager. No request form or notification is available (templates/my_accounts.html). (Original line 155.)

- [x] No export, no bulk delete

  **Verified locally.** My Accounts has no export or bulk delete controls, and an SDR cannot use the staff export endpoint (templates/my_accounts.html, growth_ops.py). (Original line 156.)

##### Release 2a demo checklist (SDR go-live gate)

- [x] Log in as SDR: lands on Today; sees only own accounts; cannot export, delete or open config

  **Verified locally.** An SDR lands on Today and sees only assigned or explicitly covered work. SDR export and config are denied and account DELETE is unsupported (workflow_web.py, growth_ops.py, disposable SDR check). (Original line 165.)

- [x] Attempt to open another rep's account by URL / API returns 403

  **Verified locally.** A second SDR received 403 for the other rep's account URL and account-scoped events API (tests/test_workflow.py, docs/workflow-delivery.md). (Original line 166.)

- [ ] Log a no-answer call with next action tomorrow in under 10 seconds; task appears in tomorrow's queue

  **Superseded.** The active brief retired the 10-second threshold. A no-answer call and one timezone-correct Tomorrow follow-up were verified (tests/test_workflow.py, docs/workflow-delivery.md). (Original line 167.)

- [x] Log a reply; person status and account stage update automatically

  **Verified locally.** A saved reply marks the contact Replied and moves eligible account work to Engaged (workflow_web.py, disposable SDR check). (Original line 168.)

- [ ] Account page shows events, contacts by persona tier and full timeline

  **Partial.** Events and the full timeline are visible. Contacts are a single list and lack the required persona tier groups (templates/workflow_account.html). (Original line 169.)

- [x] Add a manual contact; duplicate email is rejected

  **Verified locally.** A manually added contact appears in People, and a duplicate email within the account returns 409 (tests/test_contact_workflow.py). (Original line 170.)

- [x] Suppressed accounts and do-not-contact people cannot be worked

  **Verified locally.** Suppressed companies and do-not-contact people are blocked from outreach, assignment or unlock where applicable (tests/test_suppression_visibility.py, tests/test_contact_workflow.py). (Original line 171.)

#### R2b SDR completion

[Original story file](stories/03_R2b_sdr_completion.md)

##### R2-09 Daily unlock quota

- [ ] `users.daily_unlock_quota` (default 25), editable by admin in Team screen

  **Partial.** An SDR defaults to 25 daily credits and an admin edits the quota in Operations. The requested Team screen and daily_unlock_quota field name are absent (workflow_store.py, templates/operations.html). (Original line 19.)

- [ ] Enrich / unlock blocked when the SDR's credits today (from credit ledger) reach quota; message shows remaining and reset time (midnight user's timezone)

  **Partial.** Offline search and unlock both respect the SDR's timezone and daily quota. The error explains midnight reset, while remaining balance appears separately on the account page (growth_ops.py, tests/test_growth_ops.py). (Original line 20.)

- [ ] Remaining quota visible in SDR header

  **Partial.** The remaining daily demo credits appear in account Qualification. The SDR header does not show them (templates/growth_account.html, templates/base.html). (Original line 21.)

- [x] Admin and manager not limited

  **Verified locally.** The daily personal quota is only enforced for SDRs. Managers and admins are exempt from this personal cap (growth_ops.py, tests/test_growth_ops.py). (Original line 22.)

##### R2-10 Mark Positive (local)

- [ ] Modal fields (required): contact, positive type (meeting booked / demo requested / pricing requested), meeting date-time if booked, context note (min 30 characters), suggested AE (dropdown of AE users)

  **Partial.** The inline positive form takes the listed outcomes, meeting date when required, context and active AE. It accepts context shorter than 30 characters (sales_extensions.py, templates/sales_account.html). (Original line 35.)

- [x] Saves a `handoffs` row (id, account_id, person_id, user_id, ae_id, type, meeting_at, note, created_at, hubspot_status: pending)

  **Verified locally.** The positive action persists the account, person, actor, selected AE, outcome, meeting, context and timestamp in a durable pending local handoff (sales_extensions.py, tests/test_sales_extensions.py). (Original line 36.)

- [x] Person status -> positive; account stage -> Positive

  **Verified locally.** Mark Positive sets the contact and eligible account to Positive, with separate local delivery transition (sales_extensions.py, tests/test_sales_extensions.py). (Original line 37.)

- [ ] Until Release 3: email sent immediately to the selected AE and all managers with account, contact (name, title, email, phone, LinkedIn), positive type, meeting time, SDR note and last 10 activities

  **Partial.** The local AE inbox receives a saved handoff, available email and recent history. Real email to AE and managers, and all listed contact fields in the outgoing payload, are missing (sales_extensions.py, docs/workflow-delivery.md). (Original line 38.)

- [ ] Manager / admin also notified in-app

  **Partial.** Managers and admins see positive handoffs and statuses in Handoffs. A separate in-app notification is missing (sales_extensions.py, templates/sales_handoffs.html). (Original line 39.)

- [x] Handoffs list for admin / manager with status column (HubSpot push comes in R3)

  **Verified locally.** Managers and admins can open and filter the local Handoffs status list. Pending, failed and successful delivery paths were rehearsed (sales_extensions.py, tests/test_sales_extensions.py). (Original line 40.)

##### R2-11 Assignment screen

- [ ] Left: unassigned qualified accounts (filters as Accounts list)

  **Partial.** Assignments has a Qualified and unassigned filter, source and type filters, and fit sorting. Full Accounts filters such as region and date-window are missing (workflow_web.py, templates/assignment.html). (Original line 53.)

- [x] Right: reps with assigned accounts count, open tasks, touches last 7 days

  **Verified locally.** Rep workload shows assigned account counts, open tasks and outbound touches over seven days (workflow_web.py, templates/assignment.html, local manager screen). (Original line 54.)

- [x] Assign selected accounts to a rep; reassign from one rep to another

  **Verified locally.** Single and selected bulk assignment and manager transfer work with revision checks and confirmation (tests/test_workflow.py, tests/test_bulk_assignment.py). (Original line 55.)

- [ ] Rule-based assign: by region and / or type, round robin across selected reps, capped at N accounts per rep

  **Partial.** Capped round robin supports selected reps and a type-filtered pool. Region-based allocation is still pending (tests/test_bulk_assignment.py, templates/assignment.html). (Original line 56.)

- [ ] Every assignment change written to `assignments` history (account_id, user_id, assigned_by, assigned_at, ended_at)

  **Partial.** Assignments write actor, old/new owner, account and timestamp to durable audit events. The dedicated assignment history table with ended_at is absent (workflow_web.py, workflow_store.py). (Original line 57.)

- [x] Reassignment moves people ownership and open tasks with the account

  **Verified locally.** Contact access follows the permanent account owner, and active follow-ups transfer to the new owner without rewriting history (tests/test_workflow.py, tests/test_bulk_assignment.py). (Original line 58.)

##### R2-12 Audit log [migration]

- [ ] Table `audit_log` (id, user_id, action, entity, entity_id, before JSON, after JSON, at)

  **Partial.** audit_events records actor, account, action, details and timestamp. Generic entity, arbitrary before/after snapshots and the exact audit_log schema are missing (workflow_store.py). (Original line 71.)

- [ ] Logged: exports, deletes, merges, reassignments, role changes, suppression changes, config changes, Mark Positive

  **Partial.** Source report export, assignment, suppression, configuration and positive outcomes generate audit events. Full delete, merge and role-change auditing is missing with those workflows (growth_ops.py, workflow_web.py, sales_extensions.py). (Original line 72.)

- [ ] Admin view with filters by user, action and date

  **Open.** No admin audit-log browser with user, action and date filters. (Original line 73.)

##### Release 2b demo checklist

- [x] Hit the daily unlock quota; further unlocks are blocked with a clear message and remaining count resets at midnight

  **Verified locally.** The offline quota test hits the limit, denies another unlock, and allows it after the rep's local midnight (tests/test_growth_ops.py). (Original line 82.)

- [ ] Mark Positive creates a handoff and the AE receives the email within a minute

  **Partial.** A local handoff and AE inbox delivery were verified. Actual email remains open, and the active brief retired the one-minute demo threshold (tests/test_sales_extensions.py, NUNIFY_CRM_CODEX_BUILD.md). (Original line 83.)

- [ ] Manager assigns 20 unassigned accounts by region round robin; reps see them in My Accounts

  **Partial.** Capped round robin and rep My Accounts were exercised. No region-based allocation or the stated 20-account demo is verified (tests/test_bulk_assignment.py). (Original line 84.)

- [ ] Manager reassigns an account; people and open tasks move with it; audit log shows the change

  **Partial.** Replacement SDR contact access and active follow-up transfer are verified, and the audit row persists. The staff audit-log screen to inspect it is missing (tests/test_workflow.py, tests/test_bulk_assignment.py). (Original line 85.)

- [ ] Export and delete actions appear in the audit log

  **Partial.** The source funnel CSV export is audited. Delete auditing and the full account/contact export coverage are absent (growth_ops.py). (Original line 86.)

#### R1b data foundation

[Original story file](stories/04_R1b_data_foundation_rest.md)

##### R1-12 Persona configuration by type [migration]

- [ ] Table `personas` (id, org_type, tier: P1 / P2, name, title_keywords[], seniorities[], departments[], active)

  **Open.** Contact persona labels exist. There is no configurable personas table with tier, titles, seniority and department. (Original line 19.)

- [ ] Seeded from architecture doc section 4.3

  **Open.** A small offline default title map exists. The architecture's full persona set was not seeded. (Original line 20.)

- [ ] Apollo people search uses the personas matching the account's type; `unknown` type falls back to corporate personas

  **Partial.** The offline worker chooses a title by type and has a fallback. A live Apollo search using the requested full personas is pending. (Original line 21.)

- [ ] Admin config screen to add / edit / deactivate personas

  **Open.** The configuration screen edits growth settings. Persona add, edit and deactivate actions are missing. (Original line 22.)

- [ ] Existing people keep their persona tag; new persona IDs mapped where obvious

  **Partial.** The cache import preserves existing CRM people and carried Apollo persona tags forward. Mapping tags into configurable persona IDs is open. (Original line 23.)

##### R1-04 Extraction confidence and review queue [migration]

- [ ] Rule-based validation runs on every extracted record (no LLM). A record is flagged when any rule fires:
  - end date more than 14 days after start date, or end date before start date
  - event date more than 3 years in the future
  - organizer name looks like an email handle or slug: no spaces and over 15 characters, contains @, or matches the local part of an extracted email
  - organizer name is a generic word or the platform name (e.g. "Events", "Registration", "Cvent", "Home")
  - organizer name and website domain share no common token (possible mismatch)
  - missing website or missing organizer

  **Partial.** Deterministic saved extraction checks organizer and date plausibility. The full original rule set and review routing are incomplete. (Original line 36.)

- [ ] Each flag stores the rule name so the review queue shows why it was flagged

  **Partial.** Review displays stored reasons on staged records. A stable rule-name field and full per-rule coverage are missing. (Original line 43.)

- [ ] Rules live in one validation module so new rules can be added without touching the scrapers

  **Partial.** Validation is in the CRM's source-specific adapters, separate from the scrapers. A single shared rule module is missing. (Original line 44.)

- [ ] Flagged records appear in an admin **Review queue** with the field, extracted value, source URL and suggested fix

  **Partial.** Scoped Source review displays reasons, extracted values and source links. A full per-field suggested correction workflow is open. (Original line 45.)

- [ ] Admin actions: approve, edit and approve, reject (soft delete)

  **Partial.** Staff can hold, exclude and link with a corrected organization name, preserving the original payload. Generic edit-and-approve and date correction are missing. (Original line 46.)

- [ ] Known bad records from the prototype (organizer "liachaptercommunications", event dated Sep 2026 -> Sep 2031) appear in the queue after backfill

  **Partial.** Saved exports exercise organizer and date problems. The named organizer was absent from this trial's staged Cvent data and the exact pair is unverified. (Original line 47.)

##### R1-05 Generalize naming

- [ ] UI renamed: "Cvent Intelligence Pipeline" -> "Nunify Outbound"; "Companies" -> "Accounts"; "Dedicated Cvent Companies" -> "Accounts"; "Total Cvent Events" -> "Events"; "Scraper & Diagnostics" -> "Sources"

  **Partial.** The local UI uses Nunify CRM, Accounts and Sources. The original exact branding differs and collector controls remain separate. (Original line 60.)

- [ ] Persona "Cvent Event Creators" renamed "Event page contact"

  **Partial.** People marks Cvent evidence contacts as Cvent event. The specified Event page contact persona label is absent. (Original line 61.)

- [x] Code / table renames done where cheap; where a table rename is risky, a view or alias is acceptable

  **Verified locally.** Shared account and source tables support both data sources. Cvent read compatibility is retained with the cvent_read view, tested in tests/test_demo.py. (Original line 62.)

- [ ] Existing Cvent scraper still works end to end after renames

  **Partial.** Cvent saved exports and event API work end to end locally. A fresh live collection run after the changes is unverified. (Original line 63.)

##### R1-07 Type classification backfill

- [ ] Rule-based classifier (no LLM), applied in this order; first match wins:
  1. Manual override set by an admin (never overwritten)
  2. Domain lists: known agency domains, known trade show organizer domains (seed lists in config, admin-editable)
  3. TLD: `.edu` / `.ac.*` -> university; `.gov` / `.gov.*` / `.mil` -> government
  4. Name keywords -> association: Association, Society, Institute, Council, Federation, Chamber, League, Alliance, Coalition, Academy, College of (as in "American College of Cardiology"), Union, Guild, Foundation, Board of
  5. Name / website title keywords -> agency: Events, Meetings, Productions, Experiential, DMC, Destination Management, Incentives, Agency, Event Management, Conferences (when not also matching association keywords)
  6. Name / title keywords -> organizer: Expo, Exhibitions, Trade Show, Fairs, Media Group (with event listings)
  7. `.org` domain with no keyword match -> association (low confidence)
  8. Everything else -> corporate (low confidence if no Apollo industry data)

  **Partial.** Cvent seed typing and the admin override are deterministic and preserve manual data. The requested complete ordered classifier is missing. (Original line 76.)

- [ ] Apollo organization industry used as a tie-breaker when available (e.g. "nonprofit organization management" -> association; "events services" -> agency)

  **Open.** Verified Apollo industry is not used in the current type classifier. (Original line 85.)

- [ ] Each classification stores `type_rule` (which rule matched) and confidence (high for rules 1-4, medium for 5-6, low for 7-8)

  **Open.** Full type-rule and confidence provenance is absent. (Original line 86.)

- [ ] Low-confidence classifications go to the Review queue (R1-04) for a quick admin confirm

  **Open.** Source review handles identity evidence. An explicit low-confidence type review flow is absent. (Original line 87.)

- [ ] Keyword lists and domain lists stored in config, editable by admin, with a "re-run classification" button

  **Partial.** Outreach qualification has editable growth rules. Agency and organizer classification settings and a rerun action are open. (Original line 88.)

- [ ] Run on all existing accounts; summary count by type shown to admin after the run

  **Open.** A complete type reclassification run and type-count summary are missing. (Original line 89.)

- [ ] New accounts classified automatically on creation

  **Partial.** Current importers assign or infer initial types. A shared comprehensive creation-time classifier is missing. (Original line 90.)

##### R1-09 Sources model and provenance [migration]

- [ ] Tables `sources` (id, name, kind, config JSON, active), `source_runs` (id, source_id, started_at, finished_at, status, pages_fetched, accounts_new, accounts_merged, errors), `account_sources` (account_id, source_id, source_record_url, first_seen, last_seen)

  **Partial.** Source registration, runs, rows and account links exist. Full first and last seen provenance, lifecycle flags and run metrics are incomplete. (Original line 103.)

- [ ] Cvent scraper registered as a source; every run writes a `source_runs` row

  **Partial.** Cvent is registered and every CRM saved run is logged. Standalone live scraper runs do not yet log into this CRM. (Original line 104.)

- [ ] Existing accounts backfilled into `account_sources` with the Cvent source

  **Partial.** The current trial has 94 verified Cvent records linked to 32 accounts. Complete legacy company linkage and backfill are pending. (Original line 105.)

- [ ] Raw page content stored in `raw_pages` (run_id, url, content, hash, fetched_at) so re-extraction does not require re-scraping

  **Partial.** The CRM stores retained raw JSON and staged payloads. A full captured raw HTML archive with fetch hashes is missing. (Original line 106.)

- [ ] **Sources** screen: one card per source with last run, status, yield (new / merged), errors, run / stop buttons. Existing live console and single-URL extractor move under the Cvent source card.

  **Partial.** Sources shows saved run history, results and errors. Source stop, live console and single-URL extraction are missing. (Original line 107.)

##### R1-10 Event-organization roles [migration]

- [ ] Table `event_orgs` (event_id, account_id, role: host / client / producer / sponsor / venue)

  **Open.** There is no event-to-organization multi-role relation. (Original line 120.)

- [ ] Existing event -> company links migrated as role = host

  **Partial.** Saved event IDs link to confirmed organizers. No explicit host role data migration was performed. (Original line 121.)

- [ ] Role detection by rules (no LLM):
  - phrases in page text: "produced by", "managed by", "organized by", "in partnership with", "hosted by", "sponsored by", "powered by" followed by an organization name or link
  - organizer email domain differs from the brand domain in the event title / logo link -> organizer email domain = producer, brand = client
  - organizer email or domain on the known agency list (R1-07) -> producer
  - venue taken from the structured location field -> venue (not created as an account)

  **Open.** The current host identity rules do not implement phrase-based event participant role extraction. (Original line 122.)

- [ ] When no role phrase is found, the single organizer is stored as host (current behaviour)

  **Partial.** Existing event projection has a single organizer fallback. It does not persist an explicit host role. (Original line 127.)

- [ ] Account page lists events with role badges

  **Open.** Event role badges are absent. (Original line 128.)

##### R1-11 Accounts list view

- [ ] Columns: Name, Domain, Type, Platform, Source(s), Next event, Events/yr, Members, Fit score, Owner, Stage, Leads unlocked / total

  **Partial.** Assignments shows core owner, stage, domain, type, sources, next event and fit. Platform, events per year, members and lead totals are missing there. (Original line 141.)

- [ ] Filters: Type, Platform, Source, Next event window (0-3 / 4-9 / 10-18 months / none), Owner (incl. Unassigned), Stage, Region, Fit score range, Show suppressed

  **Partial.** Tested filters include Type, Source, Owner, Stage and Show suppressed. Platform, next-event window, Region and fit range are missing. (Original line 142.)

- [ ] Default sort: fit score desc, then next event asc

  **Partial.** Fit score can be sorted descending. Default is Name and the fit score tie-breaker is Name rather than next event. (Original line 143.)

- [ ] Bulk select: assign owner, change stage, disqualify, enrich (enrich respects suppression)

  **Partial.** Bulk assignment is implemented and tested with revision and suppression checks. Bulk stage change, disqualify and enrichment are missing. (Original line 144.)

- [ ] Saved views: admin can save the current filter set with a name (used as "plays" in R5)

  **Partial.** Campaigns supports named saved sales views. Saving the active Accounts filter set directly is missing. (Original line 145.)

- [ ] Events list defaults to upcoming events (start date >= today); past events available via filter

  **Partial.** Saved events can be date-sorted, with a next-event calculation. There is no upcoming default plus past filter on the full Events list. (Original line 146.)

##### R1-13 Scheduled source runs and background worker [migration]

- [ ] **Standard source interface:** every built-in scraper is a module implementing `run(source_config, cursor) -> {records, new_cursor, stats}`; records are written to `raw_records` staging and go through the same promotion path as uploads and the ingest API (normalize -> suppression -> match / merge -> classify). Adding a new scraper means adding one module, nothing else

  **Partial.** Normalized saved-source adapters, uploads and external intake all stage data. The proposed one-module collector interface and fully unified promotion are incomplete. (Original line 159.)

- [ ] **Separate worker process:** scraping and crawling run in a background worker (separate Render background worker or cron job service on the same Neon database), never inside the web app process, so the SDR dashboard is never slowed by a run

  **Partial.** An independent offline worker runs saved-source and qualification jobs. The deployed Render and Neon live scraping setup is pending. (Original line 160.)

- [ ] **Job queue** in Postgres (no extra infrastructure); jobs survive restarts; failed jobs retry up to 3 times with backoff

  **Partial.** Durable SQLite jobs, restart recovery and three scheduled retries with backoff are verified. Postgres queue deployment is pending. (Original line 161.)

- [ ] **Schedule per source** set on the Sources screen: cron-style schedule (presets: daily, weekly on a chosen day, monthly), enabled / disabled toggle, "Run now" button, next run time shown

  **Partial.** Sources supports daily, weekly and monthly presets, pause, next time and manual saved preview. A live Run now flow remains open. (Original line 162.)

- [x] **No overlapping runs:** a lock per source; a scheduled run is skipped (and logged) if the previous run is still going

  **Verified locally.** Per-source locks and logged scheduled skips passed tests/test_source_scheduling.py, including a scheduled occurrence during an active run. (Original line 163.)

- [ ] **Incremental runs:** each source stores a cursor (last seen date / page / ID); runs fetch only new or changed listings where the target site allows it; a full refresh can be triggered manually

  **Partial.** Saved imports resume by batch cursor after interruption. Live incremental fetch and full-refresh control are pending. (Original line 164.)

- [ ] **Change detection:** each staged record is marked new / changed / unchanged by comparing a content hash with the last version; for events, changed start date or location updates the event and the account's `next_event_date`; listings not seen for 3 consecutive runs are marked `gone` (not deleted)

  **Partial.** Saved replays detect unchanged facts and event date changes flow into next-event reads. Full content-hash change reporting and gone-after-three logic are open. (Original line 165.)

- [ ] **Health alerts** to admins (email and in-app): run failed after retries; run yield below 30% of the average of the last 5 runs; zero records returned. These usually mean the target site changed layout and the scraper needs fixing

  **Partial.** In-app final-failure and low-yield notices are tested. Email alerts and live collector zero-yield validation are pending. (Original line 166.)

- [ ] **Downstream effects:** new events on an existing account in the 4-9 month window create a task for the account owner (if assigned); new accounts enter at stage Discovered; run summary (new, changed, gone, merged, suppressed, errors) recorded in `source_runs` and shown on the Sources card

  **Partial.** New accounts default to Discovered and worker run counts and Today planning signals exist. Durable owner tasks and complete change metrics are open. (Original line 167.)

- [ ] Cvent scraper converted to the standard interface and scheduled **weekly**; existing single-URL extractor and live console keep working

  **Partial.** Weekly calendar configuration and saved Cvent previews are verified. The live scheduled collector, single-URL action and console are not integrated. (Original line 168.)

##### Release 1b demo checklist

- [ ] Review queue shows the known bad records (organizer "liachaptercommunications", event Sep 2026 -> Sep 2031) and can fix them

  **Partial.** Generic Cvent review and link decisions work. The named organizer is absent from the trial data and exact date correction is unverified. (Original line 178.)

- [x] UI is source-neutral (Accounts, Sources; no "Cvent Companies")

  **Verified locally.** Verified Accounts, My Accounts and Sources use the same source-neutral workflow for both saved data sources. (Original line 179.)

- [ ] Accounts list filters by Type = association and Platform = cvent and returns AARP, ABA, IIA, NFSA, SIFMA etc.

  **Partial.** Type and Cvent source filters are available. Platform filtering and the complete named result set are unverified. (Original line 180.)

- [x] Sources screen shows Cvent with run history and yield

  **Verified locally.** The running local Sources and run-detail screen show staged Cvent history and yield. The trial snapshot has one Cvent run with 542 staged records. (Original line 181.)

- [ ] An agency-run event shows both the agency (producer) and the client (host) on the event

  **Open.** The event screen has no agency producer plus client host relation. (Original line 182.)

- [ ] Enriching an association pulls Executive Director / Meetings personas, not corporate CMO personas

  **Partial.** Offline association title selection works. Real Apollo persona selection and tier rules are pending. (Original line 183.)

- [ ] Cvent source scheduled weekly; "Run now" triggers a run in the worker while the SDR dashboard stays responsive; a second "Run now" during a run is refused

  **Partial.** Weekly saved preview configuration, separate-worker manual staging and overlapping-run denial are tested. A recurring live Cvent schedule is pending. (Original line 184.)

- [ ] A rerun picks up only new / changed listings; a changed event date updates the account's next event

  **Partial.** Saved replays preserve links and update next-event reads from changed saved dates. A live incremental rerun has not been verified. (Original line 185.)

- [ ] Forcing a run with zero results sends a health alert to the admin

  **Partial.** Missing or empty input fails visibly in Sources and low yield has an in-app alert. A live email alert was not sent. (Original line 186.)

- [x] SDR workspace unaffected: Today, My Accounts, logging all still work

  **Verified locally.** Separate manager and SDR trial checks verified Today, My Accounts and logging after the saved-source work. See docs/workflow-delivery.md and working.md. (Original line 187.)

#### R3 HubSpot handoff

[Original story file](stories/05_R3_hubspot_handoff.md)

##### R3-01 HubSpot connection and settings

- [ ] Admin settings: HubSpot private app token (stored encrypted), pipeline, initial deal stage, deal name template (default "{account} - Outbound - {type}")

  **Open.** No live HubSpot settings for private app token encryption, pipeline, deal stage or naming template (docs/workflow-delivery.md). (Original line 19.)

- [ ] "Test connection" button verifies the token and lists pipelines / stages

  **Open.** No HubSpot connection-test or pipeline/stage selector is implemented. (Original line 20.)

- [ ] Map each AE user to a HubSpot owner ID and a region

  **Open.** Local receiving AE users exist. A HubSpot owner ID and region mapping is absent. (Original line 21.)

- [ ] Custom HubSpot properties created if missing (on company and deal): `outbound_source`, `outbound_play`, `outbound_account_type`, `outbound_platform`, `outbound_sdr`, `outbound_tool_id`

  **Open.** No provisioner creates the requested HubSpot company and deal properties. (Original line 22.)

##### R3-02 Push handoff to HubSpot

- [ ] On handoff create (and via a "Retry push" button):
  1. Company: search by domain; create if absent; set custom properties
  2. Contact: search by email; create if absent; associate to company
  3. Deal: create in configured pipeline / stage, owner = AE, associated to company and contact
  4. Note on deal: positive type, meeting time, SDR context note, account "Why this account" signals, last 10 activities
  5. If a meeting is booked, create a HubSpot meeting engagement at that time

  **Partial.** A durable local queue, failure capture and retry exist. Live HubSpot company/contact/deal matching, creation and note attachment are missing (sales_extensions.py, worker.py). (Original line 35.)

- [ ] Stores `hubspot_company_id`, `hubspot_contact_id`, `hubspot_deal_id` on account / person / handoff

  **Open.** No HubSpot company, person or deal IDs are stored. (Original line 41.)

- [ ] `handoffs.hubspot_status`: pending / pushed / failed (with error message); failures visible to admin with retry

  **Partial.** Local pending, delivered and failed statuses with visible errors and staff retry work. The HubSpot push state and provider delivery are missing (sales_extensions.py, tests/test_sales_extensions.py). (Original line 42.)

- [ ] Idempotent: retrying never creates duplicate companies, contacts or deals

  **Partial.** A local retry after a simulated accepted request creates one delivery receipt. HubSpot record upsert and duplicate prevention are unverified (tests/test_sales_extensions.py). (Original line 43.)

- [ ] Person status -> handed_off; account stage -> Handed off

  **Partial.** Local delivery changes contact and account to Handed off. The automatic transition from a successful HubSpot push is missing (sales_extensions.py, tests/test_sales_extensions.py). (Original line 44.)

- [ ] If the company already has an open deal in HubSpot, do not create a new deal; attach the note and contact to the existing deal and flag the handoff "existing deal" for the manager

  **Open.** No existing HubSpot deal lookup, note/contact attachment or Existing deal flag. (Original line 45.)

##### R3-03 AE routing

- [ ] Default AE = the SDR's suggested AE if set; otherwise by account region, round robin among active AEs in that region; fallback to a default AE set in settings

  **Partial.** An SDR can select an active AE. Regional round robin, configured default and optional suggested-AE routing are not implemented (sales_extensions.py). (Original line 58.)

- [ ] Manager can reroute a handoff before or after push; reroute updates the HubSpot deal owner

  **Open.** No handoff reroute action or HubSpot owner update is available. (Original line 59.)

- [ ] Routing decision logged in audit log

  **Partial.** The local handoff audit records the manually chosen AE. Regional/fallback routing decisions have no implementation to audit (sales_extensions.py). (Original line 60.)

##### R3-04 Inbound sync - suppression

- [ ] Nightly job pulls: companies with lifecycle stage = customer; companies with any open deal

  **Open.** No HubSpot customer or open-deal sync job. (Original line 73.)

- [ ] Their domains are written to suppression with reason customer / open_deal and source = hubspot

  **Open.** No HubSpot-driven suppression entries or source mapping. (Original line 74.)

- [ ] Matching accounts flagged suppressed; if such an account is assigned to an SDR, the SDR sees a banner "Customer / open deal in HubSpot - do not contact" and outreach actions are disabled

  **Partial.** Existing local suppression blocks outreach and shows a reason banner. The HubSpot-specific trigger, flag and banner text are missing (tests/test_suppression_visibility.py). (Original line 75.)

- [ ] Entries removed from suppression when no longer true (deal closed lost, customer churned), except manual entries

  **Open.** No reconciliation removes expired HubSpot suppression while retaining manual entries. (Original line 76.)

- [ ] Manual "Sync now" button for admin; last sync time shown

  **Open.** No HubSpot Sync now action or last sync time display. (Original line 77.)

##### R3-05 Inbound sync - deal outcomes

- [ ] Nightly job updates each handoff with current deal stage, amount, close date, won / lost

  **Open.** No scheduled HubSpot deal stage, amount, close date or outcome synchronization. (Original line 90.)

- [ ] Account stage reflects outcome: Won / Lost added as terminal stages

  **Open.** No HubSpot outcome drives the account to a Won or Lost terminal stage. (Original line 91.)

- [ ] Closed-lost accounts move to Nurture after 90 days and return to the SDR pool

  **Open.** No 90-day closed-lost recycling into Nurture and the SDR pool is implemented. This business timing remains applicable. (Original line 92.)

##### Release 3 demo checklist

- [ ] Mark Positive on a test contact creates company, contact, deal and note in HubSpot within one minute, owned by the right AE

  **Open.** Creation of correctly owned company, contact, deal and note in HubSpot is unimplemented. The active brief retired the one-minute demo threshold. (Original line 101.)

- [ ] Retry on the same handoff does not duplicate anything

  **Partial.** Local retry idempotency passed with a service double. A HubSpot retry with zero duplicate CRM objects remains unverified (tests/test_sales_extensions.py, docs/workflow-delivery.md). (Original line 102.)

- [ ] Marking positive on an account with an open HubSpot deal attaches to that deal instead

  **Open.** No HubSpot open-deal lookup or attach-to-existing demo. (Original line 103.)

- [ ] A HubSpot customer domain appears in suppression after sync and its account is blocked for outreach

  **Open.** No test HubSpot customer sync or imported customer suppression. (Original line 104.)

- [ ] Moving the test deal to Closed Won in HubSpot shows Won on the account after sync

  **Open.** No Closed Won writeback from the HubSpot test deal. (Original line 105.)

#### R4 Association and ingestion

[Original story file](stories/06_R4_associations_source.md)

##### R4-01 File upload source (CSV / Excel) [migration]

- [ ] Upload accepts .csv, .xlsx and .xls up to 50,000 rows; for multi-sheet Excel files, admin picks the sheet

  **Partial.** CSV and XLSX accept up to 1,000 rows and use the first sheet. XLS, 50,000 rows and sheet selection are missing. (Original line 36.)

- [ ] Each upload is attached to a source: pick an existing source or create a new one (name, kind = file_upload). Each upload creates a `source_runs` row with the file name, uploader and row counts

  **Partial.** Upload saves its filename, row data and actor audit in the common File uploads source. Source selection or creation and full run metadata are missing. (Original line 37.)

- [ ] Column mapping screen: map file columns to account fields (name, website / domain, type, country, state / region, city, members, phone, LinkedIn company URL) and optional people fields (first name, last name, full name, title, email, phone, LinkedIn URL)

  **Partial.** Fixed normalized headers recognize core organization and contact fields. An interactive mapping form and the complete field catalog are missing. (Original line 38.)

- [ ] Auto-suggests mappings from header names (e.g. "Website", "URL", "Domain" -> website); unmapped columns can be kept and are stored in `account_attributes` under their header name

  **Partial.** Header names are normalized and unused values remain in raw row evidence. Alias mapping suggestions and account attribute promotion are missing. (Original line 39.)

- [ ] Mappings saved as a template per source, so the next upload from the same source maps automatically

  **Open.** No saved upload mapping templates. (Original line 40.)

- [ ] Upload-level defaults: type (e.g. association), platform (e.g. unknown or cvent), region, tags; applied where the row has no value

  **Open.** No upload-level defaults form. (Original line 41.)

- [x] All rows first land in a staging table `raw_records` (run_id, row_number, data JSON, status); nothing touches accounts until the admin confirms

  **Verified locally.** CSV and XLSX stage source_run_rows before administrator confirmation. Verified by test_sources.py. (Original line 42.)

- [ ] **Preview before import** shows counts and a sample of each: new accounts, will merge into existing accounts, suppressed, invalid (no name and no domain, malformed domain), duplicate rows within the file

  **Partial.** Preview distinguishes existing and new matches, suppression, invalid rows and conflicts. Full prospective category totals and file-duplicate samples are missing. (Original line 43.)

- [ ] Optional row filters on the preview screen (e.g. column "Members" >= 1000, column "Type" in [...]) so only a subset is promoted; filtered-out rows stay in staging and can be promoted later

  **Partial.** Preview can filter displayed result rows. The promotion confirmation still promotes the whole eligible run. (Original line 44.)

- [ ] On confirm: rows go through normalize -> suppression -> match / merge (R1-02) -> classification (R1-07 unless type is mapped or defaulted); `account_sources` records the upload source and row

  **Partial.** Promotion normalizes, suppresses, matches safe identities and links raw evidence. Governed root-domain merge and full classification/defaults are pending. (Original line 45.)

- [ ] People rows are created with `origin = upload`, email status `unverified`, linked to their account; duplicate email within the account is skipped

  **Partial.** Same-account email duplicates are skipped and imported email is unverified. Local origin is source for upload and API contacts. (Original line 46.)

- [ ] Re-uploading the same file (same hash) warns before importing; re-importing updates existing records rather than duplicating

  **Partial.** Stable source identity makes updated reimports idempotent. File-hash detection and duplicate-file warning are missing. (Original line 47.)

- [ ] Downloadable error report (row number, reason) for invalid and skipped rows

  **Open.** Row problems appear in the run page. A downloadable error report is missing. (Original line 48.)

- [x] Admin only (managers optional via permission); every upload written to the audit log

  **Verified locally.** Admin upload is checked server-side and staging is audited. Verified by authenticated Sources workflow in test_sources.py. (Original line 49.)

##### R4-02 Import the scraped Directory of Associations data

- [x] Registered as a source "Directory of Associations" (kind = directory)

  **Verified locally.** Directory of Associations is registered with the directory adapter and shown in Sources. (Original line 63.)

- [ ] First load: the external script's existing output (CSV / Excel) is uploaded through R4-01 with a saved mapping template for this source. From R4-08 onward the scraper runs inside the platform monthly; both land in the same staging and promotion flow

  **Partial.** The saved Directory JSON uses shared staging and promotion. Upload mapping templates and monthly live collection are pending. (Original line 64.)

- [ ] Full dataset loaded into `raw_records`, keeping all scraped fields: name, website, city, state / country, association type, category, member band, listing URL, last updated

  **Partial.** The US JSON is fully staged with original organization and selected evidence fields. Remaining saved exports and a full all-field import are pending. (Original line 65.)

- [x] Promotion filter applied when moving staging rows to accounts (admin-editable, saved on the source):
  - types: International, National, State
  - members: 1,000+
  - categories included: professional, trade, medical, industry, business, technology, finance, legal, engineering, education (admin-editable list)
  - categories excluded: clubs, alumni, fraternity / sorority, YMCA / YWCA, local, hobby, religious, support groups

  **Verified locally.** Editable Directory promotion rules persist per source and apply type, membership and category filters. Verified by test_sources.py and test_directory_import.py. (Original line 66.)

- [ ] Promoted rows: `type = association`, `platform = unknown`; directory fields stored in `account_attributes` (directory_category, directory_type, member_band, listing_url); member band lower bound copied to `members` when no better figure exists

  **Partial.** Directory-only associations use Unknown platform, and original member bands and listing attributes are displayed. A canonical member count and full account_attributes projection are missing. (Original line 71.)

- [x] Rows that do not pass the filter stay in staging and can be promoted later by changing the filter, without re-scraping

  **Verified locally.** Filtered rows remain staged. A lower threshold promoted an eligible held row from the same saved file with no scrape or duplicate in a fresh disposable run. (Original line 72.)

- [ ] Merge with existing Cvent accounts by root domain (e.g. AARP, ABA, IIA) adds the directory source and attributes without overwriting Cvent data

  **Partial.** AARP has both sources with Cvent retained. Broader governed root-domain reconciliation and verified ABA/IIA crosswalks are pending. (Original line 73.)

- [ ] Sources card shows: total staged, promoted, merged with existing, suppressed, not promoted

  **Partial.** Run detail shows total and classified counts. The source card lacks promoted-versus-merged totals, and run results combine new and refreshed records. (Original line 74.)

- [x] If the directory is re-scraped later, a new load updates existing records and adds new listings only

  **Verified locally.** Stable Directory listing IDs preserve matched links and refresh changed evidence. New eligible listings can add accounts. Replay verified in test_directory_import.py and the delivery record. (Original line 75.)

##### R4-03 Website qualification crawl

- [ ] For each account with a website and no crawl in the last 90 days, fetch: home page, and up to 5 pages whose URL or link text matches conference / annual meeting / convention / expo / events / register / summit

  **Open.** Saved HTML can be uploaded. Live multipage fetching and the 90-day refresh window are missing. (Original line 88.)

- [ ] Extract with rules and pattern matching (no LLM):
  - `runs_conference`: yes if any fetched page title, heading or nav link contains annual conference / annual meeting / convention / summit / expo / congress / symposium / forum; event name taken from that heading
  - next event start date and location: date patterns (e.g. "March 3-5, 2027", "3-5 March 2027", "2027-03-03") found near the event name or on the event page; take the earliest date >= today; location from text matching "City, State" or a venue line near the date. If no date parses, store `runs_conference = yes` with `next_event_date` empty
  - registration platform, detected from registration link domains and page source: cvent.com / cventevents.com -> cvent; also detect swoogo, eventbrite, a2zinc, mapyourshow, momentus, eventsair, bizzabo, hopin / ringcentral events, whova, accelevents, memberclicks, yourmembership, personify, fonteva, impexium, nimble ams, eshow, eventscribe; others recorded as other_detected with the domain
  - member count if stated: patterns like "over 25,000 members", "representing 4,000+ professionals", "membership of 12,000"; take the largest number adjacent to member / members / professionals / companies
  - exhibitor / sponsor program (yes / no): links or headings containing exhibit / exhibitor / sponsor / sponsorship / prospectus / booth

  **Partial.** Deterministic conference heading and registration-domain extraction works for saved evidence. Requested date, location, membership, sponsor and platform coverage are incomplete. (Original line 89.)

- [ ] Each finding saved to `account_signals` with value, evidence URL, observed date, confidence (high: platform from link domain; medium: keyword or pattern match; low: inferred)

  **Partial.** Dated qualification records store score signals with evidence URLs. Per-finding values and confidence are incomplete. (Original line 95.)

- [ ] Patterns and keyword lists live in one config-driven module; a test suite of 20+ saved association pages checks extraction results so pattern changes do not regress

  **Partial.** Conference terms and known platform domains are editable. The 20-page extraction regression corpus is missing. (Original line 96.)

- [ ] Account fields updated: platform, platform_detail, platform_confidence, next_event_date, members, events_per_year (if derivable)

  **Partial.** Account view shows saved platform confidence, Directory members and dates from linked Cvent events. Crawl-derived event dates, platform detail and frequency are missing. (Original line 97.)

- [ ] Discovered events saved to `events` and linked via `event_orgs` (role = host)

  **Open.** No extraction and host linking for newly discovered crawl events. (Original line 98.)

- [ ] Crawl runs as a queued background job with concurrency limit; failures logged per account

  **Partial.** Saved qualification is batch-queued, resumable and has job-level errors. Live crawl, concurrency control and per-account errors are missing. (Original line 99.)

- [ ] Also runnable on Cvent-sourced accounts to fill members / next event

  **Partial.** Saved qualification runs on Cvent accounts. Live crawl is needed to find members and additional dates. (Original line 100.)

##### R4-04 Fit scoring

- [ ] Scoring weights stored in config (editable by admin), seeded from architecture doc section 4.4

  **Partial.** Administrators edit conference terms, platform domains and budgets. Score weights are code-defined and differ from the full architecture. (Original line 113.)

- [ ] Score recomputed when signals change and nightly (timing points change as dates approach)

  **Partial.** Saved evidence can be rescored manually. Automated signal-change and nightly recomputation are missing. (Original line 114.)

- [x] Score breakdown visible on the account page (which signals contributed how many points)

  **Verified locally.** Account qualification shows evidence labels, point amounts and links. Verified by the saved HTML and AARP flows in test_growth_ops.py. (Original line 115.)

- [ ] Stage set automatically: score >= 40 -> Qualified; < 40 -> Disqualified (reason: low fit), unless a manager has overridden

  **Partial.** The default score cutoff and stored override affect safe unassigned stage changes. A manager override screen and stored low-fit reason are missing. (Original line 116.)

##### R4-05 Credit-gated auto-enrichment

- [ ] Score >= 60: queued for Apollo enrichment automatically using association personas

  **Partial.** High score can queue offline enrichment when enabled on a manual qualification. Live Apollo and approved persona selection are pending. (Original line 129.)

- [ ] Score 40-59: appear in an "Approve enrichment" list for manager / admin with bulk approve

  **Partial.** A manager can queue a midrange account individually. An approval list and bulk action are missing. (Original line 130.)

- [ ] Admin sets a daily auto-enrichment credit budget; queue pauses when reached and resumes next day

  **Partial.** Daily automatic budget is configurable and checked at queue and claim. Exhausted auto-enrichment offers no retained approval queue for automatic next-day resumption. (Original line 131.)

- [ ] Enrichment respects suppression and writes to the credit ledger

  **Partial.** The offline worker rechecks suppression and writes to the single ledger. Paid Apollo behavior remains pending. (Original line 132.)

- [ ] Accounts with zero persona matches in Apollo flagged "no contacts found" for manual research

  **Open.** The offline double returns a synthetic contact. Provider zero-match flagging for research is missing. (Original line 133.)

##### R4-06 Cross-source merge visibility

- [ ] Account page header lists all sources with first-seen dates

  **Partial.** Account header shows every linked source. Authoritative first-seen dates for each source are pending. (Original line 146.)

- [x] Accounts list "Source" filter supports "2+ sources"

  **Verified locally.** Manager Assignments has a 2+ sources filter. Verified by test_bulk_assignment.py and the saved AARP screen check. (Original line 147.)

- [x] Merge of a directory record into an existing Cvent account keeps Cvent platform = confirmed and adds directory attributes (members, category)

  **Verified locally.** AARP exposes the Directory members and category alongside the confirmed Cvent platform. Scope and field display verified in test_account_profile.py and the saved trial. (Original line 148.)

##### R4-07 Ingest API for external scrapers [migration]

- [x] Per-source API key, created and revoked by admin from the Sources screen; keys stored hashed

  **Verified locally.** Admin can issue and revoke a hashed key per source. Authenticated lifecycle and key scoping tests pass in test_sources.py and test_scraper_ingest.py. (Original line 161.)

- [x] `POST /api/ingest/{source_id}/runs` starts a run and returns a run_id; `POST /api/ingest/{source_id}/runs/{run_id}/records` accepts batches of up to 1,000 records (JSON, same field names as the R4-01 mapping); `POST .../complete` closes the run

  **Verified locally.** The documented open, records and complete endpoints work with the shared normalized upload headings and a 1,000-row request limit. Verified in test_scraper_ingest.py. (Original line 162.)

- [ ] Records land in `raw_records` staging; promotion uses the source's saved filter and defaults (same logic as R4-01 / R4-02), either automatically on run complete (setting per source) or after admin review

  **Partial.** External batches stage original rows and have a per-source promotion mode. Arbitrary saved source filters and field defaults are missing. (Original line 163.)

- [ ] Idempotent: a record with the same source-side ID or root domain updates the staged record instead of duplicating

  **Partial.** Replays preserve stable IDs and safe normalized domain plus organization-name matches. No general root-domain dedupe with different names. (Original line 164.)

- [ ] Validation errors returned per record in the response; run summary visible on the Sources card

  **Partial.** Per-row validation errors and run detail counts work. The source card itself lacks an aggregate external run summary. (Original line 165.)

- [x] Rate limit per key; all calls logged

  **Verified locally.** Per-key rate limit and endpoint request logging verified by test_scraper_ingest.py. (Original line 166.)

- [ ] Short integration doc in `docs/ingest-api.md` with an example payload, so any script (Python, Node) can be linked in under an hour

  **Superseded.** docs/ingest-api.md verifies the documentation with sample requests, batching, receipts and replay. The under-an-hour acceptance timer is superseded by the engineering brief. (Original line 167.)

##### R4-08 Directory of Associations as a scheduled built-in source

- [ ] The existing external directory script is ported into a source module implementing the standard interface (R1-13); same fields as today's output

  **Open.** The local adapter stages saved Directory exports. Live collector connection and standalone connector policy remain pending. (Original line 181.)

- [ ] Scheduled **monthly** (the directory publishes new listings monthly); incremental where possible using the directory's "recently updated" listings, with a full refresh available manually

  **Partial.** Monthly configuration and manual saved preview are verified in test_source_scheduling.py and test_sources.py. Incremental live collection by publication date is missing. (Original line 182.)

- [ ] Rate limited (configurable delay, default 2-5 seconds random) and resumable from the last page if interrupted

  **Open.** Per-page live collection throttle and cursor are missing. Saved promotion batch recovery is available. (Original line 183.)

- [ ] Writes to the same "Directory of Associations" source and staging used by R4-02, so earlier uploaded data and new runs merge cleanly (no duplicates)

  **Partial.** Saved Directory runs share one source and stable listing IDs, with verified repeat promotion. No live run has joined that staging path. (Original line 184.)

- [ ] Promotion filter from R4-02 applied automatically on each run; newly promoted accounts are queued for the website qualification crawl (R4-03)

  **Partial.** Saved promotion uses its recorded filters. Live automatic promotion and follow-on crawl enqueue are missing. (Original line 185.)

- [ ] Health alerts from R1-13 apply

  **Partial.** Local source health shows failures, stalled work, retries and low yield. Live alerts for Directory are pending. (Original line 186.)

- [ ] Once this is live, the external script is retired; the ingest API (R4-07) remains for future scrapers that start outside the platform

  **Partial.** The external ingest API works. Directory remains a separate collector and its live connector lifecycle is pending. (Original line 187.)

##### Release 4 demo checklist

- [ ] Upload a sample Excel file with a new source: mapping auto-suggested, preview shows new / merge / suppressed / invalid counts, import creates accounts and people, error report downloads

  **Partial.** CSV/XLSX preview and confirmed import create accounts and people locally. New upload-source selection, rich mapping, complete outcome counts and error download are missing. (Original line 196.)

- [ ] Re-upload the same file: warning shown, no duplicates created

  **Partial.** Replay avoids duplicates with stable IDs. Duplicate-file warning is missing. (Original line 197.)

- [ ] Load the scraped directory data; Sources card shows staged vs promoted counts with the default filter; changing the filter promotes more rows without re-scraping

  **Partial.** A disposable run verified default and relaxed Directory filters against the same saved file, with staged and promoted counts on run detail. Those counts are not shown on the source card. (Original line 198.)

- [ ] Associations already in the Cvent data (e.g. AARP, ABA) merged, not duplicated, and show both sources

  **Partial.** AARP is verified as one account with two sources. The full named overlap and general root-domain path are pending. (Original line 199.)

- [ ] Qualification crawl run on the batch; at least some associations detected with platform = cvent from their registration links

  **Partial.** Saved HTML with a Cvent registration link qualifies through deterministic parsing. Live crawl across the Directory batch is missing. (Original line 200.)

- [ ] Account page shows evidence links for conference, platform and next event

  **Partial.** Account page shows linked Cvent events, evidence links and next event. The broader cohort lacks crawl-derived evidence for all three fields. (Original line 201.)

- [ ] Score breakdown visible; high scorers auto-enriched with association personas; mid scorers waiting for approval

  **Partial.** Fit breakdown and single-account approval exist, and high-score enrichment is offline. Live association personas and approval queue are missing. (Original line 202.)

- [ ] Daily auto-enrichment budget stops the queue when reached

  **Partial.** Local auto-budget checks block submissions above the cap. Live automatic queue pause and resumption are pending. (Original line 203.)

- [ ] A test script posts 50 records to the ingest API; they appear in staging, promote according to the source filter, and re-posting the same records creates no duplicates

  **Partial.** test_scraper_ingest.py stages and promotes 50 records and replays with zero duplicates. Additional arbitrary per-source filters are missing. (Original line 204.)

- [ ] Directory source scheduled monthly inside the platform; a manual run merges with the previously uploaded data without duplicates and queues new associations for the qualification crawl

  **Partial.** Monthly saved-export scheduling and repeat promotion are verified. Live built-in collection and crawl enqueue are missing. (Original line 205.)

#### R5 Campaigns and reporting

[Original story file](stories/07_R5_campaigns_reporting.md)

##### R5-01 Sequencer adapter

- [ ] Interface: list sequences, add contacts to sequence (with custom fields), remove / pause contact, receive webhook events

  **Partial.** Signed callback intake exists. Sequence listing, enrollment, removal and provider pause are missing. (Original line 19.)

- [ ] First implementation for the chosen sequencer

  **Open.** Sequencer vendor and live adapter are pending. (Original line 20.)

- [ ] Credentials stored encrypted in admin settings, with a test connection button

  **Open.** No live connection settings, encrypted provider credentials or test button. (Original line 21.)

- [ ] Custom fields passed per contact: first name, account name, account type, platform, next event name and date, SDR name (for personalization)

  **Open.** No outbound personalization payload to a sequencer. (Original line 22.)

##### R5-02 Campaigns and plays [migration]

- [ ] Saved views from R1-11 can be marked as **plays** with a name, description and message angle

  **Partial.** Named views can seed local campaigns. Play marking, description and message angle are missing. (Original line 35.)

- [ ] Seeded plays (architecture doc 4.5): Displacement - associations; Displacement - corporate; Agency multiplier; Greenfield - associations; Timing window

  **Open.** The five specified plays are not seeded. (Original line 36.)

- [ ] Table `campaigns` (id, name, play_id, channel: email / linkedin / call / multi, sequencer_sequence_id, owner_id, status: draft / active / paused / done, created_at)

  **Partial.** The campaigns table stores name, saved view, creator and date. Channel, sequencer sequence, owner and workflow status fields are missing. (Original line 37.)

- [ ] Table `campaign_members` (campaign_id, person_id, added_by, added_at, status: active / paused / finished / removed, removed_reason)

  **Partial.** Campaign membership stores person, actor and date with active and paused states. Finished, removed and reason are missing. (Original line 38.)

- [ ] Manager creates a campaign from a play and links it to a sequencer sequence

  **Partial.** Managers create campaigns from local saved views. A provider sequence link is missing. (Original line 39.)

- [ ] Adding members: from a play (all eligible people matching persona tiers selected) or from account / contact pages (SDR, own accounts only)

  **Partial.** SDRs can enroll accessible eligible contacts from the campaign screen. Bulk persona enrollment and account/contact-page actions are missing. (Original line 40.)

- [x] Eligibility checks: unlocked, verified email, not suppressed, not do_not_contact, not already active in another campaign, account not handed off

  **Verified locally.** Local membership enforces unlocked verified contacts, suppression and opt-outs, existing active enrollment and handed-off stage. Verified by test_campaigns.py and a disposable HTTP check. (Original line 41.)

##### R5-03 Webhook activity logging

- [x] Webhook endpoint (verified signature) receives: sent, opened, clicked, replied, bounced, unsubscribed

  **Verified locally.** A disposable HTTP check accepted all six signed event types and rejected an unsigned submission. Vendor connection remains in R5-01. (Original line 54.)

- [ ] Sent and replied create `activities` rows (source = webhook); opens and clicks stored as counters on the campaign member, not as activities

  **Partial.** Sent and replied create activities. Opens and clicks stay in the event log, separate from activities, without member counter fields or a dedicated activity source field. (Original line 55.)

- [ ] Reply -> person status replied, appears at top of the SDR's Today queue, contact paused in sequence

  **Partial.** The signed local reply marks Replied, appears in Today and pauses local membership. Provider-side pause is pending. Verified in test_campaigns.py. (Original line 56.)

- [x] Bounce -> person bounced + do_not_contact; unsubscribe -> opted_out + suppression

  **Verified locally.** Signed bounce and unsubscribe set the respective person status and do-not-contact, with email suppression. Verified via disposable HTTP and test_campaigns.py. (Original line 57.)

- [x] Idempotent on duplicate webhook deliveries

  **Verified locally.** Duplicate event IDs with identical payload are idempotent, and conflicting reuse is rejected. Verified in test_campaigns.py. (Original line 58.)

##### R5-04 LinkedIn and call steps in campaigns

- [ ] Campaign can define manual steps (day offset, type: linkedin connect / linkedin message / call, instruction text)

  **Open.** No campaign manual-step definition. (Original line 71.)

- [ ] When a member reaches a manual step day, a task is created for the account owner

  **Open.** No date-driven campaign task creation. (Original line 72.)

- [ ] Completing the task via Log activity advances the step

  **Open.** No completion-driven step advancement. (Original line 73.)

##### R5-05 Admin funnel dashboard

- [ ] Funnel stages (accounts): discovered -> qualified -> enriched -> assigned -> working -> engaged -> positive -> handed off -> won

  **Partial.** Operations has a limited funnel by source with discovered, scored, assigned, worked, positive and delivered counts. Full lifecycle and won stages are missing. (Original line 86.)

- [ ] Breakdown selector: source, account type, platform, play, rep, region; date range filter

  **Partial.** Source is supported. Other dimensions and date-range selection are missing. (Original line 87.)

- [ ] Metrics per row: count at each stage, stage-to-stage conversion, credits spent, credits per positive, positives per 100 contacts touched, HubSpot pipeline value and won value (from R3-05)

  **Partial.** Source counts, credits and credits per positive are present. Conversion, touches-normalized positives and HubSpot values are missing. (Original line 88.)

- [ ] Rep table: accounts owned, touches (7 / 30 days), replies, positives, handoffs, won, credits used

  **Partial.** Operations shows owned accounts and task workload. Full rep activity, conversions and spend table is missing. (Original line 89.)

- [ ] Campaign table: members, sent, reply rate, positive rate, bounces

  **Partial.** Campaign detail shows members, sent, replies and combined bounces and opt-outs. Reply rate, positive rate and distinct bounces are missing. (Original line 90.)

- [ ] CSV export of every table (admin only, audited)

  **Partial.** Admin can export an audited source funnel CSV. All-table export is missing. (Original line 91.)

##### R5-06 Timing triggers

- [ ] Nightly job: accounts whose next event date crosses into 9 months out and have no activity in 30 days create a task for the owner ("Event in planning window: {event} on {date}")

  **Partial.** Today shows an account planning-window cue. Nightly crossing detection, inactivity gate and durable task are missing. (Original line 104.)

- [ ] Unassigned accounts crossing the window surface in the Assignment screen as "In window - unassigned"

  **Partial.** Assignment shows and sorts next-event dates. No In window - unassigned alert. (Original line 105.)

- [x] Window bounds configurable (default 4-9 months)

  **Verified locally.** The 4-9 month default and configurable bounds drive the Today cue. A disposable HTTP check changed the lower bound and verified that the cue disappeared. (Original line 106.)

##### Release 5 demo checklist

- [ ] Create a campaign from the "Displacement - associations" play and push eligible contacts to the sequencer

  **Partial.** Campaigns can be created from local views. The seeded play and live push are missing. (Original line 115.)

- [ ] Suppressed, do-not-contact and already-sequenced people are excluded automatically

  **Partial.** Local membership excludes restricted contacts and duplicate active memberships. Provider-side sequence exclusions are pending. (Original line 116.)

- [ ] A test reply in the sequencer appears at the top of the SDR's Today queue within minutes and pauses the contact

  **Partial.** Signed local replies appear in Today and pause local membership. Live sequencer and provider pause are pending. The elapsed-time target is superseded. (Original line 117.)

- [x] A test bounce marks the person bounced and do-not-contact

  **Verified locally.** A synthetic signed bounce on the local webhook set Bounced and Do not contact in a disposable database. (Original line 118.)

- [x] Funnel shows Cvent vs Directory of Associations side by side with credits per positive

  **Verified locally.** Operations rendered both Cvent and Directory rows with credits per positive in a disposable HTTP check. (Original line 119.)

- [ ] An account crossing into the 9-month window creates a task for its owner

  **Partial.** Event planning appears in Today. Crossing-triggered task creation is missing. (Original line 120.)
