"""PayPal adapter: OAuth, Transaction Search, balances, invoices, disputes, refunds.

Talks to the PayPal REST API (sandbox by default) with httpx. Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET.
With PAYPAL_FIXTURES=<dir> it serves recorded JSON instead, so tests and judges can run Till without keys.

Everything here maps PayPal objects into Till's plain transaction rows, so the deterministic engine in
engine.py (forecast, anomalies, affordability) runs on PayPal data unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

SANDBOX = "https://api-m.sandbox.paypal.com"
LIVE = "https://api-m.paypal.com"


class PayPalError(RuntimeError):
    pass


class PayPal:
    def __init__(self):
        self.fixtures = os.environ.get("PAYPAL_FIXTURES")
        self.base = LIVE if os.environ.get("PAYPAL_ENV", "sandbox").lower() == "live" else SANDBOX
        self._token, self._expires = None, 0.0

    # ------------------------------------------------------------------ plumbing
    def configured(self) -> bool:
        return bool(self.fixtures or (os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET")))

    def _auth(self) -> dict:
        if self._token and time.time() < self._expires - 60:
            return {"Authorization": f"Bearer {self._token}"}
        cid, secret = os.environ.get("PAYPAL_CLIENT_ID"), os.environ.get("PAYPAL_CLIENT_SECRET")
        if not (cid and secret):
            raise PayPalError("PayPal keys are not set (PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET).")
        r = httpx.post(f"{self.base}/v1/oauth2/token", auth=(cid, secret),
                       data={"grant_type": "client_credentials"}, timeout=30)
        if r.status_code != 200:
            raise PayPalError(f"PayPal sign-in failed ({r.status_code}): {r.text[:200]}")
        body = r.json()
        self._token, self._expires = body["access_token"], time.time() + int(body.get("expires_in", 3000))
        self.scopes = body.get("scope", "")
        return {"Authorization": f"Bearer {self._token}"}

    def _fixture(self, name: str):
        p = Path(self.fixtures) / f"{name}.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def get(self, path: str, params: dict | None = None, fixture: str | None = None) -> dict:
        if self.fixtures:
            return self._fixture(fixture or path.strip("/").replace("/", "_"))
        r = httpx.get(self.base + path, params=params, headers=self._auth(), timeout=30)
        if r.status_code == 403 and "reporting/transactions" in path:
            raise PayPalError("Transaction Search is not enabled on this PayPal app. Turn on 'Transaction search' "
                              "under the app's Features in developer.paypal.com, then try again.")
        if r.status_code >= 400:
            raise PayPalError(f"PayPal {path} returned {r.status_code}: {r.text[:200]}")
        return r.json()

    def post(self, path: str, body: dict | None = None, fixture: str | None = None) -> dict:
        if self.fixtures:
            return self._fixture(fixture or "post_" + path.strip("/").replace("/", "_")) or {"status": "COMPLETED"}
        r = httpx.post(self.base + path, json=body or {}, timeout=30,
                       headers={**self._auth(), "Content-Type": "application/json", "Prefer": "return=representation"})
        if r.status_code >= 400:
            raise PayPalError(f"PayPal {path} returned {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    # ------------------------------------------------------------------ reads
    def transactions(self, days: int = 120) -> list[dict]:
        """Transaction Search in 31-day windows (the API maximum)."""
        if self.fixtures:
            return self._fixture("transactions").get("transaction_details", [])
        end = datetime.now(timezone.utc).replace(microsecond=0)
        start = end - timedelta(days=days)
        out = []
        while start < end:
            stop = min(start + timedelta(days=31), end)
            page = 1
            while True:
                body = self.get("/v1/reporting/transactions", {
                    "start_date": start.isoformat().replace("+00:00", "Z"),
                    "end_date": stop.isoformat().replace("+00:00", "Z"),
                    "fields": "all", "page_size": 500, "page": page})
                out += body.get("transaction_details", [])
                if page >= int(body.get("total_pages", 1) or 1):
                    break
                page += 1
            start = stop
        return out

    def balance(self) -> float:
        try:
            body = self.get("/v1/reporting/balances", {"currency_code": "USD"}, fixture="balances")
        except PayPalError:
            # balances scope not granted yet: use the running balance PayPal stamps on the latest transaction
            for d in sorted(self.transactions(days=31), key=lambda d: d.get("transaction_info", {}).get("transaction_initiation_date", ""), reverse=True):
                end = d.get("transaction_info", {}).get("ending_balance") or d.get("transaction_info", {}).get("available_balance")
                if end:
                    return float(end.get("value", 0))
            return 0.0
        for b in body.get("balances", []):
            if b.get("currency") == "USD" or b.get("primary"):
                return float(b.get("total_balance", {}).get("value", 0))
        return 0.0

    def invoices(self) -> list[dict]:
        return self.get("/v2/invoicing/invoices", {"page_size": 100, "total_required": "true"},
                        fixture="invoices").get("items", [])

    def disputes(self) -> list[dict]:
        return self.get("/v1/customer/disputes", {"page_size": 50}, fixture="disputes").get("items", [])

    # ------------------------------------------------------------------ writes (only ever called after confirmation)
    def create_and_send_invoice(self, recipient_email: str, item: str, amount: float, due_days: int = 14) -> dict:
        draft = self.post("/v2/invoicing/invoices", {
            "detail": {"currency_code": "USD", "payment_term": {"term_type": "NET_" + str(due_days if due_days in (10, 15, 30, 45, 60, 90) else 30)},
                       "note": "Thank you for your business."},
            "primary_recipients": [{"billing_info": {"email_address": recipient_email}}],
            "items": [{"name": item, "quantity": "1", "unit_amount": {"currency_code": "USD", "value": f"{amount:.2f}"}}],
        }, fixture="post_invoice")
        inv_id = draft.get("id") or draft.get("href", "").rsplit("/", 1)[-1]
        if inv_id and not self.fixtures:
            self.post(f"/v2/invoicing/invoices/{inv_id}/send", {"send_to_recipient": True})
        return {"id": inv_id, "status": "SENT"}

    def refund_capture(self, capture_id: str, amount: float | None = None) -> dict:
        body = {"amount": {"currency_code": "USD", "value": f"{amount:.2f}"}} if amount else {}
        return self.post(f"/v2/payments/captures/{capture_id}/refund", body, fixture="post_refund")

    def accept_dispute(self, dispute_id: str, note: str) -> dict:
        return self.post(f"/v1/customer/disputes/{dispute_id}/accept-claim", {"note": note}, fixture="post_accept")


# ---------------------------------------------------------------------- mapping into Till rows

def _hash(s: str) -> str:
    return "cust-" + hashlib.sha1((s or "unknown").lower().encode()).hexdigest()[:6]


def _kind(code: str) -> str:
    """Bucket PayPal transaction event codes (T-codes)."""
    if code.startswith("T11"):
        return "Refunds"
    if code.startswith("T12") or code in ("T1106",):
        return "Disputes and reversals"
    if code.startswith("T04"):
        return "Transfers to bank"
    if code.startswith(("T00", "T01", "T03")):
        return "Sales"
    return "Other PayPal"


def to_rows(details: list[dict]) -> list[dict]:
    """PayPal Transaction Search -> Till rows. Fees become their own rows so fee drift is visible.
    Customers are reduced to a short hash: no names or emails ever reach the engine, the model, or a card."""
    rows = []
    for d in details:
        ti = d.get("transaction_info", {})
        if ti.get("transaction_status") not in (None, "S", "P"):
            continue
        day = ti.get("transaction_initiation_date", "")[:10]
        if not day:
            continue
        amt = float(ti.get("transaction_amount", {}).get("value", 0))
        fee = float(ti.get("fee_amount", {}).get("value", 0) or 0)
        kind = _kind(ti.get("transaction_event_code", ""))
        who = _hash(d.get("payer_info", {}).get("email_address", ""))
        vendor = {"Sales": "PayPal sales", "Transfers to bank": "Transfer to bank"}.get(kind, f"{kind}: {who}")
        rows.append({"date": day, "vendor": vendor, "category": kind, "amount": round(amt, 2),
                     "memo": ti.get("transaction_id", ""), "customer": who,
                     "capture_id": ti.get("transaction_id", "")})
        if fee:
            rows.append({"date": day, "vendor": "PayPal fees", "category": "Fees", "amount": round(fee, 2),
                         "memo": ti.get("transaction_id", ""), "customer": who, "fee_of": amt})
    rows.sort(key=lambda r: r["date"])
    return rows


# ---------------------------------------------------------------------- PayPal-specific checks (deterministic)

def _week(d: str) -> str:
    dt = date.fromisoformat(d)
    return (dt - timedelta(days=dt.weekday())).isoformat()


def fee_watch(rows: list[dict], baseline_weeks: int = 8) -> dict:
    """Effective fee rate per week on sales; flags the latest full week if it drifts above baseline."""
    sales, fees = defaultdict(float), defaultdict(float)
    for r in rows:
        if r["category"] == "Sales" and r["amount"] > 0:
            sales[_week(r["date"])] += r["amount"]
        if r["category"] == "Fees" and r.get("fee_of", 0) > 0:
            fees[_week(r["date"])] += -r["amount"]
    weeks = sorted(w for w in sales if sales[w] > 0)
    rates = [{"week": w, "sales": round(sales[w], 2), "fees": round(fees[w], 2),
              "rate": round(fees[w] / sales[w] * 100, 2)} for w in weeks]
    if len(rates) < 3:
        return {"weeks": rates, "baseline": None, "latest": rates[-1] if rates else None, "drift": False}
    base = rates[-baseline_weeks - 1:-1]
    baseline = round(sum(r["fees"] for r in base) / max(1, sum(r["sales"] for r in base)) * 100, 2)
    latest = rates[-1]
    drift = latest["rate"] - baseline >= 0.4
    extra = round((latest["rate"] - baseline) / 100 * latest["sales"], 2) if drift else 0.0
    return {"weeks": rates, "baseline": baseline, "latest": latest, "drift": drift, "extra_this_week": extra}


def refund_radar(rows: list[dict], disputes: list[dict], today: date | None = None) -> dict:
    """Refund rate this week vs the prior 8, repeat refunders (hashed), and dispute money at risk with deadlines."""
    sales, refunds, refunders = defaultdict(float), defaultdict(float), defaultdict(int)
    for r in rows:
        w = _week(r["date"])
        if r["category"] == "Sales" and r["amount"] > 0:
            sales[w] += r["amount"]
        elif r["category"] == "Refunds":
            refunds[w] += -r["amount"]
            refunders[r["customer"]] += 1
    weeks = sorted(set(sales) | set(refunds))
    rate = lambda w: refunds[w] / sales[w] * 100 if sales[w] else 0.0
    latest = weeks[-1] if weeks else None
    prior = weeks[-9:-1]
    base = (sum(refunds[w] for w in prior) / max(1, sum(sales[w] for w in prior)) * 100) if prior else 0.0
    spike = bool(latest) and rate(latest) >= max(2 * base, base + 3) and refunds[latest] >= 50
    today = today or date.today()
    open_d = []
    for d in disputes:
        if d.get("status") in ("RESOLVED",):
            continue
        due = (d.get("seller_response_due_date") or "")[:10]
        open_d.append({"id": d.get("dispute_id"), "reason": (d.get("reason") or "").replace("_", " ").lower(),
                       "amount": float(d.get("dispute_amount", {}).get("value", 0)), "status": d.get("status"),
                       "due": due, "days_left": (date.fromisoformat(due) - today).days if due else None})
    open_d.sort(key=lambda x: (x["days_left"] is None, x["days_left"] if x["days_left"] is not None else 0))
    return {"weekly": [{"week": w, "refund_rate": round(rate(w), 2), "refunds": round(refunds[w], 2)} for w in weeks],
            "latest_rate": round(rate(latest), 2) if latest else 0.0, "baseline_rate": round(base, 2), "spike": spike,
            "repeat_refunders": sorted(([c, n] for c, n in refunders.items() if n >= 2), key=lambda x: -x[1]),
            "disputes": open_d, "at_risk": round(sum(d["amount"] for d in open_d), 2)}


def unpaid_invoices(invoices: list[dict], today: date | None = None) -> list[dict]:
    today = today or date.today()
    out = []
    for inv in invoices:
        if inv.get("status") not in ("SENT", "UNPAID", "PARTIALLY_PAID", "SCHEDULED"):
            continue
        det = inv.get("detail", {})
        due = (det.get("payment_term", {}) or {}).get("due_date") or det.get("invoice_date", "")
        amt = float((inv.get("due_amount") or inv.get("amount") or {}).get("value", 0))
        out.append({"id": inv.get("id"), "number": det.get("invoice_number"), "amount": amt, "due": due[:10],
                    "days_overdue": (today - date.fromisoformat(due[:10])).days if due else 0,
                    "customer": _hash(((inv.get("primary_recipients") or [{}])[0].get("billing_info") or {}).get("email_address", ""))})
    return sorted(out, key=lambda x: -x["days_overdue"])
