"""Till's PayPal tools. Reads are free; every write goes through preview -> confirm.

A preview tool returns a one-time confirm_token (10 minutes, single use) with a plain description of exactly
what will happen and its effect on the cash forecast. `confirm_action` is the ONLY path to a PayPal write,
and it refuses anything without a live token. The agent proposes, the owner confirms, the agent executes.
"""
from __future__ import annotations

import functools
import secrets
import time
from datetime import date

from mcp_types import CallToolResult, TextContent

from . import engine
from .paypal import PayPal, PayPalError, fee_watch, refund_radar, to_rows, unpaid_invoices

PENDING: dict[str, dict] = {}
TOKEN_TTL = 600
_pp = PayPal()
_cache: dict = {}


def _err(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=msg)], is_error=True)


def _data(max_age: float = 300) -> dict:
    """One PayPal pull, cached briefly so a chained conversation doesn't re-hit the API."""
    if _cache.get("at", 0) > time.time() - max_age:
        return _cache
    rows = to_rows(_pp.transactions())
    bal = _pp.balance()
    _cache.update(at=time.time(), rows=rows, balance=bal, opening=round(bal - sum(r["amount"] for r in rows), 2),
                  disputes=_pp.disputes(), invoices=_pp.invoices())
    return _cache


def _today(d: dict) -> date:
    return engine.as_of(d["rows"]) if d["rows"] else date.today()


def _token(kind: str, args: dict, summary: str) -> str:
    for t in [t for t, p in PENDING.items() if p["expires"] < time.time()]:
        PENDING.pop(t, None)
    tok = secrets.token_hex(3)
    PENDING[tok] = {"kind": kind, "args": args, "summary": summary, "expires": time.time() + TOKEN_TTL}
    return tok


