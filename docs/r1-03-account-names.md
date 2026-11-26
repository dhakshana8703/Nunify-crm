# R1-03 Clean account names

[Goutham's acceptance story](stories/01_R1a_prerequisites.md#r1-03-clean-account-names) aims to give SDRs organization names they can use in outreach. This work changes the name of an established account. Identity, contacts, suppression and ownership remain governed by R1-01 and R1-02.

| Check | Behavior | Verification |
| --- | --- | --- |
| R1-03.1 | Resolve names using saved Apollo organization evidence, a linked Cvent organizer, a verified Directory listing, Admin-uploaded website metadata from the organization's own host, then a label derived from the registrable domain. The Directory tier preserves the existing two-source policy. Prefer a valid `og:site_name` to the title from the same page. Show website metadata from external source feeds for explicit review. Hold disagreements in the best available tier for Admin review. Leave an account without a usable domain or trustworthy evidence for review. | Walk each tier with offline fixtures. Reject a different organization's Apollo evidence and shared registration pages. Verify that Cvent and Directory evidence remain linked. |
| R1-03.2 | Persist the chosen value in `accounts.name`, with `name_source` and `name_source_ref`. Retain the linked source record and record why the name changed. An audited prior name stays eligible for exact matching on the same registrable root with one canonical account. Apollo evidence comes from its saved organization response, with a stable provider ID and a domain that agrees with the original lookup and the CRM account. | Inspect the name and provenance in SQL and the account page. Replay an existing source ID after correcting a name. Link a fresh source ID that uses the audited prior name, and leave an unrelated same-root name for review. |
| R1-03.3 | Preview the name and source for every active account. An active Admin can apply a reviewed plan in one transaction. Skip manual names and accounts in review, avoid downgrading established provenance when stronger evidence becomes unavailable, and reject stale plans. If the domain label agrees with the existing name, preserve its spelling and capitalization. Set aside an existing multiword name that differs from a generated label for Admin review, such as Dell Technologies on `dell.com`. Repeating the backfill writes changes only when the name or provenance actually differs. | Use an isolated database and fixtures for Theiia and Tdsynnex. Check before and after state, a forced rollback, repeat execution, and preservation of sales work. Rehearse the script against a copy of the saved local data and report any records held for review. |
| R1-03.4 | Admin can edit a name on the account page. The resulting `name_source = manual` survives backfill and subsequent imports. Manager and SDR cannot write it. | Use distinct signed-in users and verify stale and unauthorized requests, the saved audit, an attempted automatic upgrade, and source replay. |

## Implementation boundary

The shared naming core resolves and verifies evidence, builds a preview and applies the same name policy in tests and through the database adapter. The existing Flask account page remains the entry point for inline edits. The backfill command handles an explicit target database. Its dry run reads the current state and its apply mode takes a reviewed preview fingerprint and active Admin credentials. The saved Apollo cache reader runs locally with a read-only source connection. It links validated evidence to an existing CRM identity before the backfill resolves it. No cache read or backfill calls Apollo.

## Rehearse on a disposable CRM copy

Use a database already initialized through R1-02. If you have an Apollo organization cache from the standalone scraper, preview the available evidence and apply it before preparing the name plan.

```powershell
python -B apollo_name_import.py --db instance/r1-03-copy.sqlite3 --cache ../cvent-scrapper/cvent_cache.db
python -B apollo_name_import.py --db instance/r1-03-copy.sqlite3 --cache ../cvent-scrapper/cvent_cache.db --apply --admin-email admin@example.test
```

Preview the backfill. Check the accounts marked for change or review and use the reported fingerprint to apply the reviewed plan. Both apply commands prompt for the Admin password without echoing it.

```powershell
python -B backfill_account_names.py --db instance/r1-03-copy.sqlite3 --dry-run
python -B backfill_account_names.py --db instance/r1-03-copy.sqlite3 --apply --expect 'paste-fingerprint-here' --admin-email admin@example.test
```

In the CRM, **Account name review** shows recorded sources and names needing review. The Admin can open an account and choose **Edit account name**. The SDR sees the resulting display name in My Accounts and on the account page.

## Saved-data rehearsal on 26 November 2026

The disposable local copy contained 769 accounts. The preview offered 17 updates. Linked Directory evidence improved one display name and the domain fallback added provenance for 16 accounts while preserving their displayed names. One account, Dell Technologies, kept its existing name for Admin review. A repeat preview offered zero changes. The account count, identity, sales work, source evidence and suppression data matched the original. SQLite integrity and foreign-key checks passed.

The saved Apollo cache had eight entries. None qualified to supply a name to an existing account in this copy. The real Theiia and Tdsynnex company export entries remain in identity review. Their expected corrections are verified with corroborated fixtures. The named case verification and the saved local copy are separate from the hosted database.

| Check | Result | Evidence |
| --- | --- | --- |
| R1-03.1 | PASS locally | Offline naming tests cover priority, saved Apollo ID and domain verification, Directory evidence, website metadata, internationalized domains, external feed review and platform rejection. |
| R1-03.2 | PASS locally | SQLite and PostgreSQL tests verify saved name, provenance, audit and exact matching of prior names. Existing source records replay and eligible fresh records link to the same account. An unrelated same-root name still goes to review. |
| R1-03.3 | PASS on the disposable copies | The 769-account rehearsal and both database engines passed. Backfill requires active Admin credentials and rejects stale plans. A forced write failure rolled back the names and audit together. The hosted dataset still needs its separate transfer and backfill. |
| R1-03.4 | PASS locally | The account-page edit is Admin-only. A manually edited name persists across backfill and source replay. |

The naming policy is in `crm_core/account_names.py`, with audited name matching in `crm_core/account_identity.py`. The Flask screen uses `account_names.py` and `templates/account_names_panel.html`. Stored evidence and prior names use `account_store.py` and `account_redirects.py`. The local commands are `apollo_name_import.py` and `backfill_account_names.py`.
