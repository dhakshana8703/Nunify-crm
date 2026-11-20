# Release 2b - SDR Workspace Completion

## Context for the agent

SDRs are live on the core workspace (Release 2a). This release adds credit quotas, the positive-handoff flow, manager assignment tools and the audit log. Until the HubSpot integration (Release 3) is built, a positive handoff is stored locally and emailed to the AE and manager.

**Release goal:** credit spend controlled per rep, positives reach AEs the same day, managers can assign and rebalance accounts, sensitive actions are traceable.

**Estimate:** 1 week.

---

## R2-09 Daily unlock quota

**As an** admin, **I want** a per-SDR daily credit quota, **so that** Apollo spend is controlled.

**Acceptance criteria**

- [ ] `users.daily_unlock_quota` (default 25), editable by admin in Team screen
- [ ] Enrich / unlock blocked when the SDR's credits today (from credit ledger) reach quota; message shows remaining and reset time (midnight user's timezone)
- [ ] Remaining quota visible in SDR header
- [ ] Admin and manager not limited

**Touches:** users, enrichment service, header, Team screen.
**Depends on:** R2-01.

---

## R2-10 Mark Positive (local)

**As an** SDR, **I want** to flag a positive conversation, **so that** it is handed to an AE.

**Acceptance criteria**

- [ ] Modal fields (required): contact, positive type (meeting booked / demo requested / pricing requested), meeting date-time if booked, context note (min 30 characters), suggested AE (dropdown of AE users)
- [ ] Saves a `handoffs` row (id, account_id, person_id, user_id, ae_id, type, meeting_at, note, created_at, hubspot_status: pending)
- [ ] Person status -> positive; account stage -> Positive
- [ ] Until Release 3: email sent immediately to the selected AE and all managers with account, contact (name, title, email, phone, LinkedIn), positive type, meeting time, SDR note and last 10 activities
- [ ] Manager / admin also notified in-app
- [ ] Handoffs list for admin / manager with status column (HubSpot push comes in R3)

**Touches:** handoffs, people, accounts, modal, notifications.
**Depends on:** R2-02, R2-03.

---

## R2-11 Assignment screen

**As a** manager, **I want** to assign accounts from a pool and see rep capacity, **so that** work is spread properly.

**Acceptance criteria**

- [ ] Left: unassigned qualified accounts (filters as Accounts list)
- [ ] Right: reps with assigned accounts count, open tasks, touches last 7 days
- [ ] Assign selected accounts to a rep; reassign from one rep to another
- [ ] Rule-based assign: by region and / or type, round robin across selected reps, capped at N accounts per rep
- [ ] Every assignment change written to `assignments` history (account_id, user_id, assigned_by, assigned_at, ended_at)
- [ ] Reassignment moves people ownership and open tasks with the account

**Touches:** assignments, accounts, Team / Assignment screen.
**Depends on:** R2-01, R2-04.

---

## R2-12 Audit log [migration]

**As an** admin, **I want** sensitive actions logged, **so that** data leakage and mistakes are traceable.

**Acceptance criteria**

- [ ] Table `audit_log` (id, user_id, action, entity, entity_id, before JSON, after JSON, at)
- [ ] Logged: exports, deletes, merges, reassignments, role changes, suppression changes, config changes, Mark Positive
- [ ] Admin view with filters by user, action and date

**Touches:** audit_log, admin screen.
**Depends on:** R2-01.

---

## Release 2b demo checklist

- [ ] Hit the daily unlock quota; further unlocks are blocked with a clear message and remaining count resets at midnight
- [ ] Mark Positive creates a handoff and the AE receives the email within a minute
- [ ] Manager assigns 20 unassigned accounts by region round robin; reps see them in My Accounts
- [ ] Manager reassigns an account; people and open tasks move with it; audit log shows the change
- [ ] Export and delete actions appear in the audit log
