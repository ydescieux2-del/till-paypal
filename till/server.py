"""Till: a voice CFO for small businesses, as an MCP server (spec 2025-11-25+, Streamable HTTP).

    python -m till.server            # http://127.0.0.1:8765/mcp  (demo page at /)

Every tool returns three things:
  - a short spoken line (first text block) a voice assistant can read as-is,
  - the supporting detail as text for the model,
  - structuredContent with a `card` that the MCP Apps UI (ui://till/card.html) renders.
"""
from __future__ import annotations

import csv
import io
import os
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx
from mcp.server.apps import Apps
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from . import engine, memory

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
CARD_URI = "ui://till/card.html"
DEFAULT_FLOOR = 5_000.0

apps = Apps()
apps.add_html_resource(CARD_URI, (WEB / "card.html").read_text(), name="Till card",
                       description="Renders any Till result as a card: big number, chart, and line items.",
                       prefers_border=True)


def _whole(a: float) -> int:
    """Round half up like the dashboard does: $228.50 -> 229 (Python's format would give 228)."""
    return int(Decimal(str(a)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _money(x: float) -> str:
    return f"-${_whole(-x):,}" if x < 0 else f"${_whole(x):,}"


def _speak_money(x: float) -> str:
    """Round for the ear: $16,717 -> 'about 16.7 thousand dollars'."""
    a = abs(x)
    s = f"{a / 1000:.1f}".removesuffix(".0") + " thousand dollars" if a >= 1000 else f"{_whole(a):,} dollars"
    return ("minus " if x < 0 else "") + s


def _books(business: str):
    saved = memory.books(business)
    if saved:
        return saved
    return engine.load_csv(), engine.DEMO_OPENING_BALANCE


def _floor(business: str) -> float:
    a = next((a for a in memory.alerts(business) if a["kind"] == "cash_below"), None)
    return a["threshold"] if a else DEFAULT_FLOOR


def _result(speech: str, detail: str, card: dict, data: dict | None = None) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=speech),
                                   TextContent(type="text", text=detail)],
                          structured_content={"speech": speech, "card": card, **(data or {})})


def _open_charges(business: str, txns) -> list[dict]:
    done = memory.reviews(business)
    return [a for a in engine.anomalies(txns) if a["id"] not in done]


# ----------------------------------------------------------------------------- tools

@apps.tool(resource_uri=CARD_URI, title="Morning cash brief",
           description="The daily money check-in. Combines cash on hand, what changed since the last brief, "
                       "the lowest point in the 13-week forecast, cash alerts, and charges that still need a "
                       "look. Use for 'how are we doing', 'morning brief', 'anything I should know'.")
def morning_brief(business: str = "demo") -> CallToolResult:
    txns, opening = _books(business)
    snap = engine.snapshot(txns, opening)
    floor = _floor(business)
    fc = engine.forecast(txns, opening, floor=floor)
    charges = _open_charges(business, txns)
    prev = memory.last_brief(business)
    low = fc["low_point"]

    parts = [f"You have {_speak_money(snap['balance'])} in the bank."]
    if prev:
        moved = snap["balance"] - prev["balance"]
        if abs(moved) >= 1:
            parts.append(f"That's {'up' if moved > 0 else 'down'} {_speak_money(abs(moved))} since your last brief.")
    parts.append(f"The tightest week ahead ends {_spoken_date(low['week_ending'])}, at {_speak_money(low['balance'])}.")
    below = fc["first_week_below_floor"]
    if below:
        parts.append(f"Heads up: that's under your {_speak_money(floor)} alert.")
    if charges:
        at_risk = sum(c["at_risk"] for c in charges)
        parts.append(f"{len(charges)} charge{'s' if len(charges) != 1 else ''} need a look"
                     + (f", about {_speak_money(at_risk)} in question." if at_risk >= 50 else "."))
    else:
        parts.append("No charges need a look.")
    speech = " ".join(parts)
    memory.record_brief(business, snap["balance"], low["balance"])

    card = {"kind": "brief", "title": "Morning cash brief", "as_of": snap["as_of"],
            "big": _money(snap["balance"]), "big_label": "cash on hand",
            "rows": [{"label": "Last 30 days in", "value": _money(snap["in_30d"])},
                     {"label": "Last 30 days out", "value": _money(snap["out_30d"])},
                     {"label": "Tightest week", "value": f"{_money(low['balance'])} (wk ending {low['week_ending']})",
                      "tone": "bad" if below else "ok"},
                     {"label": "Cash alert", "value": _money(floor)}],
            "chart": [w["balance"] for w in fc["weeks"]], "chart_floor": floor,
            "items": [{"id": c["id"], "title": c["vendor"], "detail": c["reason"], "tone": "warn"} for c in charges]}
    detail = (f"as_of {snap['as_of']}; balance {snap['balance']}; 13-week low {low['balance']} week ending "
              f"{low['week_ending']}; alert floor {floor}; open charges: "
              + ("; ".join(f"[{c['id']}] {c['reason']}" for c in charges) or "none")
              + (f"; previous brief balance {prev['balance']} at {prev['at']}" if prev else "; first brief"))
    return _result(speech, detail, card, {"snapshot": snap, "open_charges": charges})


