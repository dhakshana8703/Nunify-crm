# Nunify outbound CRM

`NUNIFY_CRM_CODEX_BUILD.md` is Dhakshana's active engineering brief. Build and verify the workflows in the existing Flask CRM. Start with manager assignment, account evidence, a logged call, and tomorrow's follow-up, then continue through the other workflows, ingestion, and operations. Work in small increments and trace applicable story IDs.

Keep `docs/architecture.md` and `docs/stories/` intact as Goutham's product references. Use them for sales needs, roles, and acceptance context. Follow the current brief for engineering decisions and order of work. Record substantive changes to the proposed workflow or architecture. Use real business dates, timezones, coverage windows, job schedules, and retry limits where applicable.

Treat Cvent and Directory of Associations as sources of one shared account model. Reuse their relevant inputs and components. Keep stable source IDs, original evidence, paid unlocks, ownership, and sales history. Keep ambiguous identities in review. Use deterministic extraction and editable business rules. Enforce role and account scope on the server, audit sensitive actions, and retain records through soft deletion. Use hyphens in UI copy.

Test the actual screens with distinct users and saved data. Use offline provider doubles. Preserve local work and keep private references, contact datasets, databases, and secrets outside Git. Rehearse migrations on a disposable database and a designated Neon branch when available. Report verified behavior, failures and recovery, and exact pending dependencies. Production deployment, paid calls, and customer communications require separate authorization.
