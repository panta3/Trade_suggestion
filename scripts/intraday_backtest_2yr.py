#!/usr/bin/env python3
"""
scripts/intraday_backtest_2yr.py — 2-year walk-forward backtest (IBKR 15-min bars).

Uses 15-min bars fetched from IBKR (2 years — IBKR's limit for 15-min history).
Derives 1-hour HTF bars by aggregating every 4 15-min bars.
2-year window includes both bull and bear market periods for PUT signal validation.

Walks bar-by-bar per trading day, calling compute_indicators() + score_intraday()
from day_trading.py with only past data visible — no lookahead.

Stats: win rate, profit factor, Sharpe, max drawdown, by grade / direction / hour / symbol.
Walk-forward split: first 67% in-sample, last 33% out-of-sample.

Usage:
    python3 scripts/intraday_backtest_2yr.py
    python3 scripts/intraday_backtest_2yr.py --symbols TSLA NVDA AAPL
    python3 scripts/intraday_backtest_2yr.py --threshold 80 --top 15
    python3 scripts/intraday_backtest_2yr.py --enable-puts   # test PUT signals too
"""

import sys, os, argparse, statistics, time, pickle
from datetime import datetime, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Use client IDs 45/46 so this script doesn't conflict with
# day_trading.py / core_signals.py which occupy 43/44.
os.environ.setdefault("IBKR_CLIENT_ID", "45")

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

from day_trading import (
    DEFAULT_SYMBOLS,
    compute_indicators,
    score_intraday,
    ATR_STOP_MULT,
    ATR_TARGET_MULT,
    SIGNAL_THRESHOLD,
)

# ── Constants ─────────────────────────────────────────────────────────────────
SLIPPAGE_PCT   = 0.10   # round-trip slippage %
MAX_HOLD_BARS  = 26     # 26 × 15-min bars = 6.5 hours (rest of day)
COOLDOWN_BARS  = 8      # 8 × 15-min bars = 2 hours between signals for same symbol
PRIMARY_WINDOW = 130    # rolling 15-min bars fed to compute_indicators (≈5 days)
HTF_AGG        = 4      # aggregate every 4 15-min bars → 1 one-hour bar


# ── Data Fetching ─────────────────────────────────────────────────────────────
_CACHE_DIR = os.path.join(ROOT, "scripts", "bar_cache")


_MIN_BARS_TO_CACHE = 2000  # Yahoo 60d fallback = 1560 bars; require >2000 to confirm real IBKR history

def _cache_key(symbol: str) -> str:
    today = datetime.now().strftime("%Y%m%d")
    return os.path.join(_CACHE_DIR, f"{symbol.upper()}_15m_{today}.pkl")


def _find_best_cache(symbol: str) -> tuple[str | None, list | None]:
    """Return (path, bars) for the largest valid cache file for this symbol, or (None, None)."""
    import glob as _glob
    pattern = os.path.join(_CACHE_DIR, f"{symbol.upper()}_15m_*.pkl")
    best_path, best_bars = None, None
    for path in _glob.glob(pattern):
        try:
            with open(path, "rb") as f:
                bars = pickle.load(f)
            if bars and len(bars) >= _MIN_BARS_TO_CACHE:
                if best_bars is None or len(bars) > len(best_bars):
                    best_path, best_bars = path, bars
        except Exception:
            pass
    return best_path, best_bars


def _fetch_15min(symbol: str, use_cache: bool = True) -> list:
    if use_cache:
        _, cached = _find_best_cache(symbol)
        if cached:
            return cached

    import ibkr_data as _id
    bars = _id.get_intraday_bars(symbol, interval="15m", period="730d")

    if use_cache and bars and len(bars) >= _MIN_BARS_TO_CACHE:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        cache_path = _cache_key(symbol)
        with open(cache_path, "wb") as f:
            pickle.dump(bars, f)

    return bars


