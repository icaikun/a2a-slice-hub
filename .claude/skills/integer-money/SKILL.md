# Integer Money Skill

All monetary math MUST use exact Python `int` values in cents.

## Rules
- Never use `float`, `float(...)`, or decimal literals.
- Tax uses integer basis points: `tax_cents = (net_cents * tax_bps) // 10000`.
