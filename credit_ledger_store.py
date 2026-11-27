"""Portable R1-08 ledger schema and legacy upgrade."""

from crm_core.credit_ledger import CREDIT_ACTIONS, OPENING_ADJUSTMENT


ACTIONS_SQL = ", ".join(f"'{action}'" for action in (*CREDIT_ACTIONS, OPENING_ADJUSTMENT))
VALID_ACTION = f"CHECK (action IN ({ACTIONS_SQL}))"
VALID_SOURCE = "CHECK (source IN ('offline', 'apollo', 'historical', 'adjustment'))"
VALID_REFERENCE = "CHECK (source = 'offline' OR (reference IS NOT NULL AND length(trim(reference)) > 0))"
VALID_LINKS = """
    CHECK ((source = 'offline' OR source = 'apollo' AND job_id IS NOT NULL
              AND user_id IS NOT NULL AND account_id IS NOT NULL
              OR source = 'historical' AND job_id IS NULL
              OR source = 'adjustment' AND job_id IS NULL AND user_id IS NULL
                  AND account_id IS NULL AND person_id IS NULL))
"""
VALID_CREDITS = f"""
    CHECK (abs(credits) < 1000000000 AND credits = round(credits, 6) AND
        ((source = 'adjustment' AND action = '{OPENING_ADJUSTMENT}' AND credits <> 0)
         OR (source <> 'adjustment' AND action <> '{OPENING_ADJUSTMENT}' AND credits > 0)))
"""
LEDGER_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS credit_ledger (
    id INTEGER PRIMARY KEY,
    job_id INTEGER UNIQUE REFERENCES credit_jobs(id),
    user_id INTEGER REFERENCES users(id),
    account_id INTEGER REFERENCES accounts(id),
    person_id INTEGER REFERENCES people(id),
    action TEXT NOT NULL,
    credits NUMERIC NOT NULL,
    on_day TEXT NOT NULL,
    at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'offline',
    reference TEXT,
    CONSTRAINT credit_ledger_action_allowed {VALID_ACTION},
    CONSTRAINT credit_ledger_source_allowed {VALID_SOURCE},
    CONSTRAINT credit_ledger_reference_required {VALID_REFERENCE},
    CONSTRAINT credit_ledger_links_allowed {VALID_LINKS},
    CONSTRAINT credit_ledger_valid_amount {VALID_CREDITS}
)
"""


def initialize_credit_ledger(connection):
    fields = {row["name"] for row in connection.execute("PRAGMA table_info(credit_ledger)")}
    if not fields:
        connection.execute(LEDGER_SCHEMA)
    elif "source" not in fields:
        if getattr(connection, "dialect", "sqlite") == "postgresql":
            _upgrade_postgres(connection)
        else:
            connection.execute("ALTER TABLE credit_ledger RENAME TO legacy_credit_ledger")
            connection.execute(LEDGER_SCHEMA)
            connection.execute("""
                INSERT INTO credit_ledger(id, job_id, user_id, account_id, person_id, action, credits, on_day, at)
                SELECT id, job_id, user_id, account_id, person_id, action, credits, on_day, at
                FROM legacy_credit_ledger
            """)
            connection.execute("DROP TABLE legacy_credit_ledger")
    connection.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS credit_ledger_source_reference
        ON credit_ledger(source, reference)
    """)
    connection.execute(f"""
        CREATE UNIQUE INDEX IF NOT EXISTS credit_ledger_one_opening_adjustment
        ON credit_ledger(action) WHERE action = '{OPENING_ADJUSTMENT}'
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS credit_ledger_daily_owner
        ON credit_ledger(user_id, on_day) WHERE source <> 'offline'
    """)


def _upgrade_postgres(connection):
    connection.execute(f"""
        ALTER TABLE credit_ledger
            DROP CONSTRAINT IF EXISTS credit_ledger_credits_check,
            ALTER COLUMN job_id DROP NOT NULL,
            ALTER COLUMN user_id DROP NOT NULL,
            ALTER COLUMN account_id DROP NOT NULL,
            ALTER COLUMN credits TYPE NUMERIC,
            ADD COLUMN source TEXT NOT NULL DEFAULT 'offline',
            ADD COLUMN reference TEXT,
            ADD CONSTRAINT credit_ledger_action_allowed {VALID_ACTION},
            ADD CONSTRAINT credit_ledger_source_allowed {VALID_SOURCE},
            ADD CONSTRAINT credit_ledger_reference_required {VALID_REFERENCE},
            ADD CONSTRAINT credit_ledger_links_allowed {VALID_LINKS},
            ADD CONSTRAINT credit_ledger_valid_amount {VALID_CREDITS}
    """)
