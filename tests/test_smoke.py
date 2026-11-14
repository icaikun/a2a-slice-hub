import json
from pathlib import Path

def test_contracts_present():
    schema = json.loads(Path("contracts/order_summary.json").read_text(encoding="utf-8"))
    assert set(schema["required"]) == {"order_id", "subtotal_cents"}
