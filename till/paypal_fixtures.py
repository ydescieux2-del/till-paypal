"""Synthetic PayPal API responses for the fictional Sunrise Bakery's online and catering orders.

    python -m till.paypal_fixtures        # writes data/paypal_fixtures/*.json

Shaped like the real PayPal REST responses (Transaction Search, balances, invoicing, disputes) so the
adapter runs the same code path with or without sandbox keys. Planted for the demo: a refund spike in the
latest week, a fee-rate jump, two open disputes with response deadlines, and an overdue catering invoice.
All names and emails are fictional.
"""
import json
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "paypal_fixtures"
TODAY = date(2026, 10, 13)
CUSTOMERS = [f"buyer{n:02d}@example.com" for n in range(1, 41)]


def _ts(d: date, rng) -> str:
    return datetime(d.year, d.month, d.day, rng.randint(8, 20), rng.randint(0, 59), tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _txn(tid, d, code, amount, fee, email, rng, status="S"):
    info = {"transaction_id": tid, "transaction_event_code": code, "transaction_initiation_date": _ts(d, rng),
            "transaction_amount": {"currency_code": "USD", "value": f"{amount:.2f}"}, "transaction_status": status}
    if fee:
        info["fee_amount"] = {"currency_code": "USD", "value": f"{fee:.2f}"}
    return {"transaction_info": info, "payer_info": {"email_address": email}}


def generate():
    rng = random.Random(1015)
    details, n = [], 0
    d = TODAY - timedelta(days=119)
    last_week = TODAY - timedelta(days=6)                      # the rolling 7-day window ending TODAY
    while d <= TODAY:
        orders = rng.randint(3, 7) + (3 if d.weekday() in (4, 5) else 0)
        for _ in range(orders):
            n += 1
            amt = round(rng.uniform(18, 95), 2)
            rate = 0.0449 if d >= last_week else 0.0349           # planted: fee-rate jump this week
            fee = -round(amt * rate + 0.49, 2)
            details.append(_txn(f"9PX{n:07d}", d, "T0006", amt, fee, rng.choice(CUSTOMERS), rng))
        if d.day in (1, 15):                                      # catering invoice payments
            n += 1
            amt = round(rng.uniform(380, 720), 2)
            details.append(_txn(f"9PX{n:07d}", d, "T0007", amt, -round(amt * 0.0349 + 0.49, 2), rng.choice(CUSTOMERS), rng))
        refund_odds = 0.6 if d >= last_week else 0.06             # planted: refund spike this week
        if rng.random() < refund_odds:
            n += 1
            who = "buyer07@example.com" if d >= last_week else rng.choice(CUSTOMERS)
            details.append(_txn(f"9PX{n:07d}", d, "T1107", -round(rng.uniform(25, 80), 2), 0, who, rng))
        if d.weekday() == 0:                                      # weekly sweep to the bank
            n += 1
            details.append(_txn(f"9PX{n:07d}", d, "T0400", -round(rng.uniform(1400, 1900), 2), 0, "", rng))
        d += timedelta(days=1)

    balances = {"balances": [{"currency": "USD", "primary": True,
                              "total_balance": {"currency_code": "USD", "value": "3184.52"},
                              "available_balance": {"currency_code": "USD", "value": "2911.07"},
                              "withheld_balance": {"currency_code": "USD", "value": "273.45"}}],
                "as_of_time": f"{TODAY.isoformat()}T23:59:59Z"}

    def inv(i, status, amount, due, email):
        return {"id": f"INV2-TILL-{i:04d}", "status": status,
                "detail": {"invoice_number": f"SB-{1000 + i}", "invoice_date": (date.fromisoformat(due) - timedelta(days=30)).isoformat(),
                           "payment_term": {"due_date": due}, "currency_code": "USD"},
                "primary_recipients": [{"billing_info": {"email_address": email}}],
                "amount": {"currency_code": "USD", "value": f"{amount:.2f}"},
                "due_amount": {"currency_code": "USD", "value": f"{amount:.2f}"}}

    invoices = {"items": [inv(1, "SENT", 640.00, "2026-09-28", "events@example.org"),
                          inv(2, "SENT", 485.00, "2026-10-20", "office@example.net"),
                          inv(3, "UNPAID", 310.00, "2026-10-27", "orders@example.com"),
                          inv(4, "PAID", 520.00, "2026-10-01", "events@example.org")], "total_items": 4}

    disputes = {"items": [
        {"dispute_id": "PP-D-27801", "create_time": "2026-10-09T17:20:00Z", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
         "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_amount": {"currency_code": "USD", "value": "42.50"},
         "seller_response_due_date": "2026-10-19T17:20:00Z"},
        {"dispute_id": "PP-D-27844", "create_time": "2026-10-11T15:05:00Z", "reason": "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
         "status": "WAITING_FOR_SELLER_RESPONSE", "dispute_amount": {"currency_code": "USD", "value": "186.00"},
         "seller_response_due_date": "2026-10-21T15:05:00Z"}]}

    OUT.mkdir(parents=True, exist_ok=True)
    for name, body in [("transactions", {"transaction_details": details, "total_pages": 1}), ("balances", balances),
                       ("invoices", invoices), ("disputes", disputes),
                       ("post_invoice", {"id": "INV2-TILL-0005", "status": "DRAFT"}),
                       ("post_refund", {"id": "REF-TILL-0001", "status": "COMPLETED"}),
                       ("post_accept", {"links": [], "status": "RESOLVED"})]:
        (OUT / f"{name}.json").write_text(json.dumps(body, indent=1))
    print(f"{OUT}  {len(details)} transactions")


if __name__ == "__main__":
    generate()
