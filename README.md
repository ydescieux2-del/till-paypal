# Till: a voice CFO for small businesses

Ask about your money out loud. Till answers in one or two spoken sentences and shows a card.

> "Morning. Anything I should worry about?"
> "Mostly fine: you have 16.7 thousand in the bank, and the tightest point, November 3, still sits at 11.2 thousand. Four charges need a look, led by a duplicate $913 flour payment. Want me to go through them?"

Till is a self-hosted **MCP server** (spec **2025-11-25**, **Streamable HTTP**) built for the Alexa+ track. It gives a
voice assistant ten tools for the money questions a small business owner asks every week. It also has a
**web demo client**: a real MCP client with a microphone, speech output, and an **MCP Apps** card host.

Small businesses don't have a CFO. They have a bank app, a spreadsheet, and a 10 pm worry about payroll. The
questions are simple: how much do I have, will I be short, did I get charged twice, can I afford to hire? But the
answers take an hour of reconciling. Till answers them in seconds, by voice. It remembers what you already
decided, so the next morning's brief only tells you what's new.

## New: Till for PayPal merchants (PayPal AI Hackathon, built Oct 15 to Nov 2026)

Everything below was added during the hackathon window on top of the open-source Till core (first commit 2026-10-14).

- **Reads a merchant's PayPal account** (sandbox): Transaction Search, balances, invoices, and disputes, mapped into
  Till's deterministic cash engine. Customer emails never reach the model or the screen; they become short hashes.
- **Voice questions for sellers:** "How's PayPal looking?", "Any refund or dispute problems?", "Who owes me money?",
  "What will my PayPal balance look like this quarter?", "Are PayPal fees creeping up?"
- **Checks that matter to a small seller:** effective fee rate against the seller's own baseline, refund-rate spikes and
  repeat refunders, dispute money at risk sorted by response deadline, overdue invoices, and a 13-week balance
  forecast that counts unpaid invoices as cash arriving on their due dates.
- **Agentic actions with a human in the loop:** send an invoice, refund a payment, or settle a small dispute. Every
  action is a two-step: a `preview_*` tool explains exactly what will happen and its effect on the cash forecast and
  returns a one-time code; `confirm_action` is the only path that touches PayPal, refuses expired or reused codes,
  and can't refund more than was paid.
- **Cash desk dashboard** at `/dashboard` (AG Grid + AG Charts) with the same numbers the voice answers use.

Run with sandbox keys in `.env` (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`), or with no keys at all:
`PAYPAL_FIXTURES=data/paypal_fixtures .venv/bin/python -m till.server`, then open `/?paypal` and `/dashboard`.
Tests: `tests/test_paypal.py` (fixtures) and `tests/test_paypal.py --live` (read-only against the sandbox).

## What it does

| Tool | Ask it | What happens |
|---|---|---|
| `morning_brief` | "How are we doing?" | Cash on hand, change since your last brief, the tightest week ahead, alerts, and charges still to review |
| `cash_snapshot` | "How much is in the bank?" | Balance, 30-day in and out, runway |
| `cash_forecast` | "Will we be short this quarter?" | 13-week forecast from recent sales and recurring bills; lowest point; first week under your alert |
| `unusual_charges` | "Any weird charges?" | Duplicate payments, bills that jumped, large first-time vendors, each with a plain reason and dollar amount |
| `review_charge` | "Dispute the flour one." | Remembers your decision so Till stops raising it |
| `can_i_afford` | "Can I hire a part-time baker at $1,800 a month?" | Re-runs the forecast with the new cost: comfortable, tight, or not yet |
| `spending_change` | "Why was September so expensive?" | Month-over-month by category, biggest drivers first |
| `set_cash_alert` | "Warn me if cash drops under 12 thousand." | Remembered; every forecast and brief checks it |
| `import_books` | (from a CSV) | Load your own transactions instead of the demo bakery |
| `forget_business` | "Start over." | Clears everything remembered |

**Every number comes from deterministic Python** (`till/engine.py`, stdlib only), never from a language model. The
model's job is to pick tools, chain them, and talk. Tool results lead with a sentence written for the ear ("16.7
thousand dollars", not "$16,717.31"), so any voice assistant can read them as-is.

**Remembers across sessions.** Cash alerts, reviewed charges, and the last brief live in SQLite. Dispute a charge
today and tomorrow's brief says "3 charges need a look", not 4.

**Cards via MCP Apps.** Every tool is bound to `ui://till/card.html` (`text/html;profile=mcp-app`) through the
`io.modelcontextprotocol/ui` extension. Hosts that support MCP Apps render a card with the big number, a forecast
line with your alert threshold, and line items. Hosts that don't still get the full text.

## Run it (2 minutes)

```bash
git clone <this repo> till && cd till
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m till.server
# MCP endpoint  http://127.0.0.1:8765/mcp
# Demo page     http://127.0.0.1:8765/      (use Chrome for the microphone)
```

Optional: copy `env.example` to `.env` and add an `ANTHROPIC_API_KEY`. With a key, Claude chooses and chains
the tools ("dispute the flour charge, and can I afford a $2,400 mixer next week?" becomes `review_charge` then
`can_i_afford`). Without one, the page uses a built-in keyword router. No key is needed to judge the MCP server.

### Connect it to an assistant

Any MCP client that speaks Streamable HTTP can use Till. For example, in Claude Code:

```bash
claude mcp add --transport http till http://127.0.0.1:8765/mcp
```

### Tests

```bash
.venv/bin/python tests/test_engine.py                                  # analysis engine
.venv/bin/python tests/test_protocol.py http://127.0.0.1:8765/mcp      # raw JSON-RPC over Streamable HTTP
```

`test_protocol.py` speaks the wire protocol directly, the same way the demo page does. It checks that
initialize negotiates 2025-11-25, lists the tools, reads the `ui://` resource, and calls every tool, including
memory across calls.

## How it's built

```
voice (Web Speech API) ──> demo page = MCP client ──Streamable HTTP──> Till MCP server (mcp 2.3.0)
                           │  agent loop: Claude picks tools                ├─ engine.py   deterministic cash math
                           │  (via /agent/llm; key stays on server)         ├─ memory.py   SQLite: alerts, reviews, briefs
                           └─ MCP Apps host: sandboxed iframe <── ui://till/card.html
```

- `till/server.py`: the ten tools, MCP Apps binding, and demo routes.
- `till/engine.py`: snapshot, recurring-bill detection, 13-week forecast, anomaly rules, affordability, and month-over-month change.
- `till/memory.py`: what Till remembers per business.
- `till/demo_data.py`: generates the fictional **Sunrise Bakery** books (seeded). Four problems are planted on purpose: a duplicate supplier payment, a software price jump, a utility spike, and a large first-time vendor.
- `web/index.html`: the voice demo client and MCP Apps host. `web/card.html` is the MCP App view.

All data in this repo is synthetic. Till is a tool for owners, not a licensed advisor, and it does not give tax or
investment advice.

## License

MIT
