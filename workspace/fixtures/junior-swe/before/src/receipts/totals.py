"""An order's receipt: subtotal, tax and total in the customer's currency."""
from decimal import Decimal, ROUND_HALF_UP

from .currency import convert

CENT = Decimal("0.01")


def receipt(order):
    lines = [(l["price"] * l["qty"]).quantize(CENT, ROUND_HALF_UP) for l in order["lines"]]
    subtotal = sum(lines, Decimal(0))
    tax = (subtotal * Decimal(order["tax_rate"])).quantize(CENT, ROUND_HALF_UP)
    src, dst = order["currency"], order["customer_currency"]
    return {
        "currency": dst,
        "subtotal": convert(subtotal, src, dst),
        "tax": convert(tax, src, dst),
        "total": convert(subtotal + tax, src, dst),
    }
