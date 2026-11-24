# Try the SDR workspace

## First time in Nunify

Use the [Association and Cvent trial](http://127.0.0.1:5002/login) for this first test. The old port 5001 address opens the same sign-in. Use the passwords provided in the trial handoff.

1. **Admin.** Sign in as `admin@nunify.com`. On **Sources**, open the Ready **Cvent - master_cvent_events.json** run, or click **Stage Cvent export** to start a fresh preview. The run page updates as the worker progresses. When it is Ready, open **Review unresolved records** to see what needs a check, then use **Promote eligible events** to apply the eligible rows. The saved export in this trial has 542 records.
2. **Manager.** Sign out and sign in as `manager@nunify.com`. On **Assignments**, set **Owner status** to **Qualified and unassigned** and select **SIFMA** under **Choose a company**. Select **Riya Rao** under **New owner**, then click **Assign**. Look for **Assignment saved for SIFMA**. If Riya already owns SIFMA, start with step 3.
3. **SDR.** Sign out and sign in as `riya@nunify.com`. Open **SIFMA** from **Today** or **My Accounts**. Check its **Cvent** tag, source evidence, and **Contacts**. Choose a contact marked **Previously unlocked** for the exercise. In **Log activity**, simulate a **Call** with **No answer**. Check **Use tomorrow**, set **Next action** to `Call SIFMA again`, and click **Save activity**. Open **Today**, then **Tomorrow**. You should see one follow-up under SIFMA.
4. **Manager check.** Sign in as the manager again. Open **SIFMA** and check **Follow-ups** for Riya's saved task.

At each step, note the screen, what you clicked, and what happened. Save those observations for the next improvement.

### What to tell your manager

> The admin reviews the imported data and works through records flagged for a check. The manager assigns a company to a rep. The rep can see why the company is a prospect, review the saved contacts, record a call, and set the next follow-up. The manager can see that follow-up on the company record. We are testing with saved data and a simulated call.

To restart this trial, run these commands in separate PowerShell windows from the `nunify-crm` folder.

```powershell
python -B app.py --db instance/association-trial.sqlite3 --port 5002
python -B source_ops.py --db instance/association-trial.sqlite3
```

## Wested walkthrough

Start the separate Wested trial using the commands below. Open the [Wested trial](http://localhost:5003/login) and sign in as `riya@example.test`. **Wested is already assigned to Riya** in this trial. Start with her first call and follow-up before moving to the other cases.

The trial app and offline worker use `instance/sdr-trial.sqlite3`. Trial assignments, notes, and tasks save in that database. The regular workspace uses `instance/workspace.sqlite3` at `http://127.0.0.1:5000`. Simulate the calls and emails in the activity form. The handoff case uses a local AE inbox.

Use separate browser profiles to keep two roles signed in at the same time. You can also sign out before switching roles.

| Role | Trial sign-in |
| --- | --- |
| SDR with an assigned account | `riya@example.test` |
| Peer SDR for the access check | `arjun@example.test` |
| SDR for coverage or transfer | `neha@example.test` |
| Manager for assignment and coverage | `manager@example.test` |
| AE for the handoff check | `ae@example.test` |

All five trial users use `nunify-demo`.

## What Riya sees

- **Today** is her landing page. It shows next actions such as newly assigned accounts, replies needing a response, and tasks that are due. **Tomorrow** shows the follow-ups due the next day.
- **My Accounts** lists the accounts she owns. Open **Wested** to see **Why this account matters**, the saved Cvent evidence, **Contacts**, **Log activity**, **Follow-ups**, and the **Activity timeline**. If you return on a later day, you can still open it from My Accounts.
- **People** shows contacts for accounts she can access. Opening a contact shows their details, outreach status, own activity timeline, and open follow-ups. **Handoffs** lets her track positive outcomes queued for an AE.

## 1. Log the first call and find tomorrow's follow-up

1. Sign in as Riya and open **Wested** from **Today** or **My Accounts**.
2. Read **Why this account matters** and select **View source evidence**. Check that the account shows its saved Cvent record and a contact.
3. Under **Log activity**, choose the saved **Contact**, select **Call** and **No answer**, and enter `Trial call - no answer` in **Notes**.
4. Click **Use tomorrow** next to **Follow-up date**, enter `Call Wested again` in **Next action**, then click **Save activity**.
5. Open **Today → Tomorrow**.

**Expected result**

The Activity timeline shows one call. **Newly assigned** clears from Today and Tomorrow shows one `Call Wested again` task. Refresh the page to check that it was saved. Leave the task open if you want to test transfer later.

## 2. Log and respond to a reply

1. Open Wested. Under **Log activity**, select the saved contact and choose **Email → Reply received**. Add a simulated note and click **Save activity**.
2. Go to **Today** and find **Unanswered reply** for that contact.
3. Return to Wested and log **Email → Sent** for the same contact, then check **Today** again.

**Expected result**

The later outbound activity clears **Unanswered reply** for that contact. The Activity timeline retains both entries. **Mark reviewed** is also available for an inbound item that needs no further reply.

## 3. Create and manage a standalone follow-up

1. Open Wested and expand **Follow-ups → Add follow-up**. Enter `Review the event page` in **Next action**, set the **Due date**, and click **Add follow-up**. You can choose the contact or leave it as an account follow-up.
2. Check **Today** or **Tomorrow** using the date you chose. An account follow-up also appears under Wested's **Follow-ups**.
3. Click **Reschedule**, select another date, and click **Save due date**. To close a task that appears in the queue, click **Mark complete**.

**Expected result**

The task appears once and the Activity timeline keeps its existing entries. The new due date appears under Wested's **Follow-ups**. Completing the task removes it from the open queue. If you want to check that open tasks transfer with an account, keep at least one follow-up open.

## 4. Work from a contact record

1. Open a contact by name from Wested's **Contacts** section or **People**. Check their source, contact details, outreach status, open follow-ups, and Activity timeline.
2. To try editing on your own record, add a contact named `Trial Events Contact` with `trial-events@example.test` from Wested's **Add contact** form. Open the new contact, select **Edit details**, update the job title, and click **Save details**.
3. On the contact page, use **Log activity** to open the account with that person selected, or use **Add follow-up** to create a task linked to that contact.

**Expected result**

The title stays saved after refresh. A follow-up created on the contact opens under that person's record and appears on the account's follow-up list. Trying to add the same email to Wested again returns a duplicate-contact message.

## 5. Send a demo request to the local AE inbox

1. As Riya, log **Email → Demo requested** on Wested using the saved contact.
2. In **Activity timeline**, click **Prepare AE handoff**, then expand **Send a positive outcome to an AE**.
3. Select the same contact, **Demo requested**, and the receiving AE. Fill in **Conversation context** and **Promised next step**. Click **Queue AE handoff**.
4. Check **Handoffs**. Once it shows **Delivered**, sign in as the AE in another profile, open **Handoffs**, and read the record.

**Expected result**

The AE sees the assigned contact, your context, the promised next step, and recent activity. The offline worker delivers the handoff inside the trial app.

## 6. Check that another SDR cannot open Riya's account

Sign in as Arjun and open **My Accounts**. Then try opening the [Wested account page](http://localhost:5003/accounts/745) directly while Riya owns it and Arjun has no coverage.

**Expected result**

Wested is absent from Arjun's assigned list. The direct link returns **403**, and Riya's follow-up is absent from Arjun's queue.

## 7. Try temporary leave coverage with the manager

1. As manager, open [Wested](http://localhost:5003/accounts/745). Expand **Leave coverage → Schedule coverage** and select Neha. Set **Starts** a few minutes in the past and **Ends** in the future, using the timezone shown on the form. Click **Schedule coverage**.
2. As Neha, open **My Accounts**, set **Access** to **Coverage**, and click **Apply**. Open Wested and log a simulated reply.
3. As manager, return to the account and click **End coverage**. Refresh the account in Neha's profile.

**Expected result**

Neha can work the account during coverage. Riya stays the owner, and the timeline shows Neha as the person who logged the reply. Ending coverage removes Neha's access.

## 8. Try a permanent transfer last

1. Make sure an open Wested task exists. As manager, go to **Assignments** and select **Wested** under **Choose a company**.
2. Select Neha as the new owner and click **Transfer**.
3. As Neha, open **My Accounts** and Wested's **Follow-ups**. As Riya, try the account link again.

**Expected result**

The open task moves with the account, the activity history stays visible to the new owner, and Riya loses direct access. The manager can transfer it back on the trial copy to repeat the SDR cases.

## Start the Wested trial

If the trial has stopped, use two PowerShell windows from the `nunify-crm` folder. Run the first line in one window and the second line in the other.

```powershell
python -B app.py --db instance/sdr-trial.sqlite3 --port 5003
python -B worker.py --offline --db instance/sdr-trial.sqlite3
```
