# Release 5 - Campaigns and Reporting (Phase 5)

## Context for the agent

Reps now work accounts, log activity and hand off positives to HubSpot. This release connects an email sequencing tool so email activity logs itself, turns saved Account views into "plays" that drive campaigns, and gives admins the funnel and ROI reporting that decides which sources and plays get more investment. Do not build an email sending engine; integrate the chosen sequencer. See `docs/architecture.md` sections 4.5 and 6.2.

**Release goal:** campaigns are created from plays, replies log automatically, and admins can see conversion and credits per positive by source, type, play and rep.

**Decision needed before starting:** sequencer (Apollo Sequences, Instantly, lemlist, Smartlead or similar). Stories below are written sequencer-agnostic behind an adapter.

---

## R5-01 Sequencer adapter

**As a** developer, **I want** a sequencer adapter interface, **so that** we can switch tools without rewriting campaigns.

**Acceptance criteria**

- [ ] Interface: list sequences, add contacts to sequence (with custom fields), remove / pause contact, receive webhook events
- [ ] First implementation for the chosen sequencer
- [ ] Credentials stored encrypted in admin settings, with a test connection button
- [ ] Custom fields passed per contact: first name, account name, account type, platform, next event name and date, SDR name (for personalization)

**Touches:** sequencer service, settings.
**Depends on:** Release 3.

---

## R5-02 Campaigns and plays [migration]

**As a** manager, **I want** to create campaigns from plays, **so that** the right message reaches the right segment.

**Acceptance criteria**

- [ ] Saved views from R1-11 can be marked as **plays** with a name, description and message angle
- [ ] Seeded plays (architecture doc 4.5): Displacement - associations; Displacement - corporate; Agency multiplier; Greenfield - associations; Timing window
- [ ] Table `campaigns` (id, name, play_id, channel: email / linkedin / call / multi, sequencer_sequence_id, owner_id, status: draft / active / paused / done, created_at)
- [ ] Table `campaign_members` (campaign_id, person_id, added_by, added_at, status: active / paused / finished / removed, removed_reason)
- [ ] Manager creates a campaign from a play and links it to a sequencer sequence
- [ ] Adding members: from a play (all eligible people matching persona tiers selected) or from account / contact pages (SDR, own accounts only)
- [ ] Eligibility checks: unlocked, verified email, not suppressed, not do_not_contact, not already active in another campaign, account not handed off

**Touches:** plays, campaigns, campaign_members, UI.
**Depends on:** R5-01.

---

## R5-03 Webhook activity logging

**As an** SDR, **I want** sequencer emails logged automatically, **so that** I do not log them by hand.

**Acceptance criteria**

- [ ] Webhook endpoint (verified signature) receives: sent, opened, clicked, replied, bounced, unsubscribed
- [ ] Sent and replied create `activities` rows (source = webhook); opens and clicks stored as counters on the campaign member, not as activities
- [ ] Reply -> person status replied, appears at top of the SDR's Today queue, contact paused in sequence
- [ ] Bounce -> person bounced + do_not_contact; unsubscribe -> opted_out + suppression
- [ ] Idempotent on duplicate webhook deliveries

**Touches:** webhook route, activities, people, campaign_members, suppression.
**Depends on:** R5-02.

---

## R5-04 LinkedIn and call steps in campaigns

**As an** SDR, **I want** manual LinkedIn and call steps to show as tasks, **so that** multi-channel campaigns run from one queue.

**Acceptance criteria**

- [ ] Campaign can define manual steps (day offset, type: linkedin connect / linkedin message / call, instruction text)
- [ ] When a member reaches a manual step day, a task is created for the account owner
- [ ] Completing the task via Log activity advances the step

**Touches:** campaigns, tasks.
**Depends on:** R5-02.

---

## R5-05 Admin funnel dashboard

**As an** admin, **I want** a funnel by source, type, play and rep, **so that** I know where to invest.

**Acceptance criteria**

- [ ] Funnel stages (accounts): discovered -> qualified -> enriched -> assigned -> working -> engaged -> positive -> handed off -> won
- [ ] Breakdown selector: source, account type, platform, play, rep, region; date range filter
- [ ] Metrics per row: count at each stage, stage-to-stage conversion, credits spent, credits per positive, positives per 100 contacts touched, HubSpot pipeline value and won value (from R3-05)
- [ ] Rep table: accounts owned, touches (7 / 30 days), replies, positives, handoffs, won, credits used
- [ ] Campaign table: members, sent, reply rate, positive rate, bounces
- [ ] CSV export of every table (admin only, audited)

**Touches:** reporting queries, admin Overview screen.
**Depends on:** R5-03, R3-05.

---

## R5-06 Timing triggers

**As an** SDR, **I want** to be prompted when an account enters its buying window, **so that** I reach out at the right time.

**Acceptance criteria**

- [ ] Nightly job: accounts whose next event date crosses into 9 months out and have no activity in 30 days create a task for the owner ("Event in planning window: {event} on {date}")
- [ ] Unassigned accounts crossing the window surface in the Assignment screen as "In window - unassigned"
- [ ] Window bounds configurable (default 4-9 months)

**Touches:** scheduler, tasks, Assignment screen.
**Depends on:** R2-04.

---

## Release 5 demo checklist

- [ ] Create a campaign from the "Displacement - associations" play and push eligible contacts to the sequencer
- [ ] Suppressed, do-not-contact and already-sequenced people are excluded automatically
- [ ] A test reply in the sequencer appears at the top of the SDR's Today queue within minutes and pauses the contact
- [ ] A test bounce marks the person bounced and do-not-contact
- [ ] Funnel shows Cvent vs Directory of Associations side by side with credits per positive
- [ ] An account crossing into the 9-month window creates a task for its owner