@apps.tool(resource_uri=CARD_URI, title="Cash on hand",
           description="Current bank balance, last-30-day cash in and out, and weekly burn / runway.")
def cash_snapshot(business: str = "demo") -> CallToolResult:
    txns, opening = _books(business)
    s = engine.snapshot(txns, opening)
    run = (f"At last month's pace that's about {s['runway_weeks']:.0f} weeks of runway."
           if s["runway_weeks"] else "You took in more than you spent over the last 30 days.")
    speech = f"You have {_speak_money(s['balance'])}. {run}"
    card = {"kind": "snapshot", "title": "Cash on hand", "as_of": s["as_of"], "big": _money(s["balance"]),
            "big_label": "cash on hand",
            "rows": [{"label": "In, last 30 days", "value": _money(s["in_30d"])},
                     {"label": "Out, last 30 days", "value": _money(s["out_30d"])},
                     {"label": "Net", "value": _money(s["net_30d"]), "tone": "bad" if s["net_30d"] < 0 else "ok"},
                     {"label": "Runway", "value": f"{s['runway_weeks']:.0f} weeks" if s["runway_weeks"] else "cash-positive"}]}
    return _result(speech, str(s), card, {"snapshot": s})


@apps.tool(resource_uri=CARD_URI, title="Cash forecast",
           description="Week-by-week cash forecast (default 13 weeks) from recent sales and recurring bills. "
                       "Reports the lowest point and the first week below the user's cash alert.")
def cash_forecast(weeks: int = 13, business: str = "demo") -> CallToolResult:
    weeks = max(1, min(int(weeks), 26))
    txns, opening = _books(business)
    floor = _floor(business)
    fc = engine.forecast(txns, opening, weeks=weeks, floor=floor)
    low, below = fc["low_point"], fc["first_week_below_floor"]
    speech = (f"Over the next {weeks} weeks, cash bottoms out at {_speak_money(low['balance'])} "
              f"the week ending {_spoken_date(low['week_ending'])}, and ends near {_speak_money(fc['ending_balance'])}.")
    speech += (f" It drops under your {_speak_money(floor)} alert the week ending {_spoken_date(below['week_ending'])}."
               if below else f" It stays above your {_speak_money(floor)} alert the whole time.")
    card = {"kind": "forecast", "title": f"{weeks}-week cash forecast", "as_of": fc["as_of"],
            "big": _money(low["balance"]), "big_label": f"lowest point, week ending {low['week_ending']}",
            "chart": [w["balance"] for w in fc["weeks"]], "chart_floor": floor,
            "rows": [{"label": f"Wk {w['week']} ({w['week_ending'][5:]})", "value": _money(w["balance"]),
                      "tone": "bad" if w["balance"] < floor else None} for w in fc["weeks"]]}
    return _result(speech, str(fc), card, {"forecast": fc})


@apps.tool(resource_uri=CARD_URI, title="Unusual charges",
           description="Charges worth a second look: duplicate payments, bills that jumped, and large first-time "
                       "vendors. Charges already reviewed are hidden unless include_reviewed is true.")
def unusual_charges(include_reviewed: bool = False, business: str = "demo") -> CallToolResult:
    txns, _ = _books(business)
    done = memory.reviews(business)
    found = engine.anomalies(txns)
    shown = found if include_reviewed else [a for a in found if a["id"] not in done]
    if not shown:
        speech = "Nothing unusual right now." + (" Everything flagged has been reviewed." if found else "")
    else:
        top = shown[0]
        speech = (f"I found {len(shown)} charge{'s' if len(shown) != 1 else ''} worth a look. "
                  f"The biggest: {top['reason']}")
    card = {"kind": "charges", "title": "Charges worth a look",
            "big": str(len(shown)), "big_label": "need review",
            "items": [{"id": a["id"], "title": f"{a['vendor']} · {_money(a['amount'])} · {a['date']}",
                       "detail": a["reason"] + (f" (marked {done[a['id']]['verdict']})" if a["id"] in done else ""),
                       "tone": "ok" if a["id"] in done else "warn"} for a in shown]}
    detail = "; ".join(f"[{a['id']}] {a['kind']} {a['reason']}" for a in shown) or "none"
    return _result(speech, detail, card, {"charges": shown})