def fetch_all(symbols: list, use_cache: bool = True) -> dict:
    n       = len(symbols)
    result  = {}
    n_cache = sum(1 for s in symbols if os.path.exists(_cache_key(s)))
    n_live  = n - n_cache

    print(f"  {n_cache}/{n} cached  |  {n_live} need IBKR/Yahoo fetch")
    if n_live > 0:
        print(f"  Fetching {n_live} symbols in parallel (5 workers)…")

    completed = [0]

    def _fetch_one(sym):
        bars = _fetch_15min(sym, use_cache=use_cache)
        completed[0] += 1
        cached = use_cache and os.path.exists(_cache_key(sym))
        src = "cache" if cached else ("IBKR" if bars and len(bars) > 100 else "Yahoo")
        tag = f"{len(bars):>5} bars" if bars else "  no data"
        print(f"    [{completed[0]:>3}/{n}] {sym:<6}  {tag}  ({src})")
        return sym, bars

    # Parallel fetch — 5 workers keeps IBKR happy without hammering it
    with ThreadPoolExecutor(max_workers=5) as ex:
        for sym, bars in ex.map(_fetch_one, symbols):
            result[sym] = bars

    n_ok = sum(1 for b in result.values() if b)
    print(f"  Done: {n_ok}/{n} symbols returned data")
    return result


# ── Bar Utilities ─────────────────────────────────────────────────────────────

def _market_hours(bars):
    out = []
    for b in bars:
        dt = datetime.fromtimestamp(b[0], tz=_ET)
        h, m = dt.hour, dt.minute
        if (h > 9 or (h == 9 and m >= 30)) and h < 16:
            out.append(b)
    return out


def _group_by_day(bars):
    days = defaultdict(list)
    for b in bars:
        dt = datetime.fromtimestamp(b[0], tz=_ET)
        days[dt.date()].append(b)
    return {d: sorted(v, key=lambda x: x[0]) for d, v in days.items() if len(v) >= 10}


def _agg_to_1hour(bars_15min):
    out = []
    for i in range(0, len(bars_15min) - (HTF_AGG - 1), HTF_AGG):
        grp = bars_15min[i:i + HTF_AGG]
        if len(grp) < HTF_AGG:
            break
        out.append((
            grp[-1][0],
            grp[0][1],
            max(b[2] for b in grp),
            min(b[3] for b in grp),
            grp[-1][4],
            sum(b[5] for b in grp),
        ))
    return out


# ── Trade Simulation ──────────────────────────────────────────────────────────

def _simulate(direction, entry, atr, future_bars):
    if direction == "CALL":
        stop   = entry - atr * ATR_STOP_MULT
        target = entry + atr * ATR_TARGET_MULT
        def tgt(h, l): return h >= target
        def stp(h, l): return l <= stop
    else:
        stop   = entry + atr * ATR_STOP_MULT
        target = entry - atr * ATR_TARGET_MULT
        def tgt(h, l): return l <= target
        def stp(h, l): return h >= stop

    exit_price  = None
    exit_reason = "timeout"
    for bar in future_bars[:MAX_HOLD_BARS]:
        _, _, hi, lo, cl, _ = bar
        if stp(hi, lo):
            exit_price  = stop
            exit_reason = "stop"
            break
        if tgt(hi, lo):
            exit_price  = target
            exit_reason = "target"
            break

    if exit_price is None:
        last = future_bars[min(MAX_HOLD_BARS - 1, len(future_bars) - 1)]
        exit_price = last[4]

    pnl = ((exit_price - entry) / entry * 100 if direction == "CALL"
           else (entry - exit_price) / entry * 100)
    pnl -= SLIPPAGE_PCT
    return round(pnl, 4), exit_reason


def _grade(score):
    if score >= 90: return "S"
    if score >= 80: return "A"
    if score >= 68: return "B"
    if score >= 55: return "C"
    return "D"


# ── Per-Symbol Backtest ───────────────────────────────────────────────────────

