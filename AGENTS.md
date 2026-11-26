# Nunify outbound CRM

`NUNIFY_CRM_CODEX_BUILD.md` is Dhakshana's active engineering brief. Build and verify the workflows in the existing Flask CRM. Start with manager assignment, account evidence, a logged call, and tomorrow's follow-up, then continue through the other workflows, ingestion, and operations. Work in small increments and trace applicable story IDs.

Read `working.md` at the start of each CRM task. Update it with verified progress, decisions, test evidence, and specific pending dependencies. Check the current code and running environment before relying on recorded counts or status.

Work from a written specification before changing behavior. Trace story and acceptance IDs, record unresolved product decisions, and define observable pass and fail cases. Run new regression checks against the old behavior before the fix, then run them and the existing workflow checks again. Record the evidence before closing a criterion. Use `docs/engineering-foundation.md` for the architecture, `docs/r1-02-root-domain-merge.md` for the active R1-02 story, and `docs/r1-01-suppression.md` for its locally verified dependency.

Keep `docs/architecture.md` and `docs/stories/` intact as Goutham's product references. Use them for sales needs, roles, and acceptance context. Follow the current brief for engineering decisions and order of work. Record substantive changes to the proposed workflow or architecture. Use real business dates, timezones, coverage windows, job schedules, and retry limits where applicable.

Treat Cvent and Directory of Associations as sources of one shared account model. Reuse their relevant inputs and components. Keep stable source IDs, original evidence, paid unlocks, ownership, and sales history. Keep ambiguous identities in review. Use deterministic extraction and editable business rules. Enforce role and account scope on the server, audit sensitive actions, and retain records through soft deletion. Use hyphens in UI copy.

Test the actual screens with distinct users and saved data. Use offline provider doubles. Preserve local work and keep private references, contact datasets, databases, and secrets outside Git. Rehearse migrations on a disposable database and a designated Neon branch when available. Report verified behavior, failures and recovery, and exact pending dependencies. Production deployment, paid calls, and customer communications require separate authorization.
