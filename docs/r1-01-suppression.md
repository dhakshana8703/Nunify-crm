# R1-01 Suppression list

Use [Goutham's R1-01 story](stories/01_R1a_prerequisites.md#r1-01-suppression-list-migration) as the product reference. Preserve the CRM's core and adapter boundaries. HubSpot synchronization belongs to R3.

| Check | Given, action and expected outcome | Verification |
| --- | --- | --- |
| R1-01.1 | Fresh and upgraded databases store rule `id`, `kind`, `value`, an approved `reason` code, `source`, `created_by` and `created_at`. Both databases reject unsupported codes. Preserve prior reason text in an optional `reason_note`, with IDs, active or removed status, and audit references intact. | Test a fresh schema, a pre-change schema, invalid writes, and two starts on SQLite and disposable PostgreSQL. |
| R1-01.2 | A new CRM stores exactly the four Cvent domain seeds as `competitor` with source `seed`. Reinitialization keeps the same IDs and respects an Admin's removal. | Read the persisted seeds, remove one, then restart. |
| R1-01.3 | A domain rule matches the full domain and nested subdomains, case-insensitively. A bare subdomain rule also blocks the company identified by its registrable domain. Goutham confirmed that `national.alpfa.org` blocks the entire ALPFA account at `alpfa.org`. Match the company's organization domain and preserved merge identities. A shared registration URL by itself never establishes company identity. | Add only `national.alpfa.org` and verify the persisted flag on ALPFA at `alpfa.org`, without requiring an existing import from the national subdomain. Verify an unrelated organizer using the same registration host remains eligible. Cover look-alikes, shared platforms and separate hosting tenants. |
| R1-01.4 | The CRM refuses enrichment, email unlock and outreach when an account or contact is suppressed. A company-wide rule restricts every linked contact, including contacts whose email is on another domain or unknown. The refusal explains the restriction. A queued request is checked again before calling the provider. A rule added during lookup and an already-blocked returned email each prevent the response from becoming a usable contact. Confirmed charges remain recorded once. | Exercise the HTTP path and use offline provider doubles to verify calls, stored contacts, credits, and job status. Verify that an individual email rule leaves the rest of its company and contacts eligible. |
| R1-01.5 | Existing matching accounts backfill `accounts.suppressed = true` and their winning `suppress_reason` code. Other accounts store false and null. Admin add, CSV add, remove and restore and new account paths keep the projection consistent in the same transaction. On overlap, removing the winning rule selects the next applicable rule. Email and person-only restrictions stay scoped to the contact. Main account lists hide matches by default and authorized users can show them with reasons. | Inspect persisted flags while exercising Admin and SDR or Manager forms. Verify normal imports and transfers preserve existing work and use the destination rules. |
| R1-01.6 | Admin can list, add, bulk add and remove rules. The add form and CSV accept the same six reason codes. A bad row or invalid headers leave the batch unchanged. Removal deactivates the rule, records its details and actor, and allows later reactivation. | Drive the existing Admin page and CSV form. Check role denial, rule state and actor and rule details in audit. |

## Implementation decisions

- The reason codes are `competitor`, `customer`, `open_deal`, `opt_out`, `bounced`, and `manual`. The UI uses human-readable labels and can display an optional note. Legacy labels map to their recognized codes, and unclassified legacy text maps to `manual` with the original explanation retained.
- The CRM sets the rule source from the recorded action. Manual entry, CSV, CRM opt-outs, sequencer events, and starter rules have their own source labels. Existing data with unknown provenance uses `legacy`. Neither UI input nor the CSV can override origin or creator.
- Account flags represent matching domain and account rules. Contact decisions use the current account and contact rules, plus direct contact restrictions. Order matches by stable rule ID, preserving current precedence. Removing the last matching rule clears both account fields. Use the live rules for outreach and paid-action authorization.
- Use the R1-02 registrable-domain normalizer for the company-wide match of a bare domain rule. Apply it to the company's own domain and retired account domains. Known shared platforms retain exact and descendant matching. Registration and event URL fields never participate in organization matching. Domain checks on a contact's own address still match the host and its descendants. Individual email rules match only that address.
- Preserve existing rule and account IDs and use the same writer transaction for rule state, stored flags and audit events. Keep CSV all-or-nothing and account flags queryable on both supported databases.

### Internationalized domain rules

For R1-01.3 and R1-01.6, accept a valid bare hostname in Admin and CSV in either its displayed or encoded DNS spelling. Convert both spellings to the same stored value using the shared hostname validator. Keep the exact hostname, including `www` when supplied, and preserve the optional `*.` prefix. A rule covers that host and its descendants. An explicit wildcard keeps that scope. A bare hostname also follows the company matching rule above. Reject URL paths, ports and malformed labels, with the same CSV all-or-nothing behavior.

Verify against an imported account, a saved or returned contact address, and the stored account flag. On upgrade, refresh saved flags for existing rules using the same matcher and retain the original rule IDs and audit history.

When importing saved contacts, evaluate domain rules against the original hostname or its DNS encoding before comparing contact identities. For example, `straße.de` encodes to `xn--strae-oqa.de`, while `strasse.de` is a separate hostname. Verify a held saved contact stays held during preview and apply, including when its company domain is different.

## Evidence

- R1-01.1 and R1-01.2 exercise fresh and upgraded schemas, repeated startup, and seed removal in [the rule metadata tests](../tests/test_r101_metadata.py).
- R1-01.3 exercises the whole-domain boundary and preserves registration evidence in [the domain tests](../tests/test_r101_domain.py).
- R1-01.4 and R1-01.6 drive the Admin form and provider behavior with offline doubles in [the suppression workflow tests](../tests/test_suppression_foundation.py) and [the growth tests](../tests/test_growth_ops.py).
- R1-01.5 checks the two persisted fields after imports and rule changes, then renders the scoped Manager and SDR lists in [the account projection tests](../tests/test_r101_projection.py).
- The internationalized-domain checks cover Admin and CSV submissions, imported account visibility, provider blocking, exact wildcard scope and a repeat startup with an existing rule. [Domain-entry regressions](../tests/test_suppression_foundation.py)
- The company-domain checks cover the ALPFA flag with and without national-subdomain evidence, all linked email hosts, scoped email opt-outs, an unrelated registration host, rule-preserving upgrades, later imports and a contact moved by an actual merge. [Company-domain regressions](../tests/test_r101_domain.py)
- [The PostgreSQL rehearsal](../tests/test_postgres_pilot.py) runs the portable acceptance cases against disposable schemas. `working.md` records the failing baseline, successful reruns, and the saved-data rehearsal.

Hosted migration, live provider proof, and rollout remain separate checks.

## Remaining verification from the two-source review

- New campaign enrollment checks suppression. Adding a suppression rule after enrollment leaves the local membership status Active. The live sequencer pre-send check remains unverified.
