"""Currency conversion with the day's rates (fixed here; the real service reads them each morning)."""
from decimal import Decimal, ROUND_HALF_UP

RATES = {("EUR", "GBP"): Decimal("0.8417"), ("GBP", "EUR"): Decimal("1.1881"), ("EUR", "USD"): Decimal("1.0732")}


def convert(amount, src, dst):
    if src == dst:
        return amount
    return (amount * RATES[(src, dst)]).quantize(Decimal("0.01"), ROUND_HALF_UP)