@apps.tool(resource_uri=CARD_URI, title="Review a charge",
           description="Record the owner's decision on a flagged charge so Till stops raising it. "
                       "verdict is one of: expected, dispute, cancel. Remembered across conversations.")
def review_charge(charge_id: str, verdict: str, note: str = "", business: str = "demo") -> CallToolResult:
    verdict = verdict.strip().lower()
    if verdict not in ("expected", "dispute", "cancel"):
        return CallToolResult(content=[TextContent(type="text", text="verdict must be expected, dispute, or cancel")],
                              is_error=True)
    txns, _ = _books(business)
    match = next((a for a in engine.anomalies(txns) if a["id"] == charge_id), None)
    if not match:
        return CallToolResult(content=[TextContent(type="text", text=f"No flagged charge with id {charge_id}. "
                                                   "Call unusual_charges to get current ids.")], is_error=True)
    memory.review(business, charge_id, verdict, note)
    action = {"expected": f"Got it. {match['vendor']} was expected, so I'll stop flagging it.",
              "dispute": f"Got it. I've noted the {match['vendor']} charge as a dispute. "
                         f"Call them or your bank about {_speak_money(match['amount'])}.",
              "cancel": f"Got it. Cancel {match['vendor']} before the next bill and I'll watch for it."}[verdict]
    left = len(_open_charges(business, txns))
    speech = f"{action} {left or 'No'} charge{'s' if left != 1 else ''} left to review."
    card = {"kind": "review", "title": "Charge reviewed", "big": verdict.title(), "big_label": match["vendor"],
            "items": [{"id": charge_id, "title": match["vendor"], "detail": match["reason"], "tone": "ok"}],
            "rows": [{"label": "Still to review", "value": str(left)}]}
    return _result(speech, f"recorded {charge_id} as {verdict}", card)


@apps.tool(resource_uri=CARD_URI, title="Can I afford it?",
           description="Test a new cost against the 13-week forecast: a monthly cost (a hire, a lease, a "
                       "subscription) and/or a one-time cost in a given week. Answers comfortable / tight / not yet.")
def can_i_afford(item: str, monthly_cost: float = 0.0, one_time_cost: float = 0.0, start_week: int = 1,
                 business: str = "demo") -> CallToolResult:
    txns, opening = _books(business)
    floor = _floor(business)
    r = engine.affordability(txns, opening, monthly_cost=monthly_cost, one_time_cost=one_time_cost,
                             start_week=max(1, min(int(start_week), 13)), floor=floor)
    cost = " and ".join(x for x in [f"{_speak_money(monthly_cost)} a month" if monthly_cost else "",
                                    f"{_speak_money(one_time_cost)} up front" if one_time_cost else ""] if x)
    lead = {"comfortable": "Yes, comfortably.", "tight": "Yes, but it gets tight.", "not yet": "Not yet."}[r["verdict"]]
    speech = (f"{lead} With {item} at {cost}, your lowest cash point goes from {_speak_money(r['low_before'])} "
              f"to {_speak_money(r['low_after'])}.")
    if r["first_week_below_floor"]:
        speech += f" You'd dip under your {_speak_money(floor)} alert the week ending {_spoken_date(r['first_week_below_floor']['week_ending'])}."
    tone = {"comfortable": "ok", "tight": "warn", "not yet": "bad"}[r["verdict"]]
    card = {"kind": "afford", "title": f"Can I afford {item}?", "big": lead.rstrip("."), "big_tone": tone,
            "big_label": cost,
            "rows": [{"label": "Lowest cash, today's plan", "value": _money(r["low_before"])},
                     {"label": f"Lowest cash, with {item}", "value": _money(r["low_after"]), "tone": tone},
                     {"label": "Cash in 13 weeks, today's plan", "value": _money(r["ending_before"])},
                     {"label": f"Cash in 13 weeks, with {item}", "value": _money(r["ending_after"])},
                     {"label": "Your cash alert", "value": _money(floor)}]}
    return _result(speech, str(r), card, {"affordability": r})


