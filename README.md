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

Use a dedicated, empty Neon database for the CRM pilot. Create a Render Python Web Service from the branch with this release. Select the Free instance and set the health check path to `/login`.

| Render setting | Value |
| --- | --- |
| Build Command | `pip install -r requirements.txt gunicorn` |
| Start Command | `gunicorn --workers 1 --bind 0.0.0.0:$PORT 'pilot_web:create_pilot_app()'` |

In Render's **Environment** settings, set `DATABASE_URL` to the direct Neon connection string. Generate a separate `CRM_SECRET_KEY` with `python -c "import secrets; print(secrets.token_hex(32))"`. Add a distinct strong password for each pilot role.

| Environment variable | Pilot sign-in |
| --- | --- |
| `CRM_ADMIN_PASSWORD` | `admin@pilot.example` |
| `CRM_MANAGER_PASSWORD` | `manager@pilot.example` |
| `CRM_SDR_PASSWORD` | `sdr@pilot.example` |

The first start creates the pilot sign-ins and adds **Pilot Association** to an empty account list. Assign the sample company to the SDR, save a simulated call with **Use tomorrow**, then redeploy and check the saved follow-up. The pilot starts the web app. Source jobs need a worker, and saved local CRM data needs a separate migration.

## Load and work accounts

For a role-by-role walkthrough, use the [manual SDR trial guide](docs/manual-sdr-trial.md).

1. As admin, open **Sources**. Stage `master_cvent_events.json` from the sibling Cvent project and `all_associations.json` from the sibling Association project. Set `--cvent-dir` or `--directory-dir` on the worker to change the input folder. Use a daily, weekly, or monthly calendar schedule to prepare saved-file previews at a chosen time.
2. Check each preview and promote eligible records. Directory defaults cover national, state, and international listings with at least 1,000 members in relevant categories. Edit those filters before staging. In **Source review**, managers and admins can verify uncertain records against an existing organization and record a decision.
3. In **Operations**, queue qualification from saved evidence. In **Assignments**, filter accounts by source, assign to one rep, or preview a capped round-robin allocation across several reps. For a transfer, the manager confirms the existing ownership change.
4. In a separate browser profile, sign in as the rep. Open an assigned account from **Today** or **My Accounts**, check its source evidence, log an activity, and use **Use tomorrow** to set a follow-up. Open **Today → Tomorrow** to see the task.
5. Log a demo or pricing request and record the promised next step in an AE handoff. The offline worker delivers the saved context to the AE's local inbox. Managers can transfer ownership or authorize a temporary coverer from the account.

Sources records failed and retried runs. To exercise recovery on a disposable database, queue a saved Cvent run, drain it once with `--cvent-dir instance/missing-source`, then use **Retry run** and drain with the correct input folder.

Future scrapers can send named batches to a scoped endpoint. See [external scraper integration](docs/ingest-api.md) and the [working notes](working.md) for the local behavior and remaining live dependencies.

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

To include the PostgreSQL rehearsal, set `CRM_TEST_POSTGRES_URL` to a disposable database and rerun the suite. The test uses a dedicated temporary schema and exercises the Admin, Manager and SDR flow across an app restart.

See [workflow design](docs/workflow-design.md), [delivery and verification](docs/workflow-delivery.md), and Goutham's [architecture](docs/architecture.md) and [stories](docs/stories/00_README.md).
