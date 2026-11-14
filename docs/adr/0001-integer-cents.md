---
id: '0001'
status: accepted
scope:
  slices:
    - orders
    - billing
---
# Use Integer Cents for All Monetary Calculations

## Decision
All monetary values must use integer cents (`int`). Floating-point arithmetic and `float` literals or calls are strictly forbidden.

## Consequences
Order subtotals, discounts, basis-point tax (`(net_cents * tax_bps) // 10000`), and invoice totals are exact integers.
