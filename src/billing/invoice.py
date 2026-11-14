"""Billing invoice calculation consuming order_summary."""


def build_invoice(order_summary: dict, discount_cents: int = 0, tax_bps: int = 0) -> dict:
    """Return {"order_id", "subtotal_cents", "discount_cents", "net_cents", "tax_cents", "total_cents"}."""
    subtotal_cents = int(order_summary["subtotal_cents"])
    discount_cents = int(discount_cents)
    tax_bps = int(tax_bps)

    net_cents = subtotal_cents - discount_cents
    tax_cents = (net_cents * tax_bps) // 10000
    total_cents = net_cents + tax_cents

    return {
        "order_id": order_summary["order_id"],
        "subtotal_cents": subtotal_cents,
        "discount_cents": discount_cents,
        "net_cents": net_cents,
        "tax_cents": tax_cents,
        "total_cents": total_cents,
    }