@apps.tool(resource_uri=CARD_URI, title="What changed in spending",
           description="Compare one month's spending by category to the month before and name the biggest "
                       "drivers. month is YYYY-MM; defaults to the last full month.")
def spending_change(month: str | None = None, business: str = "demo") -> CallToolResult:
    txns, _ = _books(business)
    r = engine.spending_change(txns, month)
    diff = r["total_this"] - r["total_last"]
    top = [d for d in r["drivers"] if abs(d["change"]) >= 1][:2]
    speech = (f"Spending was {_speak_money(r['total_this'])} in {_month_name(r['month'])}, "
              f"{'up' if diff >= 0 else 'down'} {_speak_money(abs(diff))} from {_month_name(r['previous'])}.")
    if top:
        speech += " Biggest changes: " + " and ".join(
            f"{d['category'].lower()} {'up' if d['change'] > 0 else 'down'} {_speak_money(abs(d['change']))}" for d in top) + "."
    card = {"kind": "change", "title": f"Spending, {_month_name(r['month'])} vs {_month_name(r['previous'])}",
            "big": _money(diff), "big_tone": "bad" if diff > 0 else "ok", "big_label": "change in spending",
            "rows": [{"label": d["category"], "value": f"{_money(d['this_month'])}  ({'+' if d['change'] >= 0 else ''}{_money(d['change'])})",
                      "tone": "bad" if d["change"] > 0 else "ok"} for d in r["drivers"]]}
    return _result(speech, str(r), card, {"change": r})


@apps.tool(resource_uri=CARD_URI, title="Set a cash alert",
           description="Remember a minimum cash level. Forecasts, briefs, and affordability checks warn when "
                       "cash is projected to drop under it. Persists across conversations.")
def set_cash_alert(threshold: float, business: str = "demo") -> CallToolResult:
    memory.set_alert(business, "cash_below", float(threshold))
    txns, opening = _books(business)
    fc = engine.forecast(txns, opening, floor=float(threshold))
    below = fc["first_week_below_floor"]
    speech = f"Done. I'll warn you if cash is headed under {_speak_money(threshold)}."
    speech += (f" Right now it's projected to dip under that the week ending {_spoken_date(below['week_ending'])}."
               if below else " Right now the forecast stays above it.")
    card = {"kind": "alert", "title": "Cash alert saved", "big": _money(threshold), "big_label": "warn me below",
            "chart": [w["balance"] for w in fc["weeks"]], "chart_floor": float(threshold)}
    return _result(speech, f"alert cash_below={threshold}", card)


@apps.tool(resource_uri=CARD_URI, title="Import books",
           description="Load the user's own transactions as CSV text with columns date (YYYY-MM-DD), vendor, "
                       "category, amount (positive in, negative out), memo; plus the opening balance before the "
                       "first row. Replaces the demo bakery data for this business id.")
def import_books(csv_text: str, opening_balance: float, business: str) -> CallToolResult:
    if business == "demo":
        return CallToolResult(content=[TextContent(type="text", text="Pick a business id other than 'demo'.")],
                              is_error=True)
    try:
        rows = engine.parse_rows(csv.DictReader(io.StringIO(csv_text)))
    except (KeyError, ValueError) as exc:
        return CallToolResult(content=[TextContent(type="text", text=f"Could not read the CSV: {exc}")], is_error=True)
    if not rows:
        return CallToolResult(content=[TextContent(type="text", text="The CSV had no rows.")], is_error=True)
    memory.save_books(business, rows, float(opening_balance))
    s = engine.snapshot(rows, float(opening_balance))
    speech = f"Loaded {len(rows)} transactions. You have {_speak_money(s['balance'])} as of {_spoken_date(s['as_of'])}."
    card = {"kind": "import", "title": "Books loaded", "big": str(len(rows)), "big_label": "transactions",
            "rows": [{"label": "From", "value": rows[0]["date"]}, {"label": "To", "value": rows[-1]["date"]},
                     {"label": "Cash on hand", "value": _money(s["balance"])}]}
    return _result(speech, str(s), card)


@apps.tool(resource_uri=CARD_URI, title="Start over",
           description="Forget everything remembered for a business: alerts, reviewed charges, brief history, "
                       "and imported books. Only when the user explicitly asks to reset.")
def forget_business(business: str = "demo") -> CallToolResult:
    memory.forget(business)
    return _result("Done. I've cleared what I remembered for this business.", f"cleared {business}",
                   {"kind": "reset", "title": "Memory cleared", "big": "Fresh start", "big_label": business})


