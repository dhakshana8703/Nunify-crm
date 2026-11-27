"""Monetary units for confirmed Apollo usage and reconciled history."""

from decimal import Decimal, InvalidOperation


CREDIT_ACTIONS = ("org_match", "people_search", "email_unlock", "phone_unlock")
OPENING_ADJUSTMENT = "opening_balance_adjustment"


def parse_credits(value):
    """Return the exact supported credit amount or reject an unknown receipt."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError("A credit amount is required.")
    try:
        credits = Decimal(str(value))
        if credits.is_finite() and credits.copy_abs() < 1_000_000_000:
            rounded = credits.quantize(Decimal("0.000001"))
            if rounded == credits:
                return rounded
    except InvalidOperation:
        pass
    raise ValueError("Enter a credit amount with at most six decimal places.")
