"""Orders from a JSON file: a stand-in for the orders database."""
import json
from decimal import Decimal
from pathlib import Path

DATA = Path(__file__).with_name("orders.json")


def load_order(order_id, path=DATA):
    orders = json.loads(Path(path).read_text())
    o = orders[order_id]
    o["lines"] = [dict(l, price=Decimal(l["price"])) for l in o["lines"]]
    return o