def _spoken_date(iso: str) -> str:
    from datetime import date
    d = date.fromisoformat(iso)
    return f"{d:%B} {d.day}"


def _month_name(ym: str) -> str:
    from datetime import date
    return f"{date.fromisoformat(ym + '-01'):%B}"


# ----------------------------------------------------------------------------- server + demo routes

from . import paypal_tools  # noqa: E402  (PayPal tools join the same MCP Apps extension)
paypal_tools.register(apps, CARD_URI, _result, _money, _speak_money, _spoken_date)


mcp = MCPServer("till", title="Till", version="0.1.0",
                description="A voice CFO for small businesses: cash, forecast, unusual charges, affordability.",
                instructions="Till answers money questions for a small business owner by voice. Read the first "
                             "text block of a tool result aloud as-is; it is already written for the ear. Use "
                             "morning_brief for open-ended check-ins. When the owner decides about a flagged "
                             "charge, call review_charge with its id. Numbers come only from tools; never "
                             "estimate. Till is not a licensed advisor and does not give investment or tax advice.",
                extensions=[apps])


@mcp.custom_route("/", methods=["GET"])
async def demo_page(request: Request) -> Response:
    return FileResponse(WEB / "index.html")


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard_page(request: Request) -> Response:
    return FileResponse(WEB / "dashboard.html")


@mcp.custom_route("/api/paypal/dashboard", methods=["GET"])
async def dashboard_api(request: Request) -> Response:
    import anyio
    try:
        return JSONResponse(await anyio.to_thread.run_sync(paypal_tools.dashboard_data))
    except Exception as exc:   # surface PayPal errors to the page instead of a blank screen
        return JSONResponse({"error": str(exc)}, 502)


@mcp.custom_route("/agent/status", methods=["GET"])
async def agent_status(request: Request) -> Response:
    return JSONResponse({"llm": bool(os.environ.get("ANTHROPIC_API_KEY")), "model": _agent_model(),
                         "tts": bool(os.environ.get("ELEVENLABS_API_KEY"))})


@mcp.custom_route("/agent/tts", methods=["POST"])
async def agent_tts(request: Request) -> Response:
    """Speak Till's answer in an ElevenLabs voice. The key stays on the server; without one the page
    falls back to the browser's built-in voice."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return JSONResponse({"error": "ELEVENLABS_API_KEY not set"}, 503)
    text = str((await request.json()).get("text", ""))[:1200]
    voice = os.environ.get("TILL_VOICE_ID", "cjVigY5qzO86Huf0OWal")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
                              json={"text": text, "model_id": os.environ.get("TILL_TTS_MODEL", "eleven_flash_v2_5")},
                              headers={"xi-api-key": key})
    if r.status_code != 200:
        return JSONResponse({"error": r.text[:300]}, r.status_code)
    return Response(r.content, media_type="audio/mpeg")


def _agent_model() -> str:
    return os.environ.get("TILL_AGENT_MODEL", "claude-haiku-5-5")


@mcp.custom_route("/agent/llm", methods=["POST"])
async def agent_llm(request: Request) -> Response:
    """Thin pass-through so the browser's agent loop can reach Claude without holding the key.
    The browser stays the MCP client: it runs every tool call itself over /mcp."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return JSONResponse({"error": "ANTHROPIC_API_KEY not set; the page falls back to its offline router"}, 503)
    body = await request.json()
    payload = {"model": _agent_model(), "max_tokens": 1024, "system": str(body.get("system", ""))[:8000],
               "messages": body.get("messages", [])[-24:], "tools": body.get("tools", []),
               "cache_control": {"type": "ephemeral"}}   # the system prompt and tools repeat on every hop
    last = payload["messages"][-1:] or [{}]
    if isinstance(last[0].get("content"), list) and any(b.get("type") == "tool_result" for b in last[0]["content"]):
        # After a tool result the model mostly phrases the answer; thinking there added seconds to every answer.
        payload["thinking"] = {"type": "disabled"}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post("https://api.anthropic.com/v1/messages", json=payload,
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    return JSONResponse(r.json(), status_code=r.status_code)


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def main() -> None:
    _load_dotenv()
    port = int(os.environ.get("PORT", "8765"))
    print(f"Till MCP server  http://127.0.0.1:{port}/mcp   demo page  http://127.0.0.1:{port}/")
    mcp.run("streamable-http", host="127.0.0.1", port=port, json_response=True)


if __name__ == "__main__":
    main()
