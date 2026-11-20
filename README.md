# Nunify CRM

Local two-source demo built with Flask and SQLite. The home page shows the AARP account with three saved Cvent events and the national Directory of Associations listing.

## Open the demo

From PowerShell, run these commands in `C:\Users\hp\Desktop\nunify-crm`. Use Python 3.11+ with SQLite 3.38+. Flask 3.1.3 is already installed in the `python` environment used to verify this workspace.

```powershell
python import_demo.py
python app.py
```

Open [Nunify CRM](http://127.0.0.1:5000/). Select **Canadian Technical Asphalt Association** for the directory-only example or **Directory review** for the staged listings. On an event, expand **Source evidence**. **View event record** and **View saved record** show the original IDs, values and filenames.

On a machine that needs dependencies, first create a workspace virtual environment. Use its interpreter for the two commands above.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The import reads saved exports from sibling folders `cvent-scrapper` and `association`. Set `--cvent-dir` or `--directory-dir` if those checkouts are elsewhere. The demo database is `instance/demo.sqlite3`. The app binds to loopback and reads the database without writes during requests. The application uses no external services or Apollo calls.

## Verify the import

```powershell
python import_demo.py
python import_demo.py
python -m unittest discover -s tests -v
```

On a fresh database, the first run reports nine changed source records. The second run reports zero. Running against the seeded local database reports zero on both runs. Tests use fresh databases and compare every stored account and source row, including original values. See [the milestone verification](docs/two-source-demo.md) for counts, source revisions and remaining release gates.
