"""An order's receipt: subtotal, tax and total in the customer's currency."""
from decimal import Decimal, ROUND_HALF_EVEN

from .currency import convert

CENT = Decimal("0.01")


def receipt(order):
    """Sum the lines and the tax unrounded, convert, then round once, half to even (spec 3.2, RCP-142)."""
    subtotal = sum((l["price"] * l["qty"] for l in order["lines"]), Decimal(0))
    tax = subtotal * Decimal(order["tax_rate"])
    src, dst = order["currency"], order["customer_currency"]
    total = convert(subtotal + tax, src, dst)
    return {
        "currency": dst,
        "subtotal": convert(subtotal, src, dst).quantize(CENT, ROUND_HALF_EVEN),
        "tax": convert(tax, src, dst).quantize(CENT, ROUND_HALF_EVEN),
        "total": total.quantize(CENT, ROUND_HALF_EVEN),
    }
