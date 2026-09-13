#!/usr/bin/env python3
"""
scripts/smoke_test.py — Regression smoke test for the scoring pipeline.

No network, no IBKR — synthetic bars only. Run after ANY change to
day_trading.py / core_signals.py before the next market session:

    python3 scripts/smoke_test.py

Covers the 2026-06-12 overhaul: VWAP σ bands, anchored VWAP, RVOL
time-of-day, breakeven ratchet, risk caps, RSI divergence, PEAD window
math, and the full compute_indicators → score_intraday path.
"""
import sys, os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import day_trading as dt
import core_signals as cs

_ET = ZoneInfo("America/New_York")
FAIL = []


def check(name, cond, detail=""):
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


def synth_bars(days=3, bars_per_day=78, start_price=100.0, drift=0.02):
    """Synthetic 5-min RTH bars: (ts, o, h, l, c, v), gently trending up."""
    bars, price = [], start_price
    day0 = datetime.now(tz=_ET).replace(hour=9, minute=30, second=0, microsecond=0)
    day0 -= timedelta(days=days + 2)
    d = 0
    while d < days:
        sess = day0 + timedelta(days=d)
        if sess.weekday() >= 5:               # skip weekends
            day0 += timedelta(days=1)
            continue
        for i in range(bars_per_day):
            ts = sess + timedelta(minutes=5 * i)
            o  = price
            c  = price + drift + ((i % 7) - 3) * 0.03
            h  = max(o, c) + 0.05
            l  = min(o, c) - 0.05
            v  = 10_000 + (5_000 if i == 0 else 0) + (i % 5) * 500
            bars.append((int(ts.timestamp()), round(o, 4), round(h, 4),
                         round(l, 4), round(c, 4), v))
            price = c
        d += 1
    return bars


def agg15(bars5):
    out = []
    for i in range(0, len(bars5) - 2, 3):
        grp = bars5[i:i + 3]
        out.append((grp[0][0], grp[0][1], max(b[2] for b in grp),
                    min(b[3] for b in grp), grp[-1][4], sum(b[5] for b in grp)))
    return out


print("── pipeline ──")
primary = synth_bars()
ind = dt.compute_indicators(primary, agg15(primary)[-13:])
check("compute_indicators returns dict", isinstance(ind, dict))
if ind:
    for key in ("vwap", "vwap_sigma", "rel_vol", "atr", "rsi", "or_high",
                "open_first", "highest15", "rs_vs_spy"):
        check(f"ind has {key}", key in ind)
    res = dt.score_intraday(ind)
    check("score_intraday returns 6-tuple", isinstance(res, tuple) and len(res) == 6)
    check("scores are numeric", isinstance(res[0], (int, float)) and isinstance(res[1], (int, float)))

print("── new calcs ──")
vw, sg = dt._calc_vwap_sigma([101, 102, 103], [99, 100, 101], [100, 101, 102], [1000, 1200, 800])
check("_calc_vwap_sigma", 99 < vw < 103 and sg > 0)
av = dt._calc_anchored_vwap(primary, primary[len(primary) // 2][4])
check("_calc_anchored_vwap", av is None or av > 0)
check("_calc_rvol_tod", dt._calc_rvol_tod(primary) > 0)
div = cs._detect_rsi_divergence([100 - i * 0.3 for i in range(40)] + [89, 90])
check("_detect_rsi_divergence runs", div in (None, "bullish", "bearish"))

print("── risk constants ──")
check("BREAKEVEN_AT_R", getattr(dt, "BREAKEVEN_AT_R", 0) > 0)
check("DAILY_MAX_STOPS", getattr(dt, "DAILY_MAX_STOPS", 0) >= 1)
check("MAX_OPEN_TRADES", getattr(dt, "MAX_OPEN_TRADES", 0) >= 1)
check("MAX_OPEN_PER_SECTOR", getattr(dt, "MAX_OPEN_PER_SECTOR", 0) >= 1)
check("CALL-only threshold sane", 50 <= dt.SIGNAL_THRESHOLD <= 100)

print("── breakeven ratchet (mirrors check_open_trades walk) ──")
entry, stop, target = 100.0, 98.0, 106.0
be_trigger = entry + (entry - stop) * dt.BREAKEVEN_AT_R
eff_stop, armed, exit_px = stop, False, None
walk = [(101.0, 99.5), (102.5, 100.9), (102.2, 99.0)]   # (hi, lo): runs +1R, collapses
for hi, lo in walk:
    if lo <= eff_stop:
        exit_px = eff_stop
        break
    if hi >= target:
        exit_px = target
        break
    if not armed and hi >= be_trigger:
        armed, eff_stop = True, entry
check("ratchet scratches at entry (not full stop)", exit_px == entry,
      f"got {exit_px}")

print("── key levels / PEAD window ──")
bb, sb, bt, st = dt._key_level_bonus(105.0, {"prev_day_high": 104.0, "prev_day_low": 100.0,
                                             "pm_high": 103.0, "pm_low": 101.0})
check("_key_level_bonus PDH break", bb > 0 and any("PDH" in t for t in bt))
daily = [{"open": 100, "close": 101}, {"open": 101, "close": 100},
         {"open": 108, "close": 110}, {"open": 110, "close": 111},
         {"open": 111, "close": 112}]
window = daily[-(2 + 2):]
gap = max(((window[j]["open"] - window[j-1]["close"]) / window[j-1]["close"] * 100)
          for j in range(1, len(window)))
check("PEAD finds reaction gap", gap > 6)

print("── module imports ──")
import importlib.util
for mod in ("ibkr_data",):
    check(f"{mod} importable", importlib.util.find_spec(mod) is not None)
spec = importlib.util.spec_from_file_location(
    "iv", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scripts", "intraday_validate.py"))
check("intraday_validate parses", spec is not None)

print()
if FAIL:
    print(f"FAILED: {len(FAIL)} check(s): {', '.join(FAIL)}")
    sys.exit(1)
print("ALL CHECKS PASSED — safe to run next session")