def register(apps, card_uri: str, result, money, speak_money, spoken_date):
    """Attach the PayPal tools to Till's MCP Apps extension (called before the server is built)."""

    def guard(fn):
        @functools.wraps(fn)
        def wrapped(*a, **k):
            if not _pp.configured():
                return _err("PayPal isn't connected. Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (sandbox), "
                            "or PAYPAL_FIXTURES for the demo data.")
            try:
                return fn(*a, **k)
            except PayPalError as exc:
                return _err(str(exc))
        return wrapped

    @apps.tool(resource_uri=card_uri, title="PayPal cash brief",
               description="The merchant's PayPal check-in: balance and money on hold, last 7 days of sales, fees and "
                           "refunds, disputes with response deadlines, and overdue invoices. Use for 'how's PayPal "
                           "looking', 'anything I need to handle'.")
    @guard
    def paypal_cash_brief() -> CallToolResult:
        d = _data()
        today = _today(d)
        rows = d["rows"]
        recent = [r for r in rows if (today - date.fromisoformat(r["date"])).days < 7]
        sales = sum(r["amount"] for r in recent if r["category"] == "Sales")
        fees = -sum(r["amount"] for r in recent if r["category"] == "Fees")
        refunds = -sum(r["amount"] for r in recent if r["category"] == "Refunds")
        rr = refund_radar(rows, d["disputes"], today)
        inv = [i for i in unpaid_invoices(d["invoices"], today) if i["days_overdue"] > 0]
        parts = [f"PayPal balance is {speak_money(d['balance'])}. This week you sold {speak_money(sales)} and paid "
                 f"{speak_money(fees)} in fees" + (f", and refunded {speak_money(refunds)}." if refunds >= 1 else ".")]
        if rr["disputes"]:
            first = rr["disputes"][0]
            parts.append(f"{len(rr['disputes'])} open dispute{'s' if len(rr['disputes']) != 1 else ''}, "
                         f"{speak_money(rr['at_risk'])} at risk; the first needs a response in {first['days_left']} days.")
        if inv:
            parts.append(f"{len(inv)} invoice{'s are' if len(inv) != 1 else ' is'} overdue, {speak_money(sum(i['amount'] for i in inv))}.")
        if rr["spike"]:
            parts.append(f"Refunds are running at {rr['latest_rate']:.0f} percent of sales, up from about {max(1, round(rr['baseline_rate']))} percent.")
        card = {"kind": "paypal_brief", "title": "PayPal cash brief", "as_of": today.isoformat(),
                "big": money(d["balance"]), "big_label": "PayPal balance",
                "rows": [{"label": "Sales, last 7 days", "value": money(sales)},
                         {"label": "PayPal fees, last 7 days", "value": money(fees)},
                         {"label": "Refunds, last 7 days", "value": money(refunds), "tone": "bad" if rr["spike"] else None},
                         {"label": "Disputes at risk", "value": money(rr["at_risk"]), "tone": "warn" if rr["at_risk"] else None},
                         {"label": "Overdue invoices", "value": money(sum(i["amount"] for i in inv)), "tone": "warn" if inv else None}],
                "items": [{"id": x["id"], "title": f"Dispute {money(x['amount'])}: {x['reason']}",
                           "detail": f"Respond by {x['due']} ({x['days_left']} days).", "tone": "warn"} for x in rr["disputes"]]}
        return result(" ".join(parts), f"balance {d['balance']}; sales7 {sales:.2f}; fees7 {fees:.2f}; refunds7 {refunds:.2f}; "
                                       f"disputes {rr['disputes']}; overdue {inv}", card)

    @apps.tool(resource_uri=card_uri, title="PayPal fee watch",
               description="Effective PayPal fee rate by week against the merchant's own baseline; flags drift and "
                           "what it cost this week.")
    @guard
    def paypal_fee_watch() -> CallToolResult:
        fw = fee_watch(_data()["rows"])
        if not fw["latest"]:
            return result("Not enough PayPal sales yet to measure fees.", "no data", {"kind": "fees", "title": "Fee watch"})
        lt = fw["latest"]
        if fw["baseline"] is None:
            speech = (f"This week PayPal kept {lt['rate']:.1f} percent of your sales. I need about three weeks of sales "
                      "to know your normal rate, so I'll flag drift after that.")
            card = {"kind": "fees", "title": "PayPal fee rate", "big": f"{lt['rate']:.2f}%", "big_label": "this week (baseline building)",
                    "rows": [{"label": f"Week of {w['week']}", "value": f"{w['rate']:.2f}%  ({money(w['fees'])} on {money(w['sales'])})"}
                             for w in fw["weeks"]]}
            return result(speech, str(fw), card, {"fee_watch": fw})
        speech = (f"This week PayPal kept {lt['rate']:.1f} percent of your sales, against your usual {fw['baseline']:.1f}. "
                  + (f"That's about {speak_money(fw['extra_this_week'])} more than normal; check for more card or "
                     "international payments, or a pricing change." if fw["drift"] else "That's in line with normal."))
        card = {"kind": "fees", "title": "PayPal fee rate by week", "big": f"{lt['rate']:.2f}%",
                "big_tone": "bad" if fw["drift"] else "ok", "big_label": f"this week vs {fw['baseline']:.2f}% baseline",
                "chart": [w["rate"] for w in fw["weeks"][-12:]], "chart_floor": fw["baseline"],
                "rows": [{"label": f"Week of {w['week']}", "value": f"{w['rate']:.2f}%  ({money(w['fees'])} on {money(w['sales'])})"}
                         for w in fw["weeks"][-6:]]}
        return result(speech, str(fw), card, {"fee_watch": fw})

    @apps.tool(resource_uri=card_uri, title="Refund and dispute radar",
               description="Refund rate this week against the prior 8 weeks, repeat refunders (anonymized), and open "
                           "disputes with money at risk and response deadlines.")
    @guard
    def paypal_refund_radar() -> CallToolResult:
        d = _data()
        rr = refund_radar(d["rows"], d["disputes"], _today(d))
        speech = (f"Refunds are {rr['latest_rate']:.0f} percent of sales this week, against about {max(1, round(rr['baseline_rate']))} percent normally."
                  + (" That's a spike." if rr["spike"] else ""))
        if rr["repeat_refunders"]:
            speech += f" {len(rr['repeat_refunders'])} customers have asked for more than one refund."
        if rr["disputes"]:
            speech += f" {len(rr['disputes'])} disputes are open, {speak_money(rr['at_risk'])} at risk."
        card = {"kind": "refunds", "title": "Refund and dispute radar", "big": f"{rr['latest_rate']:.1f}%",
                "big_tone": "bad" if rr["spike"] else "ok", "big_label": f"refund rate this week (normal {rr['baseline_rate']:.1f}%)",
                "chart": [w["refund_rate"] for w in rr["weekly"][-12:]],
                "items": [{"id": x["id"], "title": f"Dispute {money(x['amount'])}, respond in {x['days_left']} days",
                           "detail": x["reason"], "tone": "warn"} for x in rr["disputes"]]
                         + [{"title": f"Repeat refunds: {c}", "detail": f"{n} refunds", "tone": "warn"} for c, n in rr["repeat_refunders"][:3]]}
        return result(speech, str(rr), card, {"radar": rr})

    @apps.tool(resource_uri=card_uri, title="Unpaid invoices",
               description="PayPal invoices that are sent but unpaid, most overdue first.")
    @guard
    def paypal_unpaid_invoices() -> CallToolResult:
        d = _data()
        inv = unpaid_invoices(d["invoices"], _today(d))
        over = [i for i in inv if i["days_overdue"] > 0]
        speech = (f"{len(inv)} invoices are unpaid, {speak_money(sum(i['amount'] for i in inv))} in total."
                  + (f" {len(over)} {'is' if len(over) == 1 else 'are'} overdue; the oldest, invoice {over[0]['number']} "
                     f"for {speak_money(over[0]['amount'])}, is {over[0]['days_overdue']} days late." if over else ""))
        card = {"kind": "invoices", "title": "Unpaid PayPal invoices", "big": money(sum(i["amount"] for i in inv)),
                "big_label": "waiting to be paid",
                "items": [{"id": i["id"], "title": f"{i['number']} · {money(i['amount'])}",
                           "detail": (f"{i['days_overdue']} days overdue" if i["days_overdue"] > 0 else f"due {i['due']}"),
                           "tone": "bad" if i["days_overdue"] > 0 else None} for i in inv]}
        return result(speech, str(inv), card, {"invoices": inv})

    # ---------------------------------------------------------------- previews (no money moves)

    @apps.tool(resource_uri=card_uri, title="Preview: send an invoice",
               description="Prepare a PayPal invoice and show what will be sent. Nothing is sent until the owner "
                           "confirms with confirm_action and the returned token.")
    @guard
    def preview_invoice(recipient_email: str, item: str, amount: float, due_days: int = 30) -> CallToolResult:
        if amount <= 0 or "@" not in recipient_email:
            return _err("Need a positive amount and a valid recipient email.")
        masked = recipient_email[0] + "***@" + recipient_email.split("@")[1]
        tok = _token("invoice", {"recipient_email": recipient_email, "item": item, "amount": amount, "due_days": due_days},
                     f"Send a {money(amount)} invoice for {item} to {masked}, due in {due_days} days")
        speech = (f"Ready to invoice {masked} {speak_money(amount)} for {item}, due in {due_days} days. "
                  "Say confirm to send it.")
        card = {"kind": "preview", "title": "Confirm: send invoice", "big": money(amount), "big_label": item,
                "rows": [{"label": "To", "value": masked}, {"label": "Due", "value": f"in {due_days} days"},
                         {"label": "Confirmation code", "value": tok}]}
        return result(speech, f"confirm_token={tok}", card, {"confirm_token": tok})

    @apps.tool(resource_uri=card_uri, title="Preview: refund a payment",
               description="Prepare a refund of a PayPal payment (transaction id from the brief or radar) and show the "
                           "cash impact. Nothing is refunded until the owner confirms with confirm_action.")
    @guard
    def preview_refund(transaction_id: str, amount: float | None = None) -> CallToolResult:
        d = _data()
        sale = next((r for r in d["rows"] if r.get("capture_id") == transaction_id and r["category"] == "Sales"), None)
        if not sale:
            return _err(f"No PayPal sale with id {transaction_id}.")
        amt = round(amount or sale["amount"], 2)
        if amt > sale["amount"]:
            return _err(f"Can't refund more than the {money(sale['amount'])} paid.")
        before = engine.forecast(d["rows"], d["opening"])["low_point"]["balance"]
        after = engine.forecast(d["rows"], d["opening"], extra_once=amt)["low_point"]["balance"]
        tok = _token("refund", {"transaction_id": transaction_id, "amount": amt},
                     f"Refund {money(amt)} of payment {transaction_id}")
        speech = (f"Ready to refund {speak_money(amt)} from the {spoken_date(sale['date'])} payment. Your lowest projected "
                  f"PayPal balance goes from {money(before)} to {money(after)}. Say confirm to refund.")
        card = {"kind": "preview", "title": "Confirm: refund", "big": money(amt), "big_tone": "warn",
                "big_label": f"payment {transaction_id}",
                "rows": [{"label": "Lowest balance now", "value": money(before)},
                         {"label": "Lowest balance after refund", "value": money(after)},
                         {"label": "Confirmation code", "value": tok}]}
        return result(speech, f"confirm_token={tok}", card, {"confirm_token": tok})

    @apps.tool(resource_uri=card_uri, title="Preview: settle a dispute",
               description="For an open dispute, compare the amount at stake with the time to fight it, and prepare "
                           "to accept the claim. Nothing happens until the owner confirms with confirm_action.")
    @guard
    def preview_accept_dispute(dispute_id: str) -> CallToolResult:
        d = _data()
        rr = refund_radar(d["rows"], d["disputes"], _today(d))
        x = next((x for x in rr["disputes"] if x["id"] == dispute_id), None)
        if not x:
            return _err(f"No open dispute {dispute_id}.")
        small = x["amount"] < 75
        tok = _token("accept_dispute", {"dispute_id": dispute_id}, f"Accept dispute {dispute_id} ({money(x['amount'])})")
        speech = (f"This dispute is {speak_money(x['amount'])}, {x['reason']}, due in {x['days_left']} days. "
                  + ("At that size, settling usually costs less than an hour of gathering evidence. "
                     if small else "That's large enough that it may be worth responding with evidence instead. ")
                  + "Say confirm to accept the claim and refund the buyer.")
        card = {"kind": "preview", "title": "Confirm: accept dispute", "big": money(x["amount"]), "big_tone": "warn",
                "big_label": x["reason"], "rows": [{"label": "Response due", "value": f"{x['due']} ({x['days_left']} days)"},
                                                   {"label": "Suggestion", "value": "settle" if small else "consider responding"},
                                                   {"label": "Confirmation code", "value": tok}]}
        return result(speech, f"confirm_token={tok}", card, {"confirm_token": tok})

    # ---------------------------------------------------------------- the only write path

    @apps.tool(resource_uri=card_uri, title="Confirm a PayPal action",
               description="Execute a previewed PayPal action. Requires the confirm_token from a preview tool AND the "
                           "owner's explicit spoken or typed confirmation in this conversation. Never call this "
                           "without the owner saying confirm.")
    @guard
    def confirm_action(confirm_token: str) -> CallToolResult:
        p = PENDING.pop(confirm_token.strip().lower(), None)
        if not p or p["expires"] < time.time():
            return _err("That confirmation code is missing or expired. Preview the action again.")
        a = p["args"]
        if p["kind"] == "invoice":
            out = _pp.create_and_send_invoice(a["recipient_email"], a["item"], a["amount"], a["due_days"])
            speech = f"Sent. Invoice {out.get('id')} for {speak_money(a['amount'])} is on its way."
        elif p["kind"] == "refund":
            out = _pp.refund_capture(a["transaction_id"], a["amount"])
            speech = f"Done. Refunded {speak_money(a['amount'])}; PayPal says {out.get('status', 'submitted').lower()}."
        elif p["kind"] == "accept_dispute":
            out = _pp.accept_dispute(a["dispute_id"], "Accepting the claim; refunding the buyer.")
            speech = "Done. I accepted the claim and PayPal will refund the buyer."
        else:
            return _err("Unknown action.")
        _cache.clear()
        card = {"kind": "done", "title": "Done", "big": "Confirmed", "big_tone": "ok", "big_label": p["summary"]}
        return result(speech, f"executed {p['kind']}: {out}", card, {"executed": p["kind"], "paypal": out})

    return [paypal_cash_brief, paypal_fee_watch, paypal_refund_radar, paypal_unpaid_invoices,
            preview_invoice, preview_refund, preview_accept_dispute, confirm_action]
