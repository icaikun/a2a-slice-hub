"""Order subtotal calculation in exact integer cents."""


def compute_subtotal_cents(order_id: str, items: list[dict]) -> dict:
    """Return {"order_id": order_id, "subtotal_cents": total_int}."""
    return {
        "order_id": order_id,
        "subtotal_cents": sum(int(item["unit_cents"]) * int(item["qty"]) for item in items),
    }
