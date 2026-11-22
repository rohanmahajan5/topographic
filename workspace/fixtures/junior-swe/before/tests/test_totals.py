from decimal import Decimal

from receipts.totals import receipt


def test_same_currency_total():
    o = {"currency": "EUR", "customer_currency": "EUR", "tax_rate": "0.20",
         "lines": [{"sku": "mug", "qty": 2, "price": Decimal("7.99")}, {"sku": "tee", "qty": 1, "price": Decimal("19.00")}]}
    assert receipt(o)["total"] == Decimal("41.98")
