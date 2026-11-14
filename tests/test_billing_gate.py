from billing.invoice import build_invoice

def test_build_invoice_standard():
    result = build_invoice({"order_id": "ORD-1", "subtotal_cents": 3000}, discount_cents=500, tax_bps=1000)
    assert result == {
        "order_id": "ORD-1",
        "subtotal_cents": 3000,
        "discount_cents": 500,
        "net_cents": 2500,
        "tax_cents": 250,
        "total_cents": 2750,
    }
