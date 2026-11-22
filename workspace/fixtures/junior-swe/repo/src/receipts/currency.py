"""Currency conversion with the day's rates (fixed here; the real service reads them each morning)."""
from decimal import Decimal

RATES = {("EUR", "GBP"): Decimal("0.8417"), ("GBP", "EUR"): Decimal("1.1881"), ("EUR", "USD"): Decimal("1.0732")}


def convert(amount, src, dst):
    """amount in src as dst, unrounded: callers round once, at the end (spec 3.2)."""
    if src == dst:
        return amount
    return amount * RATES[(src, dst)]