def run_symbol(symbol, all_bars, spy_day_map, threshold, oos_start,
               enable_puts: bool = False):
    mh_bars = _market_hours(all_bars)
    if len(mh_bars) < PRIMARY_WINDOW:
        return []

    day_map = _group_by_day(mh_bars)
    if len(day_map) < 15:
        return []

    trades        = []
    seen: list    = []
    sorted_days   = sorted(day_map)

    for day in sorted_days:
        day_bars       = day_map[day]
        spy_today      = spy_day_map.get(day, [])
        spy_open       = spy_today[0][1] if spy_today else None
        cooldown_until = -1

        for i, bar in enumerate(day_bars):
            seen.append(bar)

            if len(seen) < PRIMARY_WINDOW:
                continue
            if i <= cooldown_until:
                continue

            primary = seen[-PRIMARY_WINDOW:]
            htf     = _agg_to_1hour(primary)[-13:]

            ind = compute_indicators(primary, htf)
            if not ind:
                continue

            bar_et = datetime.fromtimestamp(bar[0], tz=_ET)
            # Time filter: avoid first 30 min chop (9:30-10:00) and last 15 min (15:45+)
            in_window = (
                (bar_et.hour == 10 and bar_et.minute >= 0) or
                bar_et.hour in (11, 12, 13, 14) or
                (bar_et.hour == 15 and bar_et.minute < 45)
            )
            if not in_window:
                continue

            # Volume burst: current bar must be ≥1.5× the 20-bar average (real momentum, not noise)
            if len(primary) >= 20:
                avg_vol = sum(b[5] for b in primary[-20:]) / 20
                if avg_vol > 0 and bar[5] < avg_vol * 1.5:
                    continue

            regime_bull = True
            if spy_open and spy_today and spy_open > 0:
                spy_curr    = spy_today[min(i, len(spy_today) - 1)][4]
                spy_chg     = (spy_curr - spy_open) / spy_open * 100
                day_open    = day_bars[0][1]
                stk_chg     = (ind["price"] - day_open) / day_open * 100 if day_open else 0.0
                ind["rs_vs_spy"] = round(stk_chg - spy_chg, 2)
                # Stronger regime gate: SPY must be up ≥0.10% from open (not just marginally positive)
                regime_bull = spy_curr >= spy_open * 1.001

            bull_raw, bear_raw, _, _, _, _ = score_intraday(ind)
            bull = max(0.0, min(100.0, bull_raw))
            bear = max(0.0, min(100.0, bear_raw))

            if regime_bull:
                bear = 0.0
            else:
                bull = 0.0
            if not enable_puts:
                bear = 0.0

            if bull >= threshold and bull > bear:
                direction, score_val = "CALL", bull
            elif bear >= threshold and bear > bull:
                direction, score_val = "PUT", bear
            else:
                continue

            if i + 1 >= len(day_bars):
                continue
            entry_bar = day_bars[i + 1]
            entry     = entry_bar[1]
            atr       = ind["atr"] or (entry * 0.005)

            if atr / entry < 0.005:
                continue

            future = day_bars[i + 2:]
            if not future:
                continue

            pnl, reason = _simulate(direction, entry, atr, future)

            trades.append({
                "symbol":    symbol,
                "date":      day.isoformat(),
                "direction": direction,
                "score":     round(score_val, 1),
                "grade":     _grade(score_val),
                "entry":     round(entry, 4),
                "atr":       round(atr, 5),
                "pnl_pct":   pnl,
                "exit":      reason,
                "oos":       day >= oos_start,
                "hour":      bar_et.hour,
                "regime":    "bull" if regime_bull else "bear",
            })
            cooldown_until = i + COOLDOWN_BARS

    return trades


# ── Stats ─────────────────────────────────────────────────────────────────────

def compute_stats(trades):
    if not trades:
        return None
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p <= 0]

    win_rate = len(wins) / len(pnls) * 100
    avg_pnl  = sum(pnls) / len(pnls)
    pf       = sum(wins) / abs(sum(loss)) if loss else 999.0

    sharpe = 0.0
    if len(pnls) > 1:
        std = statistics.stdev(pnls)
        if std > 0:
            sharpe = (avg_pnl / std) * (252 ** 0.5)

    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p
        peak    = max(peak, equity)
        max_dd  = max(max_dd, peak - equity)

    return {
        "n": len(trades), "win_rate": round(win_rate, 1),
        "avg_pnl": round(avg_pnl, 3), "pf": round(min(pf, 999.0), 2),
        "sharpe": round(sharpe, 2), "max_dd": round(max_dd, 2),
    }


