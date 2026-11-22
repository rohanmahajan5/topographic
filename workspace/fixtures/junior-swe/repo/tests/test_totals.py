from decimal import Decimal

from receipts.totals import receipt


def test_same_currency_total():
    o = {"currency": "EUR", "customer_currency": "EUR", "tax_rate": "0.20",
         "lines": [{"sku": "mug", "qty": 2, "price": Decimal("7.99")}, {"sku": "tee", "qty": 1, "price": Decimal("19.00")}]}
    assert receipt(o)["total"] == Decimal("41.98")


def test_converted_total_rounds_once(order):
    # RCP-142: rounding each line before converting drifted the total by a cent
    r = receipt(order)
    assert r["currency"] == "GBP"
    assert r["total"] == Decimal("35.78")


def test_half_to_even():
    o = {"currency": "EUR", "customer_currency": "EUR", "tax_rate": "0",
         "lines": [{"sku": "x", "qty": 1, "price": Decimal("0.125")}]}
    assert receipt(o)["total"] == Decimal("0.12")
