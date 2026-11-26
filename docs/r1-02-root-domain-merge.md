# R1-02 Root-domain normalization and merge

Use [Goutham's R1-02 story](stories/01_R1a_prerequisites.md#r1-02-root-domain-normalization-and-merge-migration) as the acceptance checklist and the [engineering brief](../NUNIFY_CRM_CODEX_BUILD.md) for evidence and review policy. R1-01 restrictions apply throughout. Close each check only with recorded verification.

| Check | Expected behavior | How to verify |
| --- | --- | --- |
| R1-02.1 | A URL, bare host or email address resolves to the registrable domain, including `www`, nested subdomains and multi-label suffixes. Inputs without a usable organization domain produce no match. | Verify `https://www.national.alpfa.org/events` → `alpfa.org`, `events.example.co.uk` → `example.co.uk`, and `person@team.example.org.au` → `example.org.au`. Cover malformed input, an IP, an internationalized name, a public suffix by itself and separate tenants on a shared hosting domain. |
| R1-02.2 | The database enforces uniqueness for each populated `accounts.root_domain` after existing duplicate groups are resolved. Existing records with no usable domain remain available for review. | Observe a pending duplicate group before apply. After apply, inspect the field and try a duplicate write on SQLite and PostgreSQL. Verify backfill, restart and simultaneous imports. |
| R1-02.3 | Every confirmed merge keeps the lowest account ID within the database being merged. Source evidence, events, people, credits and allocations follow the survivor. The merge logs `from_id`, `to_id` and `merged_at`. An unresolved enrichment attempt on a duplicate blocks the merge while failed or queued for retry. A queued job with no prior attempt may follow the survivor when the plan has no other conflicts. | Upgrade a pre-change fixture containing all affected kinds of sales history. Compare IDs, amounts, source payloads, assignments and foreign keys. Verify repeat apply and rollback on a failed repoint. Retry an attempted enrichment job. Verify a fresh preview and apply still block while preserving the job, request key and credit history. Verify a never-attempted queued job can move. |
| R1-02.4 | A read-only dry run lists the survivor, duplicate records, affected data and any conflicts. | Compare database state before and after preview. Change the data after preview and verify apply rechecks its plan before writing. |
| R1-02.5 | `national.alpfa.org` and `alpfa.org` resolve to one ALPFA account. A bare `national.alpfa.org` suppression rule blocks the survivor and every linked contact before and after a merge or replay. `medc.cventevents.com` remains suppressed via R1-01 and cannot become an eligible organizer or enrichment target. | Run both cases with disposable fixtures. Test paid actions and outreach before and after apply, replay through both organization domains, and an unrelated organizer whose event uses a shared registration URL. |
| R1-02.6 | Each account writer uses the same normalized root and matches or creates through the shared account identity policy. Reimports retain the existing account ID and original source evidence. Ambiguous chapter or shared-domain records enter review. | Drive Cvent, Directory, CSV and API through their normal paths. Check the curated seed and saved-data transfer writers too. Replay each source and verify distinct external IDs, original hosts and payloads still link correctly. |
| R1-02.7 | Admins and Managers can preview and explicitly confirm a merge in the UI. The lower account ID survives regardless of selection order. The server checks the confirming actor's current role and binds confirmation to that actor's preview. SDRs and AEs cannot access the merge UI or write endpoints. Old account links follow the survivor's permissions. Bulk migration through the CLI requires Admin credentials. | Use separate Admin, Manager, SDR and AE sessions. Verify preview, confirmation, duplicate submission, stale input, unauthorized writes, the saved merge audit and Manager denial at the bulk CLI. |

## Engineering decisions to verify

- Use the locally bundled Public Suffix List snapshot dated 25 November 2026 and pinned `idna` 3.20 for deterministic normalization. Preserve each observed hostname in source evidence and retain the account's existing display domain. The root domain is for identity matching. R1-01 still evaluates the original company and contact domains. [Public Suffix List](https://publicsuffix.org/list/)
- The lowest account ID within the database being merged always survives. This applies to web and bulk merges. Account creation times play no part in the decision. IDs across databases use transfer mappings. Preserve merged-away IDs through merge history and resolve them to the surviving account. An import of an old source ID or former domain must still resolve to the survivor.
- Preserve IDs for source evidence, contacts, activity, tasks, credits and provider jobs. Keep raw evidence and historical snapshots. Repoint live references together under one writer transaction. Audit the actor and the account IDs. Keep the current assignment on the survivor and retain the prior allocation and qualification snapshots in the audit and retired records. The schema change and an explicit merge apply are separate steps.
- A shared root alone may describe a chapter or a different organization. Automatic apply needs corroborated identity. Preview identifies competing owners, duplicate contact identities, conflicting manual qualifications, incompatible coverage and provider work that cannot safely change account IDs. Pending handoffs and previously attempted failed or queued enrichment requests on a duplicate must be resolved before apply. Ownership disputes need an explicit review decision.
- The merge UI follows the Admin and Manager permissions in [Goutham's architecture](architecture.md). R1-01 suppression administration and bulk CLI apply retain their Admin permissions.
- Keep R1-01 account and domain holds effective across a merge and every subsequent import. Eligibility checks, assignment, contact access and paid operations must resolve the same active account. Fuzzy matching across different domains remains out of scope.

## Verified before implementation

- The local preview has 752 accounts and 752 distinct stored domains. The inspected `accounts` schema has no `root_domain` field and the database has no `account_merges` table. Distinct stored hosts alone do not establish unique root domains.
- The current source matcher can reuse an exact host and sends ambiguous parent and chapter cases to review. An acceptance probe with three sibling hosts for one organization created three accounts across Cvent, Directory and external intake. The expected count is one.
- The preview has `alpfa.org` and no separate `national.alpfa.org` account. The two-record merge needs an explicit fixture. The active `cventevents.com` rule already matches `medc.cventevents.com`, and no separate CRM account at that host exists in the preview.
- There is no account merge script, corresponding dry-run command or Admin merge screen in the current checkout. Source Review links evidence to an account.

## Run the merge rehearsal

Use a disposable database with its existing CRM schema and users. In the web app, sign in as an Admin or a Manager, open **Merge accounts**, select two accounts, and click **Preview merge**. The preview identifies the lower-ID survivor, the accounts to retire and the record IDs affected. Review any conflicts. Use **Confirm merge** for a conflict-free preview.

For the one-time migration from the CRM folder, run the dry run on the disposable database.

```powershell
python -B merge_accounts.py --db instance\r102-rehearsal.sqlite3 --dry-run
```

Copy the fingerprint from that dry run. Apply requires an active CRM Admin sign-in and rechecks the reviewed plan inside the writer transaction.

```powershell
python -B merge_accounts.py --db instance\r102-rehearsal.sqlite3 --apply --expect REVIEWED_FINGERPRINT --admin-email admin@example.test
```

Repeat the dry run after applying. A completed set of merges returns an empty group list. Confirming a stale UI preview returns a conflict response before writes, and rerunning an already completed CLI apply reports zero groups.

## Implementation files

| Area | Files |
| --- | --- |
| Domain normalization and schema | `organization_identity.py`, `public_suffix_list.dat`, `requirements.txt`, `account_identity_schema.py`, `account_redirects.py`, `crm_store.py` |
| Merge decisions, SQL and CLI | `crm_core/account_merge.py`, `account_merge_store.py`, `merge_accounts.py` |
| Staff merge UI and scope | `account_merges_web.py`, `templates/account_merges.html`, `templates/base.html`, `app.py`, `workflow_web.py`, `account_store.py`, `assignment_store.py`, `growth_store.py` |
| Source admissions and replays | `crm_core/account_identity.py`, `crm_core/ingest.py`, `ingest_store.py`, `cvent_import.py`, `directory_import.py`, `crm_core/transfer.py`, `transfer_store.py`, `source_review_store.py` |
| Suppression and saved contact imports | `crm_core/suppression.py`, `suppression_projection.py`, `workflow_store.py`, `crm_core/legacy_contacts.py`, `legacy_contact_store.py`, `transfer_source_contacts.py` |
| Acceptance and compatibility tests | `tests/test_r102_*.py`, `tests/test_postgres_pilot.py`, `tests/test_core_boundary.py`, `tests/test_bulk_qualification.py`, `tests/test_r101_projection.py`, `tests/test_legacy_contact_core.py`, `tests/test_saved_source_transfer.py` |

## Verification status

| Check | Local result | Evidence |
| --- | --- | --- |
| R1-02.1 | PASS | URL, email, suffix, Unicode and private hosting cases resolve using the shared normalizer. |
| R1-02.2 | PASS | SQLite and PostgreSQL backfill legacy roots, reject duplicate writes after repair and converge on one account under concurrent intake. |
| R1-02.3 | PASS | The lowest ID survives, history stays linked, and forced failure rolls back. Requeued requests with a prior attempt block retirement, while a never-attempted queued job can follow the survivor. |
| R1-02.4 | PASS | Dry run reports affected IDs without writes. Changed history blocks confirmation, and repeat CLI apply reports zero remaining groups. |
| R1-02.5 | PASS | The ALPFA hold survives a merge and later imports. A contact on an outside email domain still cannot be unlocked or contacted from the CRM. The Cvent competitor and unrelated registration cases pass. |
| R1-02.6 | PASS | The Cvent, Directory and external sibling-domain regression produces one account and retains each source record. CSV, replay and transfer paths also verify shared identity. |
| R1-02.7 | PASS | Separate web sessions verify Admin and Manager confirmation, SDR and AE denials, preview ownership and role recheck. Bulk CLI requires Admin. Retired links follow survivor permissions. |

The disposable saved-data rehearsal preserved all 752 original account IDs. On 26 November 2026, Dhakshana reported all 50 PostgreSQL tests passing in 1,853.851 seconds on the designated `r1-02-rehearsal` Neon branch. The existing CRM account count was one both before and after the temporary-schema tests. The local 769-account saved-data rehearsal and the hosted fixture run used separate data. See [working notes](../working.md) for the saved-data run and test evidence.
