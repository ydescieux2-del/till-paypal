"""Two SANDBOX orders paid from the sandbox buyer's PayPal wallet, so the buyer can open real disputes.

    .venv/bin/python tools/dispute_orders.py create     # prints two approve links; log in as the sandbox BUYER
    .venv/bin/python tools/dispute_orders.py capture [--wait]   # captures once approved (--wait polls up to 3 hours)

Card orders from seed_paypal_sandbox.py can't be disputed (no buyer account), hence these. Order ids are kept in
data/dispute_orders.json (gitignored). Sandbox only.
"""
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_paypal_sandbox import BASE, ROOT, token  # noqa: E402  (loads .env, refuses live)
import httpx  # noqa: E402

LOG = ROOT / "data" / "dispute_orders.json"
ORDERS = [("Celebration cake, Saturday pickup", "42.50"), ("Catering tray, office lunch", "186.00")]


def create(h):
    out = []
    for item, amt in ORDERS:
        body = {"intent": "CAPTURE",
                "purchase_units": [{"amount": {"currency_code": "USD", "value": amt}, "description": item}],
                "payment_source": {"paypal": {"experience_context": {
                    "brand_name": "Sunrise Bakery", "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING",
                    "return_url": "https://example.com/paid", "cancel_url": "https://example.com/cancel"}}}}
        r = httpx.post(f"{BASE}/v2/checkout/orders", json=body, timeout=60,
                       headers={**h, "PayPal-Request-Id": str(uuid.uuid4())})
        r.raise_for_status()
        o = r.json()
        link = next(lk["href"] for lk in o["links"] if lk["rel"] in ("payer-action", "approve"))
        out.append({"order_id": o["id"], "item": item, "amount": amt, "approve": link})
        print(f"${amt}  {item}\n  {link}")
    LOG.write_text(json.dumps(out, indent=1))


def capture(h, wait=False):
    """With --wait: poll until the buyer approves each order (up to 3 hours), then capture it."""
    for o in json.loads(LOG.read_text()):
        for _ in range(360 if wait else 1):
            st = httpx.get(f"{BASE}/v2/checkout/orders/{o['order_id']}", headers=h, timeout=60).json().get("status")
            if st != "PAYER_ACTION_REQUIRED" and st != "CREATED":
                break
            if wait:
                time.sleep(30)
        if st == "COMPLETED":
            print(f"${o['amount']}  {o['item']}: already captured")
            continue
        r = httpx.post(f"{BASE}/v2/checkout/orders/{o['order_id']}/capture", json={}, timeout=60,
                       headers={**h, "PayPal-Request-Id": str(uuid.uuid4())})
        status = r.json().get("status") if r.status_code < 300 else f"{r.status_code} {r.text[:160]}"
        print(f"${o['amount']}  {o['item']}: {status}")


if __name__ == "__main__":
    h = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
    if sys.argv[1] == "create":
        create(h)
    else:
        capture(h, wait="--wait" in sys.argv)
