# CRM workflow design

The existing Flask app and SQLite source snapshot are the starting point. Account IDs and source-record IDs survive reimport. The importer owns source evidence. The CRM owns assignments, contacts, activities, tasks, stages, coverage, and handoffs. Add local workflow tables keyed to account IDs alongside source records. Store dates for follow-up as local calendar dates and timestamps for activity, coverage, and delivery in UTC.

Users have stable IDs, password hashes, roles, and IANA timezones. Staff can see the accounts their role permits. An SDR works owned accounts and accounts with current delegated coverage. Contact access inherits the account scope. AEs see their own handoffs. Look up the current role, account scope, and restrictions on the server, including on evidence endpoints. HTML form submissions carry CSRF tokens. Manager changes carry the account revision to detect stale forms.

Assignment and task transfer commit together. Activity, contact status, account stage, and any new task commit together. Each submission has a stable request key so an identical retry returns the previous result. A conflicting request cannot overwrite an intervening assignment. Coverage is a separate time-bounded grant. Tasks that outlast it belong to the permanent owner. Preserve the actor, previous assignments, and customer context for audit.

Handoffs save the intent and delivery state before a provider call. The worker claims work with a recoverable lease, uses a stable idempotency key, and records failures for an authorized retry. Source runs stage raw records, validate and resolve them, and record outcomes. A run that stops can be retried against the same inputs. Imports and provider jobs execute outside the web request. Local development uses offline delivery and provider adapters. Live configuration and Neon verification are separate rollout checks.

This build uses end-to-end workflows to sequence work from the existing two-source prototype. It brings assignment and handoff visibility into the first usable sales build and uses SQLite for local verification. The original story pack remains the sales reference.
