# Trade Suggestion

A personal algorithmic trading research project: two independent rule-based
scoring engines (an intraday scanner and a swing-trade evaluator) that scan a
stock universe, score setups against a weighted-confluence model, log every
signal as a paper trade, and track real accuracy over time. Includes a Flask
+ React dashboard, walk-forward backtesting tools, and an Interactive
Brokers data/execution layer — including an MCP server that exposes IBKR
data to AI agents.

This is a research and paper-trading system, not a live-money execution
system — see [Current Status](#current-status) before drawing any
conclusions from it.

## What's actually in here

### Two independent strategies

**Intraday scanner** (`day_trading.py`) — scans a watchlist every 5 minutes
during market hours, scores each symbol on a 0–100 scale (opening range
breakout with a volume qualifier, VWAP position, RSI momentum, an
order-flow-imbalance proxy, key-level confluence, relative strength vs.
SPY, and more), and logs Grade S/A/B/C setups. Stop = entry ± 2×ATR,
target = entry ± 3×ATR, minimum 1.5 R:R.

**Swing scorer** (`core_signals.py` → `evaluate_stock()`) — a longer-horizon
version of the same idea: trend (EMA9/21), 52-week-high proximity, RSI,
CMF, OBV, MFI, Bollinger compression, and relative strength, combined into
a single confluence score for swing-length setups.

Both strategies log every signal as a paper trade (not real orders) and are
independently backtestable (`Backtest.py` for swing, `scripts/intraday_backtest.py`
for intraday) and independently accuracy-tracked (`scripts/daily_validate.py`,
`scripts/intraday_validate.py`).

### Architecture

```
                    ┌─────────────────┐
                    │  yfinance /      │   free delayed bars, used by default
                    │  Interactive     │
                    │  Brokers (IBKR)  │   real-time bars + order execution,
                    └────────┬─────────┘   via ib_insync (ibkr_data.py)
                             │
         ┌───────────────────┼───────────────────┐
         │                   │                    │
  day_trading.py      core_signals.py       Backtest.py /
  (intraday scan +    (shared indicators +   scripts/*_backtest.py
   scoring engine)     swing scoring)        (walk-forward backtests)
         │                   │
         └─────────┬─────────┘
                    │
                 api.py                 Flask REST API + serves the
                (Flask server)          built React dashboard
                    │
            web/ (React + Vite)         live signals, charts (echarts,
                                         lightweight-charts), backtest UI
```

`mcp_ibkr.py` is a separate [Model Context Protocol](https://modelcontextprotocol.io)
server that exposes IBKR historical bars, live quotes, and account data as
tools an AI agent can call directly.

Automation runs via Windows Task Scheduler (PowerShell scripts in
`scripts/`) — market-open/close scans, nightly accuracy validation, and a
Friday weekly review — with push notifications on Grade S/A signals via
[ntfy.sh](https://ntfy.sh) (configured in `data/config.env`).

No pandas/numpy anywhere in the codebase — indicators and backtesting are
implemented directly in pure Python against plain lists/dicts.

## Current status

Numbers below are computed directly from the actual closed-trade logs
(`data/day_trades.json`, `data/paper_trades.json`), not copied from an
older status doc — those go stale fast in a project like this.

| Strategy | Closed trades | Win rate | Profit factor | Avg P&L/trade | Trade dates |
|---|---:|---:|---:|---:|---|
| Intraday scanner | 22 | 40.9% | **0.48** | −0.35% | May 19 – Jun 4, 2026 |
| Swing scorer | 12 | 66.7% | **2.16** | +3.12% | Apr 17 – May 17, 2026 |

Two things worth being direct about:

- **Both tracks are currently dormant.** Neither has a new closed trade
  since early June 2026, despite scheduled tasks still touching some state
  files (`data/cooldowns.json` was last modified Sept 12, 2026, but its
  contents are stale May-era timestamps) — worth checking whether the
  scheduled scan tasks are actually still finding/logging qualifying setups
  before trusting the automation is fully live.
- **The intraday scanner is currently unprofitable** (PF 0.48, n=22) — a
  small sample, but not a track record to act on. The swing scorer looks
  better (PF 2.16, n=12) but n=12 is far too small to draw a real
  conclusion either way. Both need meaningfully more closed trades before
  the profit factor numbers mean anything — treat everything above as "what
  the data says today," not a performance claim.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Frontend (only needed if you're changing the dashboard UI —
# a built copy is expected at web/dist/ for api.py to serve)
cd web && npm install && npm run build && cd ..

cp data/config.env.example data/config.env  # then edit NTFY_TOPIC to your own
```

Interactive Brokers features (`ibkr_data.py`, `mcp_ibkr.py`, live order
data) require TWS or IB Gateway running locally with the API enabled —
without it, the system falls back to free delayed data via `yfinance`.

## Usage

```bash
# Start the dashboard + API (http://localhost:5000)
python3 api.py

# Single intraday scan snapshot (during market hours)
python3 day_trading.py --once

# Check accuracy on closed trades
python3 scripts/intraday_validate.py
python3 scripts/daily_validate.py

# Walk-forward backtests
python3 Backtest.py                          # swing
python3 scripts/intraday_backtest.py         # intraday (2yr hourly bars as 5-min proxy)

# Find the best signal-score threshold from real closed-trade history
python3 scripts/optimize_threshold.py
```

## Project structure

```
Trade_suggestion/
├── day_trading.py          intraday scanner + scoring engine
├── core_signals.py         shared indicator math + swing evaluate_stock()
├── api.py                  Flask REST API + dashboard server
├── Backtest.py              swing walk-forward backtest
├── ibkr_data.py             Interactive Brokers data layer (ib_insync)
├── mcp_ibkr.py              MCP server exposing IBKR to AI agents
├── signals.py / largecapsignals.py / rsi_watch.py / SPXindex.py
│                            smaller/legacy signal & watch scripts
├── web/                     React + Vite dashboard (charts, live signals)
├── scripts/
│   ├── daily_scan.py / daily_validate.py
│   ├── intraday_backtest.py / intraday_validate.py
│   ├── optimize_threshold.py
│   ├── weekly_review.sh
│   └── setup_task.ps1 / setup_validate_task.ps1   Windows Task Scheduler registration
├── data/                    trade logs, accuracy history, config (gitignored except structure)
└── runbook.pdf / runbook.tex   full operations guide (schedule, scoring table, benchmarks)
```

## Known gaps

- No requirements.txt existed before this README — dependencies above were
  reverse-engineered from actual imports across the codebase, not from a
  manifest, so double-check versions if something breaks on a fresh install.
- RVOL currently uses a 20-bar rolling average, which is inflated right at
  the open — a time-of-day-normalized version (compare current bar to the
  same time-slot average over the last 10 sessions) would be more accurate.
- No Volume Profile (POC/VAH/VAL) yet — previous-session POC as
  support/resistance would add structural context to the ORB and breakout
  signals.
- Real 5-minute intraday backtesting is limited by data access — Yahoo only
  gives 5 days of free 5-minute history, so `intraday_backtest.py` uses
  hourly bars as a proxy. A paid historical data source (e.g. Polygon.io)
  would remove this limitation.
- `Backtest.py` uses `yfinance` with `auto_adjust=True`, which applies
  splits/dividends retroactively — can slightly overstate historical
  returns. Not urgent, but worth knowing about before trusting an exact
  backtest number.
