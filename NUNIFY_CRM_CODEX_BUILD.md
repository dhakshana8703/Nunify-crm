# Build Nunify’s outbound CRM

## Mandate from Dhakshana

Work in my existing `nunify-crm` repository. I own the technical direction. I am asking you to build the complete outbound CRM and exercise it through the screens my sales team will use. Make routine engineering decisions yourself. Goutham is the sales manager. Use his documents to understand what the team needs to accomplish. You can change the architecture, implementation order, screens, or workflow when you can explain how the change serves that goal. Keep progressing through implementation, testing, and fixes after writing your plan.

The product succeeds when an SDR can open Today, understand why an organization is worth contacting, contact the right person, record what happened, and reliably pick up the next action. A manager can distribute work, cover absences, and find missed follow-ups. An account executive (AE) can take over a qualified opportunity with the conversation intact. The CRM owns prospecting and qualification. HubSpot is the intended destination for qualified deals.

## Remove inherited timers and release gates

Update the active project instructions, including `AGENTS.md` and active workspace rules if needed. Remove inherited day/week estimates, countdowns, release-order gates, waits for a demo meeting, and numeric demo timing thresholds such as logging a call in ten seconds or delivering a handoff email within a minute. Remove any artificial delay or pass/fail check introduced solely to enforce those targets. Keep Goutham’s supplied originals intact as reference material. When an older handoff directs you to complete a Cvent-only story or stop after the AARP demo, this brief gives my current direction.

Real business time still matters. A follow-up due tomorrow must appear tomorrow in the rep’s timezone. Leave coverage needs a start and end. A recurring import needs a configurable schedule. Network calls need timeouts and safe retries. Identify what a timer actually does before changing it. Preserve existing working behavior and operational safeguards. Make business timing policies configurable where the team needs control.

## Start from the actual workspace

1. Locate `nunify-crm`, `cvent-scrapper`, the Association project, and the extracted private handoff in the VS Code workspace. Check the target branch, current changes, existing architecture, tests, and normal start command. Preserve my local work. Develop in the CRM repository. Treat the source projects as inputs and keep private reference documents and real contact datasets outside Git.
2. Use the local code and running application as evidence. The last local report said four tests passed and a repeated import produced one stable AARP account with three Cvent events and one national directory listing. Rerun the relevant tests and inspect the page. Build on what exists. Verify any claimed feature before marking it complete.
3. Trace one account from source record through assignment to follow-up. Write a short low-level design in the repo covering data ownership, identifiers and constraints, permissions, transactional writes, background jobs, and failure recovery. Record important departures from Goutham’s design in a few sentences. Then implement the first incomplete workflow immediately.

## Product behavior to deliver

