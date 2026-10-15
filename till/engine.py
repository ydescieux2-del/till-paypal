"""Deterministic cash analysis over a list of transactions. Pure functions, stdlib only.

Every number Till speaks comes from here, never from a language model.
A transaction is a dict: date (YYYY-MM-DD), vendor, category, amount (+ in, - out), memo.
"""
from __future__ import annotations

import csv
import hashlib
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import median

DEMO_CSV = Path(__file__).resolve().parent.parent / "data" / "demo_bakery.csv"
DEMO_OPENING_BALANCE = 18_000.00


def load_csv(path: Path | str = DEMO_CSV) -> list[dict]:
    with open(path, newline="") as f:
        return parse_rows(csv.DictReader(f))


def parse_rows(rows) -> list[dict]:
    out = []
    for r in rows:
        out.append({"date": r["date"].strip(), "vendor": r["vendor"].strip(),
                    "category": (r.get("category") or "Uncategorized").strip(),
                    "amount": float(r["amount"]), "memo": (r.get("memo") or "").strip()})
    out.sort(key=lambda r: r["date"])
    return out


def _d(s: str) -> date:
    return date.fromisoformat(s)


def as_of(txns) -> date:
    return _d(txns[-1]["date"])


def balance(txns, opening: float) -> float:
    return round(opening + sum(t["amount"] for t in txns), 2)


def _window(txns, end: date, days: int):
    start = end - timedelta(days=days - 1)
    return [t for t in txns if start <= _d(t["date"]) <= end]


# ---------------------------------------------------------------- snapshot

def snapshot(txns, opening: float) -> dict:
    end = as_of(txns)
    last30 = _window(txns, end, 30)
    cash_in = sum(t["amount"] for t in last30 if t["amount"] > 0)
    cash_out = -sum(t["amount"] for t in last30 if t["amount"] < 0)
    net = cash_in - cash_out
    bal = balance(txns, opening)
    weekly_burn = -net * 7 / 30 if net < 0 else 0.0
    runway_weeks = round(bal / weekly_burn, 1) if weekly_burn > 0 else None
    return {"as_of": end.isoformat(), "balance": bal, "in_30d": round(cash_in, 2),
            "out_30d": round(cash_out, 2), "net_30d": round(net, 2),
            "weekly_burn": round(weekly_burn, 2), "runway_weeks": runway_weeks}


# ---------------------------------------------------------------- recurring + forecast

_CADENCES = [(7, "weekly"), (14, "biweekly"), (30, "monthly"), (91, "quarterly")]


def recurring_outflows(txns) -> list[dict]:
    """Vendors paid on a steady cadence, with their typical amount and next expected date."""
    by_vendor = defaultdict(list)
    for t in txns:
        if t["amount"] < 0:
            by_vendor[t["vendor"]].append(t)
    out = []
    for vendor, ts in by_vendor.items():
        if len(ts) < 2:
            continue
        dates = sorted({_d(t["date"]) for t in ts})
        if len(dates) < 2:
            continue
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        g = median(gaps)
        cadence = next(((days, name) for days, name in _CADENCES if abs(g - days) <= max(2, days * 0.15)), None)
        if not cadence:
            continue
        recent = [-t["amount"] for t in ts[-3:]]
        out.append({"vendor": vendor, "category": ts[-1]["category"], "cadence": cadence[1],
                    "every_days": cadence[0], "typical": round(median(recent), 2),
                    "last": dates[-1].isoformat()})
    return sorted(out, key=lambda r: -r["typical"])


def _step(d: date, every_days: int) -> date:
    """Next due date. Monthly and quarterly bills land on the same day of the month."""
    months = {30: 1, 91: 3}.get(every_days)
    if not months:
        return d + timedelta(days=every_days)
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue


def forecast(txns, opening: float, weeks: int = 13, extra_weekly: float = 0.0,
             extra_once: float = 0.0, once_week: int = 1, floor: float = 5_000.0,
             expected_inflows: list[tuple[str, float]] | None = None) -> dict:
    """13-week cash forecast: average weekly sales of the last 8 weeks, recurring bills on their
    cadence, and other spending at its recent weekly average."""
    end = as_of(txns)
    last56 = _window(txns, end, 56)
    weekly_in = sum(t["amount"] for t in last56 if t["amount"] > 0) / 8
    rec = recurring_outflows(txns)
    rec_vendors = {r["vendor"] for r in rec}
    other_out = -sum(t["amount"] for t in last56 if t["amount"] < 0 and t["vendor"] not in rec_vendors) / 8

    bal = balance(txns, opening)
    rows, low = [], None
    for w in range(1, weeks + 1):
        w_start, w_end = end + timedelta(days=7 * (w - 1) + 1), end + timedelta(days=7 * w)
        bills = 0.0
        for r in rec:
            nxt = _step(_d(r["last"]), r["every_days"])
            while nxt <= w_end:
                if nxt >= w_start:
                    bills += r["typical"]
                nxt = _step(nxt, r["every_days"])
        out = bills + other_out + extra_weekly + (extra_once if w == once_week else 0.0)
        # money already owed to the business (e.g. unpaid invoices), counted in the week it's due;
        # anything already past due is counted in week 2, not week 1, since it's late for a reason
        owed = sum(amt for due, amt in (expected_inflows or [])
                   if (w_start <= max(_d(due), end + timedelta(days=8)) <= w_end))
        bal = bal + weekly_in + owed - out
        row = {"week": w, "week_ending": w_end.isoformat(), "cash_in": round(weekly_in + owed, 2), "owed_in": round(owed, 2),
               "cash_out": round(out, 2), "balance": round(bal, 2)}
        rows.append(row)
        if low is None or bal < low["balance"]:
            low = row
    below = next((r for r in rows if r["balance"] < floor), None)
    return {"as_of": end.isoformat(), "weeks": rows, "low_point": low, "floor": floor,
            "first_week_below_floor": below, "ending_balance": rows[-1]["balance"]}


