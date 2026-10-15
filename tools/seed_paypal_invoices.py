"""One-off: create and send three SANDBOX invoices for the demo bakery (one past due, two upcoming).
Recipients are example.com addresses; sandbox never emails real people."""
import os, uuid
from datetime import date, timedelta
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parent.parent
for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
assert os.environ.get("PAYPAL_ENV", "sandbox") == "sandbox"
B = "https://api-m.sandbox.paypal.com"
tok = httpx.post(f"{B}/v1/oauth2/token", auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
                 data={"grant_type": "client_credentials"}).json()["access_token"]
H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "Prefer": "return=representation"}
today = date.today()
specs = [("events@example.org", "Corporate breakfast catering, 40 guests", 640.00, today - timedelta(days=45), today - timedelta(days=15)),
         ("office@example.net", "Weekly office pastry delivery, October", 485.00, today - timedelta(days=10), today + timedelta(days=5)),
         ("orders@example.com", "Wedding dessert table deposit", 310.00, today, today + timedelta(days=12))]
for email, item, amt, inv_date, due in specs:
    body = {"detail": {"currency_code": "USD", "invoice_date": inv_date.isoformat(),
                       "payment_term": {"due_date": due.isoformat()}, "note": "Thank you for choosing Sunrise Bakery."},
            "invoicer": {"business_name": "Sunrise Bakery (demo)"},
            "primary_recipients": [{"billing_info": {"email_address": email}}],
            "items": [{"name": item, "quantity": "1", "unit_amount": {"currency_code": "USD", "value": f"{amt:.2f}"}}]}
    r = httpx.post(f"{B}/v2/invoicing/invoices", json=body, headers={**H, "PayPal-Request-Id": str(uuid.uuid4())}, timeout=60)
    if r.status_code >= 300:
        print("create failed", r.status_code, r.text[:300]); continue
    inv_id = r.json().get("id") or r.json().get("href", "").rsplit("/", 1)[-1]
    s = httpx.post(f"{B}/v2/invoicing/invoices/{inv_id}/send", json={"send_to_recipient": True}, headers=H, timeout=60)
    print(inv_id, "sent" if s.status_code < 300 else f"send failed {s.status_code} {s.text[:200]}", amt, "due", due)
