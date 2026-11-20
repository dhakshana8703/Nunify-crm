# Release 2a - SDR Workspace Core

## Context for the agent

Release 1a added suppression, root-domain dedupe, clean names, a credit ledger, and owner / stage / next event fields on accounts. This release builds the SDR's daily workspace so reps can work entirely inside the tool, and enforces role permissions. Roles and screens are defined in `docs/architecture.md` sections 5 and 6. Reps will work the accounts already enriched from the Cvent source; type classification, persona config by type and the full Accounts list arrive in Release 1b, so screens must handle `type = unknown` gracefully. Do not build HubSpot push (Release 3) or sequencer integration (Release 5).

**Release goal:** SDRs go live. A rep can see what to do today, research the account, contact people and log every touch without a spreadsheet.

**Estimate:** 1 week. Build in the order listed.

---

## R2-01 Roles and server-side permissions [migration]

**As an** admin, **I want** roles enforced on every API route, **so that** SDRs only see and change what they should.

**Acceptance criteria**

- [ ] `users.role` supports: admin, manager, sdr, ae
- [ ] Middleware checks role and ownership on every route; UI hiding alone is not acceptable
- [ ] Permission matrix from architecture doc section 5.2 implemented; key rules:
  - SDR reads / writes only accounts where `owner_id = self` and their people
  - SDR can view the unassigned pool read-only (name, type, platform, next event, score - no contact details)
  - SDR cannot export, delete, edit config, run sources or reassign
  - Manager has admin rights except sources, config, suppression and user management
- [ ] Automated tests: an SDR token calling another SDR's account, export, delete and config endpoints gets 403
- [ ] Admin can change a user's role from the Team screen

**Touches:** users, auth middleware, all routes, tests.
**Depends on:** Release 1a.

---

## R2-02 Person outreach status [migration]

**As an** SDR, **I want** each person to have a sales status separate from lock / unlock, **so that** I know where every contact stands.

**Acceptance criteria**

- [ ] `people.outreach_status`: not_contacted / in_sequence / replied / connected / positive / not_interested / wrong_person / bounced / opted_out / handed_off (default not_contacted)
- [ ] `people.do_not_contact` boolean; set automatically for opted_out and bounced; such people cannot be added to campaigns or unlocked again
- [ ] Opted-out emails are added to the suppression list (R1-01)
- [ ] Lock / unlock remains a separate field (`unlocked`)
- [ ] Status shown as a badge in People list and contact page; filterable

**Touches:** people, suppression, People list.
**Depends on:** R2-01.

---

## R2-03 Activity log [migration]

**As an** SDR, **I want** to log calls, emails, LinkedIn touches and notes against a person, **so that** the full history is in one place.

**Acceptance criteria**

- [ ] Table `activities` (id, person_id, account_id, user_id, type: email / call / linkedin / meeting / note, direction: outbound / inbound, outcome, body, occurred_at, source: manual / webhook)
- [ ] Outcomes per type:
  - call: no answer / voicemail / connected / wrong number / not interested / meeting booked
  - email: sent / replied / bounced
  - linkedin: connection sent / accepted / message sent / replied
- [ ] **Log activity modal** opens from contact page, account page and Today queue; required fields: type, outcome; optional: note, next action date
- [ ] Completing the modal takes under 10 seconds for a no-answer call (keyboard-friendly, outcome buttons not dropdowns)
- [ ] Logging certain outcomes updates person status automatically (replied -> replied; connected -> connected; bounced -> bounced; not interested -> not_interested; meeting booked -> prompts Mark Positive)
- [ ] Setting a next action date creates a task (R2-04)
- [ ] Account `stage` moves to Working on first logged outbound activity, Engaged on first reply / connected call

**Touches:** activities, people, accounts, modal component.
**Depends on:** R2-02.

---

## R2-04 Tasks and next actions [migration]

**As an** SDR, **I want** follow-up tasks with due dates, **so that** nothing slips.

**Acceptance criteria**

- [ ] Table `tasks` (id, user_id, account_id, person_id, kind: call / email / linkedin / research / other, due_at, status: open / done / snoozed, note, created_at, completed_at)
- [ ] Created from Log activity (next action date) or manually from account / contact page
- [ ] Complete, snooze (1 day / 3 days / 1 week / custom), reassign (manager only)
- [ ] Overdue tasks highlighted
- [ ] When an account is reassigned, its open tasks move to the new owner

