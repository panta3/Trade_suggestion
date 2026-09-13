# Session Handoff — May 13 2026

## What Was Built / Changed Today

### Intraday Scanner (`day_trading.py`)
- **ORB volume qualifier** — ORB bonus now requires `rel_vol ≥ 1.3` to award full +25 pts. Without volume it drops to +8. Backed by Zarattini et al. 2024 (SSRN 4729284).
- **OFI proxy** — Added `(close−open)/(high−low)` as a bar-level order flow signal. >0.65 = +8 bull, <−0.65 = +8 bear. (arXiv:2408.03594)

### Swing Scorer (`core_signals.py → evaluate_stock()`)
- **52-week high proximity** — `hi52` was computed but never used. Now wired into scoring:
  - ≤3% from 52-wk high → +1.2 (at highs)
  - ≤15% → +0.8 (near highs, sweet spot)
  - ≤25% → +0.3
  - >30% below → −0.8 warning (structural downtrend)

### New Scripts
| Script | Purpose |
|---|---|
| `scripts/intraday_backtest.py` | Walk-forward backtest using 2yr hourly bars as 5-min proxy |
| `scripts/optimize_threshold.py` | Finds best SIGNAL_THRESHOLD from closed trade history |
| `scripts/weekly_review.sh` | Friday master report (runs all 3 validators) |

### Docs
- `runbook.pdf` — 6-page operations guide: schedule, commands, scoring table, benchmarks (v1.1)

---

## Current Live Performance (14 trades, early data)

| Metric | Value |
|---|---|
| Win Rate | 50.0% |
| Profit Factor | 1.78 |
| Avg P&L/trade | +0.26% (after 0.10% slippage) |
| CALL accuracy | 63.6% WR, PF 4.09 (n=11) |
| PUT accuracy | 0% WR (n=3, too small) |

> Need 50+ closed trades before drawing real conclusions. Check again end of week.

---

## Full Automation Schedule

| Time (ET) | Task | Script |
|---|---|---|
| Login | `GapFadeAlgo_Start` | `api.py` |
| 9:30 AM | `GapFadeAlgo_DailyScan_Open` | `scripts/daily_scan.py` |
| Continuous | api.py scanner loop | `day_trading.py` (every 5 min) |
| 3:45 PM | `GapFadeAlgo_DailyScan_Close` | `scripts/daily_scan.py --close` |
| 4:00 PM | `GapFadeAlgo_SwingValidate` | `scripts/daily_validate.py` |
| 4:15 PM | `GapFadeAlgo_IntradayValidate` | `scripts/intraday_validate.py --close-eod` |
| Friday 4:30 PM | `GapFadeAlgo_WeeklyReview` | `scripts/weekly_review.sh` |

Push notifications: ntfy topic `aarav-scanner-9274` — fires on Grade S/A only.

---

## Known Gaps / Things to Do Next

### High Priority (do when you have 30+ trades)
- [ ] Run `python3 scripts/optimize_threshold.py` — check if threshold should move from 68
- [ ] Grade A/S vs B split: if Grade A/S WR > 60%, tighten threshold to 80+
- [ ] PUT performance is 0% on 3 trades — watch if this persists (algo may be better CALL-only)

### Medium Priority (next 1–2 weeks)
- [ ] **RVOL time-of-day normalization** — compare current bar volume vs same time-slot average over last 10 sessions. Currently uses 20-bar rolling avg which is inflated at open. (TradingView "Volume+ RVOL By Time of Day" is the reference implementation)
- [ ] **Volume Profile (POC/VAH/VAL)** — previous session POC as support/resistance. Can build from OHLCV. Adds structural context to ORB and breakout signals.
- [ ] Fix `GapFadeAlgo_DailyScan_Close` PowerShell task — may have the `$true` boolean bug from before. Re-register if swing close scan isn't running.

### Low Priority / Research
- [ ] Intraday backtest on real 5-min data — Yahoo only gives 5 days free. Options: (a) save daily 5-min bars to CSV as they come in and backtest after 2 months, (b) pay for historical data source (Polygon.io ~$29/mo)
- [ ] Swing backtest (`Backtest.py`) — note yfinance `auto_adjust=True` applies retroactively, may slightly overstate returns. Not urgent.

---

## Key File Locations

```
Trade_suggestion/
├── day_trading.py          ← intraday scanner + scoring engine
├── core_signals.py         ← shared math + swing evaluate_stock()
├── api.py                  ← web dashboard + scheduler
├── Backtest.py             ← swing walk-forward backtest
├── data/
│   ├── day_trades.json     ← intraday paper trades
│   ├── paper_trades.json   ← swing paper trades
│   ├── accuracy_stats.json ← historical accuracy runs
│   ├── config.env          ← NTFY_TOPIC=aarav-scanner-9274
│   ├── scanner.log         ← rolling api.py log
│   └── validate.log        ← nightly validate output
├── scripts/
│   ├── intraday_validate.py
│   ├── optimize_threshold.py
│   ├── intraday_backtest.py
│   └── weekly_review.sh
└── runbook.pdf             ← full operations guide
```

---

## Quick Start for Tomorrow

```bash
# Check overnight validate ran OK
tail -30 data/validate.log

# Manual accuracy check
python3 scripts/intraday_validate.py

# Start scanner + dashboard
python3 api.py
# → open http://localhost:5000

# Single scan snapshot (during market hours)
python3 day_trading.py --once
```

---

## Scoring Engine Quick Reference

| Grade | Score | ntfy | Notes |
|---|---|---|---|
| S | ≥90 | Yes (high) | Full confluence |
| A | ≥80 | Yes (normal) | Strong setup |
| B | ≥68 | No | Above threshold |
| C | ≥55 | No | Logged only |

Stop = entry ± ATR×2.0 — Target = entry ± ATR×3.0 — Min R:R = 1.5
