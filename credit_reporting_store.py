"""SQL projections from the confirmed Apollo credit ledger."""

from decimal import Decimal

from account_store import active_session_user


class _SqliteCreditSum:
    def __init__(self):
        self.total = Decimal(0)

    def step(self, credits):
        if credits is not None:
            self.total += Decimal(str(credits))

    def finalize(self):
        return format(self.total, "f")


def credit_sum_function(connection):
    if getattr(connection, "dialect", "sqlite") == "postgresql":
        return "SUM"
    connection.create_aggregate("credit_sum", 1, _SqliteCreditSum)
    return "credit_sum"


class SqlCreditReportingStore:
    def __init__(self, connection):
        self.connection = connection

    def active_user(self, user_id):
        return active_session_user(self.connection, user_id)

    def user_total(self, user_id, day=None):
        credit_sum = credit_sum_function(self.connection)
        values = (user_id,)
        query = f"""
            SELECT COALESCE({credit_sum}(credits), '0')
            FROM credit_ledger WHERE user_id = ? AND source <> 'offline'
        """
        if day is not None:
            query += " AND on_day = ?"
            values += (day,)
        return Decimal(str(self.connection.execute(query, values).fetchone()[0]))

    def team_totals(self):
        credit_sum = credit_sum_function(self.connection)
        return [dict(row, credits=Decimal(str(row["credits"]))) for row in self.connection.execute(f"""
            SELECT l.user_id, u.name, u.active, {credit_sum}(l.credits) AS credits
            FROM credit_ledger l LEFT JOIN users u ON u.id = l.user_id
            WHERE l.source <> 'offline'
            GROUP BY l.user_id, u.name, u.active ORDER BY u.name, l.user_id
        """)]
