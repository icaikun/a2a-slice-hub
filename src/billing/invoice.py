"""Billing invoice calculation consuming order_summary."""


def build_invoice(order_summary: dict, discount_cents: int = 0, tax_bps: int = 0) -> dict:
    """Return {"order_id", "subtotal_cents", "discount_cents", "net_cents", "tax_cents", "total_cents"}."""
    raise NotImplementedError
