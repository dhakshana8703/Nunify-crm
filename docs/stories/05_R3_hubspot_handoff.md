# Release 3 - HubSpot Handoff (Phase 3)

## Context for the agent

Release 2b created local handoffs (emailed to AEs as an interim step) when an SDR marks a contact Positive. This release pushes those handoffs into HubSpot as company + contact + deal owned by an AE, and pulls HubSpot data back for suppression and attribution. This tool owns everything before a positive response; HubSpot owns the deal. See `docs/architecture.md` section 7.

**Release goal:** a positive creates a correctly owned HubSpot deal in under a minute, existing customers and open deals are never prospected, and deal outcomes are attributed back to source and type.

**Decisions needed before starting:** HubSpot pipeline and stage for new handoffs; AE list with HubSpot owner IDs and regions; private app token with CRM read / write scopes.

---

## R3-01 HubSpot connection and settings

**As an** admin, **I want** to connect HubSpot and configure the handoff, **so that** pushes land in the right pipeline.

**Acceptance criteria**

- [ ] Admin settings: HubSpot private app token (stored encrypted), pipeline, initial deal stage, deal name template (default "{account} - Outbound - {type}")
- [ ] "Test connection" button verifies the token and lists pipelines / stages
- [ ] Map each AE user to a HubSpot owner ID and a region
- [ ] Custom HubSpot properties created if missing (on company and deal): `outbound_source`, `outbound_play`, `outbound_account_type`, `outbound_platform`, `outbound_sdr`, `outbound_tool_id`

**Touches:** settings, users (hubspot_owner_id, region).
**Depends on:** Release 2b.

---

## R3-02 Push handoff to HubSpot

**As an** SDR, **I want** Mark Positive to create the deal in HubSpot automatically, **so that** the AE can act immediately.

**Acceptance criteria**

- [ ] On handoff create (and via a "Retry push" button):
  1. Company: search by domain; create if absent; set custom properties
  2. Contact: search by email; create if absent; associate to company
  3. Deal: create in configured pipeline / stage, owner = AE, associated to company and contact
  4. Note on deal: positive type, meeting time, SDR context note, account "Why this account" signals, last 10 activities
  5. If a meeting is booked, create a HubSpot meeting engagement at that time
- [ ] Stores `hubspot_company_id`, `hubspot_contact_id`, `hubspot_deal_id` on account / person / handoff
- [ ] `handoffs.hubspot_status`: pending / pushed / failed (with error message); failures visible to admin with retry
- [ ] Idempotent: retrying never creates duplicate companies, contacts or deals
- [ ] Person status -> handed_off; account stage -> Handed off
- [ ] If the company already has an open deal in HubSpot, do not create a new deal; attach the note and contact to the existing deal and flag the handoff "existing deal" for the manager

**Touches:** handoffs, HubSpot service, accounts, people.
**Depends on:** R3-01.

---

## R3-03 AE routing

**As a** manager, **I want** handoffs routed to the right AE automatically, **so that** I do not assign each one by hand.

**Acceptance criteria**

- [ ] Default AE = the SDR's suggested AE if set; otherwise by account region, round robin among active AEs in that region; fallback to a default AE set in settings
- [ ] Manager can reroute a handoff before or after push; reroute updates the HubSpot deal owner
- [ ] Routing decision logged in audit log

**Touches:** routing service, handoffs.
**Depends on:** R3-02.

---

## R3-04 Inbound sync - suppression

**As an** admin, **I want** HubSpot customers and open deals synced in daily, **so that** SDRs never prospect them.

**Acceptance criteria**

- [ ] Nightly job pulls: companies with lifecycle stage = customer; companies with any open deal
- [ ] Their domains are written to suppression with reason customer / open_deal and source = hubspot
- [ ] Matching accounts flagged suppressed; if such an account is assigned to an SDR, the SDR sees a banner "Customer / open deal in HubSpot - do not contact" and outreach actions are disabled
- [ ] Entries removed from suppression when no longer true (deal closed lost, customer churned), except manual entries
- [ ] Manual "Sync now" button for admin; last sync time shown

**Touches:** sync job, suppression, accounts.
**Depends on:** R3-01.

---

## R3-05 Inbound sync - deal outcomes

**As an** admin, **I want** deal stages and amounts synced back, **so that** I can attribute pipeline and revenue to sources and plays.

**Acceptance criteria**

- [ ] Nightly job updates each handoff with current deal stage, amount, close date, won / lost
- [ ] Account stage reflects outcome: Won / Lost added as terminal stages
- [ ] Closed-lost accounts move to Nurture after 90 days and return to the SDR pool

**Touches:** handoffs, accounts, sync job.
**Depends on:** R3-02.

---

## Release 3 demo checklist

- [ ] Mark Positive on a test contact creates company, contact, deal and note in HubSpot within one minute, owned by the right AE
- [ ] Retry on the same handoff does not duplicate anything
- [ ] Marking positive on an account with an open HubSpot deal attaches to that deal instead
- [ ] A HubSpot customer domain appears in suppression after sync and its account is blocked for outreach
- [ ] Moving the test deal to Closed Won in HubSpot shows Won on the account after sync