| When this happens | What users must experience |
|---|---|
| Cvent and the directory identify the same organization | One account shows both sources, their original values, and dated evidence. Source-only accounts also work. Preserve source record IDs. A chapter can share its parent’s domain, so ambiguous matches stay available for review. Reimporting or correcting a source row preserves ownership, paid unlocks, notes, and history. |
| A manager assigns or permanently transfers accounts | A rep sees their assigned work in My Accounts and Today. A permanent transfer carries open tasks and contact ownership with it, preserves history, and records who made the change. Server-side permissions enforce the rep, manager, admin, and AE scopes. |
| A rep starts their day | Today explains why each item appears and what action is due. Prioritize unanswered inbound replies and overdue or due tasks. Show newly assigned work and relevant event-planning opportunities. The account page brings together organization details, upcoming events, source evidence, contacts, owner, outreach restrictions, and timeline. |
| A rep calls, receives a reply, gets no answer, or is ghosted | Let the rep record the channel, outcome, note, and next action in one interaction. Distinguish **account stage**, **this contact’s status**, and **the next task with an owner and due date**. A no-answer call can create tomorrow’s task. An actual reply updates the conversation. Silence leaves room for follow-up or a deliberate nurture decision. Opt-outs and suppression prevent further prohibited outreach and paid unlocks. |
| A rep is on leave and a prospect responds | Provide manager-authorized, time-limited coverage. Keep the account owner visible. Give the covering rep access to the work needed for coverage, surface incoming replies received by the CRM, and record who responded. End temporary access when coverage expires. Any commitment due after coverage ends must still have an owner who can fulfill it. Manual inbound logging must work until an inbox or sequencer is connected. |
| A prospect requests a demo or pricing, or books a qualifying meeting | Record the positive outcome, contact, context, agreed next step, and receiving AE. Save the handoff durably before attempting delivery. Show pending, delivered, or failed status. Support retry without creating a duplicate handoff or deal. An AE can see what the customer was promised. |
| More source records or contacts arrive | Scheduled sources, external ingest, and CSV/Excel uploads use shared staging, validation, suppression, matching, and promotion logic. Keep scheduled collection out of the web request. Handle overlapping runs and partial failures. Qualify before paid enrichment, choose contacts appropriate to organization type, and account for spend in one credit ledger. Use deterministic rules and editable configuration. |
| Managers and admins run the team | Support assignment and workload review, overdue work, positives and handoffs, source health, and credit usage. Support saved sales views, campaign membership, sequencer activity and reporting as appropriate to the agreed sales model. Scope exports and sensitive operations on the server and retain an audit trail. |

## How to make it dependable

Prefer the simplest design that fits the inspected repository. Reuse working discovery, parsing, authentication, and enrichment behavior after checking it against the CRM’s ownership and permissions. A single web application with a relational database and a separate worker is a reasonable starting point. Keep source facts separate from rep-entered facts so a later import has no reason to overwrite sales work.

Make actions safe to repeat. For example, saving a call and its follow-up should commit both or leave the rep able to retry without duplicating either. Simulate two people acting on the same account, a double-click on Save, reassignment while a form is open, and an external service failing after accepting a request. Use database constraints, transactions, and durable job or delivery state where the failure actually occurs. Surface failures to the person who can resolve them. Verify recovery as well as the successful path.

Build in usable increments. Start with a **manager assigning an account → rep seeing why it matters → rep logging a no-answer call → follow-up appearing tomorrow**. Then continue across replies, handoff, reassignment, coverage, wider ingestion, and operations. Use focused commits and retain the original story IDs where useful for tracing requirements. A completed increment is a checkpoint for the next piece of work.

## Proof and delivery

- Run the app and drive the important paths through its normal UI with separate user accounts. Also verify authorization directly on the server. Cover cases where a user has permission and where access should be denied. Include a source-only account and an ambiguous parent/chapter case alongside AARP.
- Test reimports, rapid repeated actions, concurrent assignment, suppression, quota handling, inbound reply attribution, temporary coverage expiry, and delivery retries. Use saved data and test doubles for Apollo, email, the sequencer, and HubSpot. Preserve historical paid unlocks and reconcile legacy credit data before migration.
- Exercise migrations on a disposable database and a designated Neon branch when available. Verify data counts, links between records, and a recovery path. Check that a failed worker run is visible and recoverable while salespeople can keep using the app.
- Goutham left the final HubSpot pipeline/AE routing, sequencer vendor, and upload-role policy open. Implement and test the surrounding flows using configurable choices or adapters. Keep unconfigured live connections visibly pending. Work on the rest while a genuine business decision or credential is outstanding.
- Leave a runnable development build, exact start and test commands, a short manager/SDR walkthrough, and an honest delivery report. Identify what passed locally, what passed against staging, what needs a real integration or policy decision, and what still blocks production rollout. Keep production deployment, paid calls, and real customer communications as separate authorized actions.

Begin in the existing checkout now. Carry the work as far as the available environment permits. If an external dependency blocks part of the product, finish the independent work and record the precise remaining dependency so the next session can resume.
