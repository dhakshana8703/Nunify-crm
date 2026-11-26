# Nunify CRM

Nunify combines saved Cvent events and Directory of Associations listings in one outbound account list. Managers can filter and assign accounts, SDRs work their queue, and AEs receive qualified handoffs with the conversation history.

## Run locally

Use Python 3.11+ and SQLite 3.38+. From this folder in PowerShell:

```powershell
python -m pip install -r requirements.txt
python -B app.py --db instance/workspace.sqlite3
```

Open [Nunify CRM](http://127.0.0.1:5000/). In a second terminal, run the worker against the same database:

```powershell
python -B worker.py --offline --db instance/workspace.sqlite3
```

The worker reads saved source exports and uses local adapters for Apollo and AE delivery. Add `--once` to drain the queued jobs and exit. These sign-ins apply to the running local workspace and use the password `nunify-demo`. For a fresh disposable database, add `--demo` to seed local test users. Campaign and handoff test contacts are created inside their test databases.

| Role | Sign-in |
| --- | --- |
| Manager | `manager@example.test` |
| Riya Rao, SDR | `riya@example.test` |
| Arjun Mehta, SDR | `arjun@example.test` |
| Neha Shah, SDR | `neha@example.test` |
| Kavya Nair, SDR | `kavya@example.test` |
| AE | `ae@example.test` |
| Admin | `admin@example.test` |

## Hosted pilot on Render and Neon

Use a dedicated, empty Neon database for the CRM pilot. Create a Render Python Web Service from the branch with this release and select the Free instance. For a build that includes database readiness, set the health check path to `/healthz`. Check **Operations** for the worker's last completed pass.

| Render setting | Value |
| --- | --- |
| Build Command | `pip install -r requirements.txt gunicorn` |
| Start Command | `gunicorn --workers 1 --bind 0.0.0.0:$PORT 'pilot_web:create_pilot_app()'` |

In Render's **Environment** settings, set `DATABASE_URL` to the direct Neon connection string. Generate a separate `CRM_SECRET_KEY` with `python -c "import secrets; print(secrets.token_hex(32))"`. Add a distinct password for each pilot user.

| Environment variable | Pilot sign-in |
| --- | --- |
| `CRM_ADMIN_PASSWORD` | `admin@nunify.com` |
| `CRM_MANAGER_PASSWORD` | `manager@nunify.com` |
| `CRM_SDR_PASSWORD` | `riya@nunify.com` |
| `CRM_ARJUN_PASSWORD` | `arjun@nunify.com` |
| `CRM_NEHA_PASSWORD` | `neha@nunify.com` |
| `CRM_KAVYA_PASSWORD` | `kavya@nunify.com` |

The first start creates the admin, manager, and Riya sign-ins and adds **Pilot Association** to an empty account list. Set the additional password variables to create separate logins for Arjun, Neha, and Kavya. Each active SDR appears in the manager's assignment dropdown. An earlier pilot's test sign-ins are renamed in place, preserving users' assignments and history. Updated environment passwords take effect on deployment. Assign the sample company to an SDR, save a simulated call with **Use tomorrow**, then redeploy and check the saved follow-up. The pilot starts the web app. Source jobs need a worker, and saved local CRM data needs a separate migration.

### Transfer the prepared companies and contacts once

After the hosted CRM is running, set strong passwords for its users before adding real contacts. From the `nunify-crm` folder on the computer with the saved local CRM, run a preview:

```powershell
python -B transfer_saved_sources.py
```

Paste the direct connection URI for the CRM's Neon database at the prompt. The terminal keeps it hidden. Review the proposed counts, then apply:

```powershell
python -B transfer_saved_sources.py --apply
```

Enter the same URI and the hosted admin password. The default input is `instance/association-trial.sqlite3`. The transfer copies the saved Cvent and Directory company records, review items, qualification results, source-linked contacts, and prior Apollo unlocks. It preserves existing hosted users, saved work, and suppression rules. Local trial users and sales activity, the demo contact, and raw scrape run history stay in the local database. Identity conflicts stop the transfer and leave the hosted data intact.

On Render, refresh **Assignments** and select **Qualified and unassigned** to start allocating companies. Future imports can run separately through the source worker.

### Transfer the saved companies and Cvent roster

Use the company-only mode for the 769-account saved rehearsal. It also carries the selected Cvent company roster and its review status. Existing hosted work stays in place.

```powershell
python -B transfer_saved_sources.py --source instance/r1-03-name-rehearsal.sqlite3 --companies-only --apply
```

Use the direct Neon connection for the database backing the **Nunify CRM** Render service. The command shows the preview before it prompts for the hosted Admin password. Both connection and password prompts keep the typed values hidden. Omit `--apply` to run only the preview.

## Load and work accounts

For a role-by-role walkthrough, use the [manual SDR trial guide](docs/manual-sdr-trial.md).

1. As admin, open **Sources**. Stage `master_cvent_events.json` from the sibling Cvent project and `all_associations.json` from the sibling Association project. Set `--cvent-dir` or `--directory-dir` on the worker to change the input folder. Use a daily, weekly, or monthly calendar schedule to prepare saved-file previews at a chosen time.
2. Check each preview and promote eligible records. Directory defaults cover national, state, and international listings with at least 1,000 members in relevant categories. Edit those filters before staging. In **Source review**, managers and admins can verify uncertain records against an existing organization and record a decision.
3. In **Operations**, queue qualification from saved evidence. In **Assignments**, filter accounts by source, assign to one rep, or preview a capped round-robin allocation across several reps. For a transfer, the manager confirms the existing ownership change.
4. In a separate browser profile, sign in as the rep. Open an assigned account from **Today** or **My Accounts**, check its source evidence, log an activity, and use **Use tomorrow** to set a follow-up. Open **Today → Tomorrow** to see the task.
5. Log a demo or pricing request and record the promised next step in an AE handoff. The offline worker delivers the saved context to the AE's local inbox. Managers can transfer ownership or authorize a temporary coverer from the account.

Sources records failed and retried runs. To exercise recovery on a disposable database, queue a saved Cvent run, drain it once with `--cvent-dir instance/missing-source`, then use **Retry run** and drain with the correct input folder.

### Connect or disconnect a saved source

Set `CRM_SOURCE_CONNECTORS` to the same value for the web app and source worker, then restart both.

| Value | Saved imports available |
| --- | --- |
| `cvent,directory` or unset | Cvent and Directory of Associations |
| `cvent` | Cvent only |
| `directory` | Directory of Associations only |
| `none` | Neither saved export |

To disconnect Cvent locally, run `$env:CRM_SOURCE_CONNECTORS = "directory"` in the PowerShell session used to launch each process. In **Sources**, the disconnected source stays visible with its run history. Saved source tags, contacts, ownership, activity and follow-ups stay on the same accounts. Pending jobs and schedules for the disconnected source wait until it is connected again. Restore `cvent,directory` and restart both processes to resume its imports.

CSV, XLSX and external API intake remain available. Add future scrapers through the [external scraper integration](docs/ingest-api.md). Revoke a scraper's key in **Sources** to block future submissions from that scraper. Work already submitted stays in the CRM.

Use **People** to see account-scoped contacts and check their source and email verification status. To preview named Cvent contacts for already-linked events in an existing local database:

```powershell
python -B cvent_contacts.py --db instance/workspace.sqlite3
```

Use `--apply --admin-email admin@example.test` to apply validated contacts with an interactive admin password prompt. The import accepts explicit saved first-party work contacts and records their event evidence. Deliverability and paid Apollo unlocks have their own verification.

## Import saved Cvent Apollo leads

Preview a saved legacy Apollo cache against the accounts with corroborated Cvent organizer evidence.

```powershell
python -B cvent_apollo_import.py --db instance/association-trial.sqlite3
```

Use `--source` to select a matching read-only cache snapshot. Add `--apply --admin-email admin@example.test` for the local trial and authenticate at the prompt. The importer keeps the original Apollo ID and prior unlock state, restores only supported saved channels, and records aggregate results in the audit log. Ambiguous matches remain in the source snapshot for review. Credit reconciliation uses the deployed data and provider usage.

## Verify

```powershell
python -B -m unittest discover -s tests -v
```

The default suite uses synthetic source fixtures and runs from a clean checkout. To include the PostgreSQL rehearsal, set `CRM_TEST_POSTGRES_URL` to a disposable database and rerun the suite. The test uses a dedicated temporary schema and exercises the Admin, Manager and SDR flow across an app restart. Pull requests run the same suite with a disposable PostgreSQL service in GitHub Actions once the workflow is pushed.

See the [engineering foundation specification](docs/engineering-foundation.md), [workflow design](docs/workflow-design.md), [delivery and verification](docs/workflow-delivery.md), and Goutham's [architecture](docs/architecture.md) and [stories](docs/stories/00_README.md).
