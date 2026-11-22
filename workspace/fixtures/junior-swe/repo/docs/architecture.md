# Architecture

One process, standard library only. A request names an order; `store` loads it, `totals` builds the
receipt, `api` writes it as JSON.

Money is `Decimal` throughout. Conversion uses the day's rates from `currency`. The product spec
("Receipts: totals and rounding", in the team's shared drive) fixes the rounding rule: one rounding
per figure, at the end, half to even.
