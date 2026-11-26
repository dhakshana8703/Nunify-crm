# R1-06 Account facts, timing, stages and ownership

[Goutham's story](stories/01_R1a_prerequisites.md#r1-06-account-type-platform-timing-and-stage-fields-migration) depends on R1-02. Every update targets the surviving company ID. Source evidence, sales activity, suppression and existing ownership remain attached to that company.

| Check | Behavior | Pass or fail evidence |
| --- | --- | --- |
| R1-06.1 | Persist platform, platform detail, confidence, next event, events per year, members, country and region on the account. Expose the existing organization type, saved fit score, stage and owner from their authoritative records in the same account read model. | Upgrade an existing database and open a mixed-source account. Compare stored facts and the rendered view with the original evidence. An ambiguous value stays unknown, and an unrelated account's work stays intact. |
| R1-06.2 | An account starts at Discovered and supports the ten stage values in the architecture. The existing actions advance the stage when their evidence warrants it and preserve later stages. Confirmed contact discovery can mark an eligible earlier stage Enriched. | Exercise creation, qualification, source replay, confirmed enrichment, assignment, outreach, reply, and handoff. Uncertain, blocked, or repeated operations leave the right stage and work attached. Live HubSpot delivery remains under R3. |
| R1-06.3 | A Manager or Admin continues assigning through the shared account ownership workflow. Backfill verified Cvent Team allocations using stable Cvent company IDs and an explicit mapping to active Nunify SDRs. Preview first, preserve existing owners, block conflicting rows and write one audited transaction. | Use different IDs across databases, unknown or inactive reps, retired company IDs, existing owners, duplicate or conflicting rows, stale plans, repeat and rollback. The earlier 50-row setup batch stays outside the backfill until its ownership is verified. |
| R1-06.4 | Store the earliest valid linked event date on or after the CRM business day. Refresh after event evidence is written, corrected, moved or merged. The worker refreshes once per business day on its first pass, including after downtime. | Add past, today and future events, malformed dates and an undated roster row. Correct one event, move or merge another, replay, then advance the business date through midnight. Check the stored date and normal UI. Roll back a failing source write without changing its facts. |
| R1-06.5 | Linked Cvent event evidence or an approved Cvent company roster link sets `platform=cvent` and `platform_confidence=confirmed`. An unlinked review row cannot set these fields. A single corroborated non-Cvent registration platform stores its name as detail. | Import both Cvent forms, a Directory-only account and conflicting website signals. The Cvent roster entry confirms the platform and contributes zero events. |
| R1-06.6 | New accounts default to Unknown when no explicit supported organization type accompanies the input. Existing types and Admin changes survive migration and replay. Leave automatic classification to R1-07. | Try no type, malformed type, a valid explicit type and a plausible name that would trigger the old heuristic. Verify Admin edit, Manager denial, stale edit and source replay. |
| R1-06.7 | Assignments shows owner, stage and next event with owner and stage filters. SDRs see relevant facts on their assigned accounts under their existing access rules. | Sign in separately as Admin, Manager, owning SDR and peer. Check Unassigned and named owner filters, stage filtering, upcoming dates and account scope. |

## Data and time decisions

- Keep `accounts.organization_type`, `account_work.owner_id` and `account_work.stage`, and `qualifications.score` as authoritative. The account read model exposes them together. This preserves the existing assignment, stage, qualification and merge transactions. The new stored account columns represent facts derived from evidence.
- Use `Asia/Kolkata` for the CRM business day unless `CRM_BUSINESS_TIMEZONE` selects another IANA time zone. Nightly catch-up uses this shared date. The existing user-local date still applies to personal queues. Reads should remain useful if a worker is delayed across midnight.
- Replaying older inputs or finishing qualification after midnight preserves any later business date already recorded on the account. A qualified Directory listing provides an explicit Association type through its source adapter. The Cvent event name alone supplies no type.
- The application applies the Unknown default on every supported account creation path. Fresh schemas and upgraded PostgreSQL also apply the column default. An existing SQLite file keeps its account table definition and data during the additive facts migration.
- Event frequency is the count of distinct, dated, linked events in the trailing twelve calendar months through today. An event exactly one year ago is outside the window. Clamp the boundary to the last day of its month when necessary. Keep the value unknown when there are no valid dated events. A linked company roster row is company evidence and contributes zero events.
- Use consistent company country and exact member counts from linked Directory evidence. Ignore event venue country, ambiguous ranges and contradictory listings. Resolve known country aliases before comparing values. When country is available, derive the territory using the sales regions in the story. Keep unknown and contradictory values available for review.
- Recognize `none` as a platform value when explicit evidence declares no platform. Missing or conflicting evidence is Unknown. Cvent company approval establishes source coverage, and a confirmed Cvent label takes precedence over weaker qualification signals.
- Refresh derived facts inside the source, qualification, merge or transfer transaction. Use the same derivation for startup and the daily refresh. Concurrent writers already serialize through the CRM database adapters.

Rehearse changes with fixtures and an isolated copy of the 769-account database. Provider integrations remain offline for these tests. Keep the original references, live Neon data and production deployment separate.

## Preview verified Cvent allocations

Use a disposable CRM database and an approved map of legacy Cvent user IDs to active Nunify SDR emails. A map can contain a value such as `{"5": "approved-sdr@example.test"}`. Keep the map and export outside Git.

```powershell
python -B import_cvent_allocations.py --db instance/r106-copy.sqlite3 --source cvent-allocations.xlsx --user-map approved-sdrs.json
python -B import_cvent_allocations.py --db instance/r106-copy.sqlite3 --source cvent-allocations.xlsx --user-map approved-sdrs.json --apply --expect 'paste-preview-fingerprint-here' --admin-email admin@example.test
```

The preview lists rows ready to apply, unchanged rows and rows held with reasons. Apply rechecks the fingerprint and authenticates an active Admin. Use `--db -` to enter a PostgreSQL URL at a private prompt for a disposable PostgreSQL rehearsal.

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| R1-06.1 | PASS locally | The SQLite and PostgreSQL account screens used persisted facts. Data preserved across the migration includes identities, owners, stages, tasks, contacts, credit records, suppression and source payloads. |
| R1-06.2 | PASS locally | Both databases validate newly written stages and default to Discovered. Verified discovery advances eligible early stages, respects a later concurrent stage, and leaves existing legacy stage history intact through merge. |
| R1-06.3 | PASS on fixtures, real ownership pending | Both databases passed stable-ID mapping, suppression, inactive user, conflict, stale-preview, replay and rollback checks. Previewed the 50 setup rows with zero eligible. The actual export needs verified allocations and an approved SDR crosswalk. |
| R1-06.4 | PASS locally | Accepted inserts and corrections update the stored date, a merge refreshes the survivor, and a failed write rolls back both evidence and facts. The new-day worker pass, late qualification and malformed legacy-date display all passed. |
| R1-06.5 | PASS locally | All 52 Cvent-linked accounts in the 769-account rehearsal gained confirmed platform. The 20 based on company roster records remain separate from event counts. |
| R1-06.6 | PASS locally | Intake verifies the Unknown default and trusted Directory type. The existing Admin edit and role regression passed. |
| R1-06.7 | PASS locally | Assignments and Admin-to-SDR replay regressions passed. The Manager opened a persisted source-backed profile with the expected platform and sales region. |

The failing baselines reproduced missing persisted facts, a guessed Cvent type, invalid stage handling, an absent ownership import, a stale qualification timestamp that reversed the saved date and an invalid fallback date. The focused suites passed on SQLite and disposable PostgreSQL 17.12 after the changes. The broader SQLite regression passed 61 cases, and the source review, connector, profile boundary and facts policy run passed 19. Starting the new app on an exact `5d2b255` PostgreSQL pilot copy preserved the existing workflow and source rows. Repeat startup and transfer replay also passed. The 769-account SQLite rehearsal preserved the source file and its data, and 12 companies had a saved upcoming date at the rehearsal business day.

The implementation is local. Deploying the updated app and running the hosted worker remain separate from this rehearsal.