def _row(label, trades, indent=2):
    s = compute_stats(trades)
    if s is None:
        print(f"{' '*indent}{label:<24}  —")
        return
    flag = (" ✓" if s["win_rate"] >= 55 and s["pf"] >= 1.3 else
            " ✗" if s["win_rate"] < 45 or s["pf"] < 0.9 else "")
    print(
        f"{' '*indent}{label:<24}"
        f"  n={s['n']:>4}  WR={s['win_rate']:>5.1f}%"
        f"  PF={s['pf']:>5.2f}  Sharpe={s['sharpe']:>5.2f}"
        f"  MaxDD={s['max_dd']:>6.2f}%  AvgP={s['avg_pnl']:>+.3f}%{flag}"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="2-year walk-forward backtest (IBKR 15-min)")
    ap.add_argument("--symbols",     nargs="+", default=None)
    ap.add_argument("--threshold",   type=float, default=SIGNAL_THRESHOLD)
    ap.add_argument("--hold",        type=int,   default=MAX_HOLD_BARS,
                    help="Max bars to hold (1 bar = 15 min)")
    ap.add_argument("--top",         type=int,   default=20)
    ap.add_argument("--grade",       default=None, choices=["S","A","B","C"])
    ap.add_argument("--no-cache",    action="store_true")
    ap.add_argument("--enable-puts", action="store_true",
                    help="Test PUT signals — off by default, use to validate bear market edge")
    ap.add_argument("--json",        action="store_true",
                    help="Output results as JSON to stdout (used by API endpoint)")
    ap.add_argument("--precache-only", action="store_true",
                    help="Fetch and cache bars for all symbols then exit (no backtest)")
    args = ap.parse_args()

    # In --json mode, redirect all print() output to stderr so stdout is pure JSON
    if args.json:
        import sys as _sys2
        _real_stdout = _sys2.stdout
        _sys2.stdout = _sys2.stderr

    symbols     = args.symbols or DEFAULT_SYMBOLS
    threshold   = args.threshold
    use_cache   = not args.no_cache
    enable_puts = args.enable_puts

    n_cached   = sum(1 for s in (["SPY"] + symbols) if os.path.exists(_cache_key(s)))
    n_uncached = len(symbols) + 1 - n_cached
    est_fetch  = n_uncached * 5
    est_walk   = len(symbols) * 13000 * 0.006
    est_total  = est_fetch + est_walk
    est_str    = f"{int(est_total//60)}m {int(est_total%60)}s"

    puts_label = "CALLS + PUTS" if enable_puts else "CALLS only"
    print("=" * 72)
    print(f" INTRADAY BACKTEST 2YR  |  {len(symbols)} symbols  |  threshold={threshold:.0f}  |  {puts_label}")
    print(f" Data: IBKR 15-min bars × 2 years  (Yahoo 60-day fallback if unavailable)")
    print(f" Slippage: {SLIPPAGE_PCT:.2f}% RT  |  MaxHold: {MAX_HOLD_BARS} bars ({MAX_HOLD_BARS*15//60}h)  |  Cooldown: {COOLDOWN_BARS} bars ({COOLDOWN_BARS*15//60}h)")
    print(f" Primary window: {PRIMARY_WINDOW} 15-min bars  |  HTF: 1-hour (aggregated)")
    print(f" Cache: {'OFF' if not use_cache else f'{n_cached} hits / {n_uncached} need fetch'}")
    print(f" Estimated runtime: ~{est_str}  (fetch {est_fetch:.0f}s + walk {est_walk:.0f}s)")
    print("=" * 72)

    fetch_list = list(dict.fromkeys(["SPY"] + symbols))
    t0   = time.time()

    if args.precache_only:
        print(f"PRE-CACHE MODE — fetching {len(fetch_list)} symbols, no backtest")
        data = fetch_all(fetch_list, use_cache=use_cache)
        n_ok = sum(1 for b in data.values() if b)
        print(f"PRECACHE_DONE:{n_ok}/{len(fetch_list)}")
        return

    data = fetch_all(fetch_list, use_cache=use_cache)

    spy_raw     = _market_hours(data.get("SPY", []))
    spy_day_map = _group_by_day(spy_raw)

    all_ts = [b[0] for bars in data.values() for b in bars]
    if not all_ts:
        print("No data returned. Check IBKR connection.")
        return

    from datetime import date as _date
    min_dt    = datetime.fromtimestamp(min(all_ts), tz=_ET).date()
    max_dt    = datetime.fromtimestamp(max(all_ts), tz=_ET).date()
    span      = (max_dt - min_dt).days
    oos_start = min_dt + timedelta(days=int(span * 0.67))

    print(f"\n Date range : {min_dt} → {max_dt}  ({span} days)")
    print(f" IS cutoff  : {min_dt} → {oos_start - timedelta(days=1)}")
    print(f" OOS period : {oos_start} → {max_dt}")
    t_fetch = time.time() - t0
    print(f"\n Fetch done in {t_fetch:.0f}s")
    print(f" Running walk-forward on {len(symbols)} symbols…")
    t1 = time.time()

    all_trades = []
    for sym in symbols:
        bars = data.get(sym, [])
        if not bars:
            continue
        t = run_symbol(sym, bars, spy_day_map, threshold, oos_start, enable_puts)
        all_trades.extend(t)
        if t:
            oos_n = sum(1 for x in t if x["oos"])
            print(f"   {sym:<6} {len(t):>4} trades  ({oos_n} OOS)")

    if not all_trades:
        print("\nNo trades generated. Try lowering --threshold.")
        if args.json:
            import json as _json
            _real_stdout.write(_json.dumps({
                "no_trades": True,
                "reason": f"No trades generated at threshold {threshold:.0f}. Lower --threshold or add more symbols.",
                "meta": {
                    "symbols":   len(symbols),
                    "threshold": threshold,
                    "date_range": f"{min_dt} to {max_dt}",
                    "note": "SPY/QQQ are regime tickers — they rarely generate intraday signals. Use a stock universe instead.",
                }
            }))
            _real_stdout.flush()
        return

    is_t  = [t for t in all_trades if not t["oos"]]
    oos_t = [t for t in all_trades if t["oos"]]

    print(f"\n Total trades: {len(all_trades)}   IS: {len(is_t)}   OOS: {len(oos_t)}")

    print(f"\n{'─'*72}\n OVERALL\n{'─'*72}")
    _row("In-sample",     is_t)
    _row("Out-of-sample", oos_t)
    _row("Combined",      all_trades)

    grade_order = ["S","A","B","C"]
    if args.grade:
        grade_order = grade_order[:grade_order.index(args.grade)+1]
    print(f"\n{'─'*72}\n OOS BY GRADE\n{'─'*72}")
    for g in grade_order:
        _row(f"Grade {g}", [t for t in oos_t if t["grade"] == g])

    print(f"\n{'─'*72}\n OOS BY REGIME\n{'─'*72}")
    _row("Bull (CALL only)", [t for t in oos_t if t.get("regime") == "bull"])
    _row("Bear (PUT only)",  [t for t in oos_t if t.get("regime") == "bear"])

    print(f"\n{'─'*72}\n OOS BY DIRECTION\n{'─'*72}")
    _row("CALL", [t for t in oos_t if t["direction"] == "CALL"])
    _row("PUT",  [t for t in oos_t if t["direction"] == "PUT"])

    print(f"\n{'─'*72}\n OOS BY SIGNAL HOUR (ET)\n{'─'*72}")
    for h in range(10, 16):
        ht = [t for t in oos_t if t["hour"] == h]
        if ht:
            _row(f"{h}:xx ET", ht)

    print(f"\n{'─'*72}\n OOS BY EXIT TYPE\n{'─'*72}")
    for ex in ["target", "stop", "timeout"]:
        _row(ex, [t for t in oos_t if t["exit"] == ex])

    print(f"\n{'─'*72}\n OOS TOP SYMBOLS (by trade count)\n{'─'*72}")
    sym_groups = defaultdict(list)
    for t in oos_t:
        sym_groups[t["symbol"]].append(t)
    for sym, st in sorted(sym_groups.items(), key=lambda x: -len(x[1]))[:args.top]:
        _row(sym, st)

    oos_s = compute_stats(oos_t)
    print(f"\n{'='*72}")
    if oos_s:
        wr, pf, sharpe = oos_s["win_rate"], oos_s["pf"], oos_s["sharpe"]
        be_wr = ATR_STOP_MULT / (ATR_STOP_MULT + ATR_TARGET_MULT) * 100
        if pf >= 1.3 and sharpe >= 1.5:
            verdict = f"PASS — edge confirmed OOS  (PF {pf:.2f}  Sharpe {sharpe:.2f}  WR {wr:.1f}%  BE={be_wr:.0f}%)"
        elif pf >= 1.0 and sharpe >= 0:
            verdict = f"BORDERLINE — positive edge, grow sample  (PF {pf:.2f}  Sharpe {sharpe:.2f}  WR {wr:.1f}%)"
        else:
            verdict = f"FAIL — no confirmed OOS edge  (PF {pf:.2f}  Sharpe {sharpe:.2f}  WR {wr:.1f}%)"
        print(f" VERDICT: {verdict}")
        print(f" OOS MaxDD={oos_s['max_dd']:.2f}%  AvgP={oos_s['avg_pnl']:+.3f}%/trade  n={oos_s['n']}")
    elapsed = time.time() - t0
    print(f" Total runtime: {int(elapsed//60)}m {int(elapsed%60)}s  (fetch {t_fetch:.0f}s + walk {time.time()-t1:.0f}s)")
    print("=" * 72)

    if args.json:
        import json as _json, sys as _sys
        # Build per-symbol OOS summary
        sym_rows = []
        for sym, st in sorted(sym_groups.items(), key=lambda x: -len(x[1])):
            s = compute_stats(st)
            if not s: continue
            sym_rows.append({
                "symbol":   sym,
                "n":        s["n"],
                "wr":       round(s["win_rate"], 1),
                "avg_pnl":  round(s["avg_pnl"], 3),
                "pf":       round(s["pf"], 2),
                "sharpe":   round(s["sharpe"], 2),
                "max_dd":   round(s["max_dd"], 2),
            })
        oos_s = compute_stats(oos_t)
        is_s  = compute_stats(is_t)
        out = {
            "meta": {
                "symbols":   len(symbols),
                "threshold": threshold,
                "date_range": f"{min_dt} to {max_dt}",
                "oos_start":  str(oos_start),
                "elapsed_s":  round(elapsed, 1),
            },
            "is":  {k: round(v, 3) if isinstance(v, float) else v for k, v in (is_s or {}).items()},
            "oos": {k: round(v, 3) if isinstance(v, float) else v for k, v in (oos_s or {}).items()},
            "by_symbol": sym_rows,
            "by_grade": {
                g: (lambda s: {k: round(v,3) if isinstance(v,float) else v for k,v in s.items()})(
                    compute_stats([t for t in oos_t if t["grade"] == g]) or {}
                ) for g in ["S","A","B","C"]
            },
            "by_direction": {
                d: (lambda s: {k: round(v,3) if isinstance(v,float) else v for k,v in s.items()})(
                    compute_stats([t for t in oos_t if t["direction"] == d]) or {}
                ) for d in ["CALL","PUT"]
            },
        }
        _real_stdout.write(_json.dumps(out))
        _real_stdout.flush()


if __name__ == "__main__":
    main()
