"""Create a day's worth of SANDBOX activity for the demo bakery: card orders (captured), the odd refund.

    .venv/bin/python tools/seed_paypal_sandbox.py [orders]      # default: 4 to 9 orders

Sandbox only (refuses to run against live). Uses PayPal's published test card. Run once a day so Transaction
Search accumulates real history before the demo. Appends capture ids to data/sandbox_captures.json.
"""
import json
import os
import random
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
assert os.environ.get("PAYPAL_ENV", "sandbox") == "sandbox", "refusing to seed a live account"
BASE = "https://api-m.sandbox.paypal.com"
LOG = ROOT / "data" / "sandbox_captures.json"
ITEMS = [("Sourdough loaf", 9, 14), ("Pastry box", 24, 38), ("Celebration cake", 45, 95), ("Coffee and croissant", 8, 13),
         ("Catering tray", 120, 260), ("Cookie dozen", 18, 26)]


def token():
    r = httpx.post(f"{BASE}/v1/oauth2/token", auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
                   data={"grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def main(n):
    h = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
    log = json.loads(LOG.read_text()) if LOG.exists() else []
    made = 0
    for _ in range(n):
        name, lo, hi = random.choice(ITEMS)
        amt = f"{random.uniform(lo, hi):.2f}"
        body = {"intent": "CAPTURE",
                "purchase_units": [{"amount": {"currency_code": "USD", "value": amt}, "description": name}],
                "payment_source": {"card": {"number": "4111111111111111", "expiry": "2030-12", "security_code": "123",
                                            "name": "Sandbox Buyer",
                                            "billing_address": {"address_line_1": "1 Test St", "admin_area_2": "Los Angeles",
                                                                "admin_area_1": "CA", "postal_code": "90037", "country_code": "US"}}}}
        r = httpx.post(f"{BASE}/v2/checkout/orders", json=body, timeout=60,
                       headers={**h, "PayPal-Request-Id": str(uuid.uuid4())})
        if r.status_code >= 300:
            print("order failed", r.status_code, r.text[:300])
            continue
        o = r.json()
        caps = [c for pu in o.get("purchase_units", []) for c in pu.get("payments", {}).get("captures", [])]
        if caps:
            log.append({"capture_id": caps[0]["id"], "amount": float(amt), "item": name, "status": caps[0]["status"]})
            made += 1
    # roughly one refund every other day, against an older capture
    if random.random() < 0.5 and len(log) > 5:
        c = random.choice(log[:-3])
        if not c.get("refunded"):
            r = httpx.post(f"{BASE}/v2/payments/captures/{c['capture_id']}/refund", json={}, timeout=60,
                           headers={**h, "PayPal-Request-Id": str(uuid.uuid4())})
            if r.status_code < 300:
                c["refunded"] = True
                print("refunded", c["capture_id"])
    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text(json.dumps(log, indent=1))
    print(f"created {made} captured orders; {len(log)} total logged")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else random.randint(4, 9))
