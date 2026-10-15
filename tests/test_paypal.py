"""PayPal tool checks against the recorded sandbox-shaped fixtures (no keys needed).

    python tests/test_paypal.py            # fixtures
    python tests/test_paypal.py --live     # also read-only checks against the real sandbox (.env keys)
"""
import asyncio
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LIVE = "--live" in sys.argv
if not LIVE:
    os.environ["PAYPAL_FIXTURES"] = str(ROOT / "data" / "paypal_fixtures")
os.environ["TILL_DB"] = str(Path(tempfile.mkdtemp()) / "t.db")
if LIVE:
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from till import paypal as p          # noqa: E402
from till import paypal_tools as pt   # noqa: E402
import till.server as srv             # noqa: E402


def call(name, **args):
    r = asyncio.run(srv.mcp.call_tool(name, args))
    return r


def text(r):
    return r.content[0].text


def test_mapping_hides_customers():
    rows = p.to_rows(p.PayPal().transactions())
    assert rows and all("@" not in r["vendor"] and "@" not in r.get("customer", "") for r in rows)
    assert any(r["category"] == "Fees" for r in rows)


def test_fixture_findings():
    if LIVE:
        return
    pp = p.PayPal()
    rows = p.to_rows(pp.transactions())
    fw = p.fee_watch(rows)
    assert fw["drift"] and fw["latest"]["rate"] > fw["baseline"]
    rr = p.refund_radar(rows, pp.disputes(), today=date(2026, 10, 13))
    assert rr["spike"] and len(rr["disputes"]) == 2 and rr["at_risk"] == 228.5
    inv = p.unpaid_invoices(pp.invoices(), today=date(2026, 10, 13))
    assert len(inv) == 3 and inv[0]["days_overdue"] == 15


def test_read_tools_speak():
    for name in ("paypal_cash_brief", "paypal_forecast", "paypal_fee_watch", "paypal_refund_radar", "paypal_unpaid_invoices"):
        r = call(name)
        assert not getattr(r, "is_error", False), (name, text(r))
        assert "$" not in text(r).split(".")[0] or name == "paypal_refund_radar", (name, text(r))   # first sentence is for the ear


def test_confirm_gate():
    if LIVE:
        return   # never move money in the live check
    assert getattr(call("confirm_action", confirm_token="nope00"), "is_error", False)
    r = call("preview_invoice", recipient_email="orders@example.com", item="cake", amount=50)
    tok = r.structured_content["confirm_token"]
    assert "o***@example.com" in text(r)                              # recipient masked
    assert not getattr(call("confirm_action", confirm_token=tok), "is_error", False)
    assert getattr(call("confirm_action", confirm_token=tok), "is_error", False)   # single use
    assert getattr(call("preview_refund", transaction_id="9PX0000005", amount=99999), "is_error", False)  # can't over-refund


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("ALL PAYPAL CHECKS PASSED" + (" (live sandbox)" if LIVE else " (fixtures)"))
