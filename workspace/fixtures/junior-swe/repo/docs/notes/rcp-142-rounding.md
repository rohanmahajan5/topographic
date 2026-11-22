# RCP-142: receipt totals a cent off for converted orders

**Cause.** `totals.receipt` rounded each line to cents in the order's currency, and `currency.convert`
rounded again after converting. Two roundings drifted a converted total by up to a cent (ord-1002:
35.77 GBP shown, 35.78 GBP by the spec).

**Fix.** Lines and tax are summed unrounded, converted, and rounded once, half to even (spec 3.2).
`convert` no longer rounds.

**Checked.** `test_converted_total_rounds_once` failed before the fix and passes after; the full suite passes.
The spec's acceptance criteria (3.4) were re-read after the pull request was opened.