# ---------------------------------------------------------------- anomalies

def _aid(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:8]


def anomalies(txns, lookback_days: int = 45) -> list[dict]:
    """Charges worth a second look in the last `lookback_days`. Rule-based, so every flag has a
    plain reason and a dollar amount."""
    end = as_of(txns)
    since = end - timedelta(days=lookback_days)
    found = []
    out_tx = [t for t in txns if t["amount"] < 0]

    # 1. same vendor, same amount, within 7 days
    for i, a in enumerate(out_tx):
        for b in out_tx[i + 1:]:
            gap = (_d(b["date"]) - _d(a["date"])).days
            if gap > 7:
                break
            if (b["vendor"] == a["vendor"] and abs(b["amount"] - a["amount"]) < 0.01
                    and -a["amount"] >= 100 and _d(b["date"]) >= since):
                found.append({"id": _aid("dup", b["vendor"], b["date"], b["amount"]), "kind": "duplicate",
                              "vendor": b["vendor"], "date": b["date"], "amount": -b["amount"],
                              "at_risk": -b["amount"],
                              "reason": f"Paid {b['vendor']} the same ${-b['amount']:,.2f} twice, {gap} days apart."})

    # 2. recurring charge that jumped (any charge in the window vs that vendor's usual amount)
    by_vendor = defaultdict(list)
    for t in out_tx:
        by_vendor[t["vendor"]].append(t)
    for vendor, ts in by_vendor.items():
        if len(ts) < 4:
            continue
        hits = []
        for i in range(3, len(ts)):
            t = ts[i]
            window = [-p["amount"] for p in ts[max(0, i - 6):i]]
            prior = median(window)
            steady = (max(window) - min(window)) <= prior * 0.35   # irregular spend is not a "bill"
            now = -t["amount"]
            if steady and _d(t["date"]) >= since and now > prior * 1.4 and now - prior >= 50:
                hits.append((t, prior, now))
        if not hits:
            continue
        t, prior, now = hits[0]
        still = -ts[-1]["amount"] > prior * 1.4
        per_year = (now - prior) * 12 if still else None
        tail = (f"about ${per_year:,.0f} a year if it sticks." if still
                else "it was a one-time jump; the next bill was back to normal.")
        found.append({"id": _aid("jump", vendor, t["date"]), "kind": "price_jump", "vendor": vendor,
                      "date": t["date"], "amount": now, "at_risk": round(now - prior, 2), "ongoing": still,
                      "reason": f"{vendor} was usually ${prior:,.0f}; the {_d(t['date']):%b %-d} charge was "
                                f"${now:,.0f} (up {now / prior - 1:.0%}), {tail}"})

    # 3. first-time vendor, large amount
    seen = set()
    for t in txns:
        if t["amount"] < 0 and t["vendor"] not in seen and -t["amount"] >= 1_000 and _d(t["date"]) >= since:
            found.append({"id": _aid("new", t["vendor"], t["date"]), "kind": "new_vendor", "vendor": t["vendor"],
                          "date": t["date"], "amount": -t["amount"], "at_risk": 0.0,
                          "reason": f"First payment ever to {t['vendor']}: ${-t['amount']:,.2f}. "
                                    "Worth confirming it was expected."})
        seen.add(t["vendor"])

    found.sort(key=lambda a: (-a["at_risk"], a["date"]))
    return found


# ---------------------------------------------------------------- spending change

def _month_totals(txns, ym: str) -> dict:
    tot = defaultdict(float)
    for t in txns:
        if t["date"].startswith(ym) and t["amount"] < 0:
            tot[t["category"]] += -t["amount"]
    return tot


def spending_change(txns, month: str | None = None) -> dict:
    """Compare a full month's spending by category to the month before. Defaults to the latest
    complete month."""
    end = as_of(txns)
    if month is None:
        first = end.replace(day=1)
        month = (first - timedelta(days=1)).strftime("%Y-%m")
    y, m = map(int, month.split("-"))
    prev = date(y, m, 1) - timedelta(days=1)
    pm = prev.strftime("%Y-%m")
    cur, old = _month_totals(txns, month), _month_totals(txns, pm)
    rows = []
    for cat in sorted(set(cur) | set(old)):
        delta = cur.get(cat, 0) - old.get(cat, 0)
        rows.append({"category": cat, "this_month": round(cur.get(cat, 0), 2),
                     "last_month": round(old.get(cat, 0), 2), "change": round(delta, 2)})
    rows.sort(key=lambda r: -abs(r["change"]))
    return {"month": month, "previous": pm, "total_this": round(sum(cur.values()), 2),
            "total_last": round(sum(old.values()), 2), "drivers": rows[:5]}


# ---------------------------------------------------------------- affordability

def affordability(txns, opening: float, monthly_cost: float = 0.0, one_time_cost: float = 0.0,
                  start_week: int = 1, floor: float = 5_000.0) -> dict:
    base = forecast(txns, opening, floor=floor)
    new = forecast(txns, opening, extra_weekly=monthly_cost * 12 / 52, extra_once=one_time_cost,
                   once_week=start_week, floor=floor)
    low = new["low_point"]["balance"]
    if low >= floor * 2:
        verdict = "comfortable"
    elif low >= floor:
        verdict = "tight"
    else:
        verdict = "not yet"
    return {"verdict": verdict, "floor": floor, "low_before": base["low_point"]["balance"],
            "low_after": low, "low_week_after": new["low_point"]["week_ending"],
            "ending_before": base["ending_balance"], "ending_after": new["ending_balance"],
            "first_week_below_floor": new["first_week_below_floor"]}
