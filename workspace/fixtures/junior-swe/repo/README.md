# receipts-api

A small HTTP service that turns an order into a receipt: line items, tax, the total in the
customer's currency. Standard library only (Python 3.11+).

```sh
python -m receipts.api --port 8080      # GET /receipts/<order id>
python -m pytest -q
```

- `src/receipts/totals.py` adds up an order.
- `src/receipts/currency.py` converts between currencies with the day's rates.
- `src/receipts/store.py` reads orders from a JSON file (a stand-in for the orders database).
- `docs/` holds the design notes; `docs/notes/` one note per fixed ticket.

Fixture data: this repository is fake, made for the agent workspace's mock scenario.