**Touches:** tasks, account and contact pages.
**Depends on:** R2-03.

---

## R2-07 Account page

**As an** SDR, **I want** everything about an account on one page, **so that** I understand why it is a target and who to contact.

**Acceptance criteria**

- [ ] Header: name, domain link, type, platform (+ detail), region, members, next event, fit score, stage, owner, sources
- [ ] **Why this account** panel: for now, built from Cvent events (event name, date, Cvent page link) and "Uses Cvent - confirmed". Designed so richer signals from `account_signals` (Release 4) can be added later
- [ ] Events list (upcoming first); role badges added later with R1-10
- [ ] Contacts grouped by persona tier (P1, P2, other) with status badge, email status, LinkedIn link, last touch
- [ ] Account activity timeline (all people, newest first)
- [ ] Actions: enrich (existing persona logic until R1-12; quota enforced from R2-09), unlock email, add contact manually, log activity, create task, change stage (Nurture / Disqualified with reason), Mark Positive
- [ ] Stage change to Disqualified requires a reason (wrong fit / no events / already customer / competitor / other)

**Touches:** account detail screen.
**Depends on:** R2-03, R2-04.

---

## R2-08 Contact page and manual contacts

**As an** SDR, **I want** a contact page and the ability to add people Apollo missed, **so that** I can work the right person.

**Acceptance criteria**

- [ ] Contact page: name, title, persona, email + status, phone, LinkedIn, account link, outreach status, activity timeline, open tasks
- [ ] Actions: log activity, change status, mark wrong person, mark do not contact, edit details, Mark Positive
- [ ] Add contact manually from account page: name, title, email, LinkedIn, phone, persona; `origin = manual`
- [ ] Duplicate check on email and LinkedIn URL within the account

**Touches:** contact screen, people.
**Depends on:** R2-02, R2-03.

---

## R2-05 Today queue (SDR home)

**As an** SDR, **I want** a home screen that tells me what to do today, **so that** I start working immediately.

**Acceptance criteria**

- [ ] Default landing page for SDR role
- [ ] Sections in order:
  1. Replies and inbound needing response (person status = replied, no outbound activity since)
  2. Tasks due today and overdue
  3. Accounts entering the timing window (next event 4-9 months out, no activity in last 30 days)
  4. Newly assigned accounts with no activity
- [ ] Each row: account, person (if any), reason, due date, and quick actions (log activity, open, snooze, done)
- [ ] Counters at the top: touches today, replies this week, positives this month
- [ ] Empty state message when nothing is due

**Touches:** Today screen.
**Depends on:** R2-03, R2-04.

---

## R2-06 My Accounts

**As an** SDR, **I want** a list of my accounts, **so that** I can plan account-based outreach.

**Acceptance criteria**

- [ ] Columns: Name, Domain, Type, Platform, Next event, Events, Stage, Leads unlocked / total, Last touch date, Open tasks; scoped to `owner_id = self`
- [ ] Filters: Stage, Next event window (0-3 / 4-9 / 10-18 months / none), Type, search by name / domain
- [ ] Default sort: next event ascending (upcoming first), accounts with no activity on top within ties
- [ ] "Request more accounts" button sends a notification to managers / admin with an optional note
- [ ] No export, no bulk delete

**Touches:** My Accounts screen, notifications.
**Depends on:** R2-01.

---

## Release 2a demo checklist (SDR go-live gate)

- [ ] Log in as SDR: lands on Today; sees only own accounts; cannot export, delete or open config
- [ ] Attempt to open another rep's account by URL / API returns 403
- [ ] Log a no-answer call with next action tomorrow in under 10 seconds; task appears in tomorrow's queue
- [ ] Log a reply; person status and account stage update automatically
- [ ] Account page shows events, contacts by persona tier and full timeline
- [ ] Add a manual contact; duplicate email is rejected
- [ ] Suppressed accounts and do-not-contact people cannot be worked

**After this passes:** onboard SDRs onto their existing allocations. Until R1-12 ships, reps work accounts already enriched and do not enrich new association accounts.
