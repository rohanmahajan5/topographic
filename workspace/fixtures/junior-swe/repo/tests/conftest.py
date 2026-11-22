from decimal import Decimal

import pytest


@pytest.fixture
def order():
    return {"currency": "EUR", "customer_currency": "GBP", "tax_rate": "0.20",
            "lines": [{"sku": "mug", "qty": 3, "price": Decimal("7.99")}, {"sku": "cap", "qty": 1, "price": Decimal("11.45")}]}
