# Manager follow-up queue

During the Render demo on 27 November 2026, Riya saved follow-ups due 28 and 30 November. The Manager needs to find both from Team queue with Riya selected. This extends [R2-04 tasks](stories/02_R2a_sdr_core.md#r2-04-tasks-and-next-actions-migration) and [R2-11 Manager workload](stories/03_R2b_sdr_completion.md#r2-11-assignment-screen). [R2-05 daily work](stories/02_R2a_sdr_core.md#r2-05-today-queue-sdr-home) supplies the Today and Tomorrow views.

| Check | Observable result |
| --- | --- |
| MQ-01 | The Manager and Admin open Team queue from the sidebar and land on **All open**. The **SDR** dropdown includes **All SDRs** and individual SDRs. Selecting Riya and applying the filter shows both saved dates together, plus eligible overdue follow-ups, for any company Riya owns a task on. The existing Today and Tomorrow links continue to open their daily views. |
| MQ-02 | A row shows the account link, task title, owner, due date and status. The filter uses the saved task owner and respects search and pagination. Open and rescheduled tasks appear. Completed tasks and suppressed accounts or contacts stay out of the queue. |
| MQ-03 | A Manager can reschedule or complete an eligible task using the existing actions and returns to the same selected SDR in All open. A permanent account transfer moves the task into the new owner's queue. Direct requests for the staff view from an SDR or AE return 403. Normal SDR Today, Tomorrow and coverage behavior remains available. |

The view reads the existing task records through the work-store adapter. The shared agenda rules apply account visibility, outreach restrictions and the task date in the account owner's timezone. Flask selects the view, handles filters and renders the rows. There is no database migration for this change.
