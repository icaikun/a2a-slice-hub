from orders.calculator import compute_subtotal_cents

def test_compute_order_subtotal():
    result = compute_subtotal_cents("ORD-1", [{"sku": "A", "unit_cents": 1250, "qty": 2}, {"sku": "B", "unit_cents": 500, "qty": 1}])
    assert result == {"order_id": "ORD-1", "subtotal_cents": 3000}
