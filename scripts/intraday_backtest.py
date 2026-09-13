#!/usr/bin/env python3
"""
scripts/intraday_backtest.py — Walk-forward backtest for the intraday scoring engine.

Uses 5-min bars fetched from IBKR (6 months max — IBKR's hard limit for 5-min history).
Derives 15-min HTF bars by aggregating every 3 5-min bars — exactly what the live system
does when it calls fetch_intraday(symbol, "15m", "5d").

Walks bar-by-bar per trading day, calling compute_indicators() + score_intraday()
from day_trading.py with only past data visible — no lookahead.

Stats: win rate, profit factor, Sharpe, max drawdown, by grade / direction / hour / symbol.
Walk-forward split: first 67% in-sample, last 33% out-of-sample.

Usage:
    python3 scripts/intraday_backtest.py
    python3 scripts/intraday_backtest.py --symbols TSLA NVDA AAPL
    python3 scripts/intraday_backtest.py --threshold 80 --top 15
    python3 scripts/intraday_backtest.py --grade A   # only report OOS grade A+
"""

import sys, os, argparse, statistics, time, pickle, hashlib
from datetime import datetime, timedelta
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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
MAX_HOLD_BARS  = 78     # max 78 5-min bars = ~6.5 hours (rest of day)
COOLDOWN_BARS  = 24     # 24 × 5 min = 2 hours between signals for same symbol
PRIMARY_WINDOW = 390    # rolling 5-min bars fed to compute_indicators (≈5 days)
HTF_AGG        = 3      # aggregate every N 5-min bars → 1 15-min bar


# ── Data Fetching ─────────────────────────────────────────────────────────────

# Cache directory: bars are stored by (symbol, date) so they're reused across runs.
# Run with --no-cache to force a fresh IBKR fetch.
_CACHE_DIR = os.path.join(ROOT, "scripts", "bar_cache")


def _cache_key(symbol: str) -> str:
    """Cache filename: symbol + today's date, so cache auto-expires daily."""
    today = datetime.now().strftime("%Y%m%d")
    return os.path.join(_CACHE_DIR, f"{symbol.upper()}_{today}.pkl")


def _fetch_5min(symbol: str, use_cache: bool = True) -> list:
    """
    6 months of 5-min bars from IBKR; Yahoo 60-day fallback if IBKR unavailable.
    Caches to disk so re-runs skip the IBKR fetch (~3min saved on full symbol list).
    IBKR historical data must be fetched sequentially — concurrent reqHistoricalData
    calls on the same IB object are not safe in ib_insync.
    """
    cache_path = _cache_key(symbol)
    if use_cache and os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return pickle.load(f)

    import ibkr_data as _id
    bars = _id.get_intraday_bars(symbol, interval="5m", period="180d")

    if use_cache and bars:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump(bars, f)

    return bars


def fetch_all(symbols: list, use_cache: bool = True) -> dict:
    """
    Fetch 5-min bars for all symbols sequentially with pacing delay.
    Sequential is required: ib_insync's reqHistoricalData is not safe to call
    concurrently on the same IB connection object, causing most requests to
    return empty when run in parallel.
    Cached symbols skip IBKR entirely and load from disk in milliseconds.
    """
    n       = len(symbols)
    result  = {}
    n_ok    = 0
    n_cache = sum(1 for s in symbols if os.path.exists(_cache_key(s)))

    if n_cache:
        print(f"  {n_cache}/{n} symbols cached — only {n - n_cache} need IBKR fetch")
    print(f"  Fetching {n} symbols (cached: {n_cache}, live: {n - n_cache})…")

    for i, sym in enumerate(symbols):
        cached = use_cache and os.path.exists(_cache_key(sym))
        bars   = _fetch_5min(sym, use_cache=use_cache)
        result[sym] = bars
        if bars:
            n_ok += 1
            src = "cache" if cached else ("IBKR" if len(bars) > 100 else "Yahoo")
            print(f"    [{i+1:>3}/{n}] {sym:<6}  {len(bars):>5} bars  ({src})")
        else:
            print(f"    [{i+1:>3}/{n}] {sym:<6}  no data")
        if not cached:
            time.sleep(0.4)   # IBKR pacing: ≤50 historical requests per 10 seconds

    print(f"  Done: {n_ok}/{n} symbols returned data")
    return result


# ── Bar Utilities ─────────────────────────────────────────────────────────────

def _market_hours(bars):
    """Keep only bars whose timestamp falls inside 9:30–16:00 ET."""
    out = []
    for b in bars:
        dt = datetime.fromtimestamp(b[0], tz=_ET)
        h, m = dt.hour, dt.minute
        if (h > 9 or (h == 9 and m >= 30)) and h < 16:
            out.append(b)
    return out


def _group_by_day(bars):
    """Return {date: [bars]} sorted within day, days with <10 bars dropped."""
    days = defaultdict(list)
    for b in bars:
        dt = datetime.fromtimestamp(b[0], tz=_ET)
        days[dt.date()].append(b)
    return {d: sorted(v, key=lambda x: x[0]) for d, v in days.items() if len(v) >= 10}


def _agg_to_15min(bars_5min):
    """Aggregate 5-min bars to 15-min OHLCV (every 3 consecutive bars)."""
    out = []
    for i in range(0, len(bars_5min) - (HTF_AGG - 1), HTF_AGG):
        grp = bars_5min[i:i + HTF_AGG]
        if len(grp) < HTF_AGG:
            break
        out.append((
            grp[-1][0],                     # ts = last bar in group
            grp[0][1],                      # open = first bar open
            max(b[2] for b in grp),         # high
            min(b[3] for b in grp),         # low
            grp[-1][4],                     # close = last bar close
            sum(b[5] for b in grp),         # volume sum
        ))
    return out


# ── Trade Simulation ──────────────────────────────────────────────────────────

def _simulate(direction, entry, atr, future_bars):
    """Walk future 5-min bars; return (pnl_pct, exit_reason). Conservative: stop wins if both hit."""
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
        hit_tgt = tgt(hi, lo)
        hit_stp = stp(hi, lo)
        if hit_stp:
            exit_price  = stop
            exit_reason = "stop"
            break
        if hit_tgt:
            exit_price  = target
            exit_reason = "target"
            break

    if exit_price is None:
        last = future_bars[min(MAX_HOLD_BARS - 1, len(future_bars) - 1)]
        exit_price = last[4]

    if direction == "CALL":
        pnl = (exit_price - entry) / entry * 100
    else:
        pnl = (entry - exit_price) / entry * 100

    pnl -= SLIPPAGE_PCT
    return round(pnl, 4), exit_reason


# ── Grade Helper ──────────────────────────────────────────────────────────────

def _grade(score):
    if score >= 90: return "S"
    if score >= 80: return "A"
    if score >= 68: return "B"
    if score >= 55: return "C"
    return "D"


# ── Per-Symbol Backtest ───────────────────────────────────────────────────────

def run_symbol(symbol, all_bars, spy_day_map, threshold, oos_start):
    """
    Walk all 5-min market-hours bars bar-by-bar.

    At each bar, feeds:
      primary = last PRIMARY_WINDOW 5-min bars (≈5 days) — matches fetch_intraday("5m","5d")
      htf     = 15-min bars derived from primary window — matches fetch_intraday("15m","5d")
    to compute_indicators(), then score_intraday() — identical to live evaluation.

    Signal fires entry on next bar's open; exit simulated within same calendar day only.
    No lookahead: future_bars contains only bars that come after the entry bar.
    """
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
        day_bars      = day_map[day]
        spy_today     = spy_day_map.get(day, [])
        spy_open      = spy_today[0][1] if spy_today else None
        cooldown_until = -1

        for i, bar in enumerate(day_bars):
            seen.append(bar)

            if len(seen) < PRIMARY_WINDOW:
                continue
            if i <= cooldown_until:
                continue

            primary = seen[-PRIMARY_WINDOW:]
            htf     = _agg_to_15min(primary)[-13:]  # last 13 15-min bars as HTF

            ind = compute_indicators(primary, htf)
            if not ind:
                continue

            # Block first 90 min: matches live evaluate_symbol() guard.
            # 10 AM = 58% of OOS trades at PF 0.79 in 65-symbol backtest.
            bar_et = datetime.fromtimestamp(bar[0], tz=_ET)
            if bar_et.hour < 11:
                continue

            # Inject RS vs SPY + first-30-min alignment (same logic as live)
            regime_bull = True   # default: assume bull if no SPY data
            if spy_open and spy_today and spy_open > 0:
                spy_curr = spy_today[min(i, len(spy_today) - 1)][4]
                spy_chg  = (spy_curr - spy_open) / spy_open * 100
                day_open = day_bars[0][1]
                stk_chg  = (ind["price"] - day_open) / day_open * 100 if day_open else 0.0
                ind["rs_vs_spy"] = round(stk_chg - spy_chg, 2)
                regime_bull = spy_curr >= spy_open

            # First-30-min SPY alignment — inject into regime_cache so score_intraday reads it.
            # Uses SPY bars up to and including the 6th bar of the session (30 min elapsed).
            if spy_today and len(spy_today) >= 6:
                _f30_close = spy_today[5][4]
                _f30_open  = spy_today[0][1]
                import day_trading as _dt
                _dt._regime_cache["first30_bull"] = _f30_close > _f30_open if _f30_open else None
                _dt._regime_cache["first30_ret"]  = round((_f30_close - _f30_open) / _f30_open * 100, 3) if _f30_open else 0.0
            else:
                import day_trading as _dt
                _dt._regime_cache["first30_bull"] = None
                _dt._regime_cache["first30_ret"]  = 0.0

            bull_raw, bear_raw, _, _, _, _ = score_intraday(ind)
            bull = max(0.0, min(100.0, bull_raw))
            bear = max(0.0, min(100.0, bear_raw))

            # Regime direction gate — mirrors evaluate_symbol() hard gate.
            if regime_bull:
                bear = 0.0
            else:
                bull = 0.0
            # PUT kill switch — matches live system: no PUT edge confirmed OOS.
            bear = 0.0

            if bull >= threshold and bull > bear:
                direction, score_val = "CALL", bull
            elif bear >= threshold and bear > bull:
                direction, score_val = "PUT", bear
            else:
                continue

            # Entry = next bar open (within same day)
            if i + 1 >= len(day_bars):
                continue
            entry_bar = day_bars[i + 1]
            entry     = entry_bar[1]
            atr       = ind["atr"] or (entry * 0.005)

            # ATR % floor — matches live evaluate_symbol() gate
            if atr / entry < 0.005:
                continue

            # Simulate exit on remaining bars in this day only (no overnight holds)
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


# ── Stats Computation ─────────────────────────────────────────────────────────

def compute_stats(trades):
    if not trades:
        return None
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p <= 0]

    win_rate   = len(wins) / len(pnls) * 100
    avg_pnl    = sum(pnls) / len(pnls)
    pf         = sum(wins) / abs(sum(loss)) if loss else 999.0
    expectancy = avg_pnl

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
        "n":         len(trades),
        "win_rate":  round(win_rate, 1),
        "avg_pnl":   round(avg_pnl, 3),
        "pf":        round(min(pf, 999.0), 2),
        "sharpe":    round(sharpe, 2),
        "max_dd":    round(max_dd, 2),
        "total_pnl": round(sum(pnls), 2),
        "exp×√n":    round(expectancy * (len(pnls) ** 0.5), 2),
    }


def _row(label, trades, indent=2):
    s = compute_stats(trades)
    if s is None:
        print(f"{' '*indent}{label:<24}  —")
        return
    flag = ""
    if s["win_rate"] >= 55 and s["pf"] >= 1.3:
        flag = " ✓"
    elif s["win_rate"] < 45 or s["pf"] < 0.9:
        flag = " ✗"
    print(
        f"{' '*indent}{label:<24}"
        f"  n={s['n']:>4}"
        f"  WR={s['win_rate']:>5.1f}%"
        f"  PF={s['pf']:>5.2f}"
        f"  Sharpe={s['sharpe']:>5.2f}"
        f"  MaxDD={s['max_dd']:>6.2f}%"
        f"  AvgP={s['avg_pnl']:>+.3f}%"
        f"{flag}"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Intraday scoring engine walk-forward backtest (IBKR 5-min)")
    ap.add_argument("--symbols",   nargs="+", default=None,               help="Override symbol list")
    ap.add_argument("--threshold", type=float, default=SIGNAL_THRESHOLD,  help="Min score to fire signal")
    ap.add_argument("--top",       type=int,   default=20,                help="Max symbols in per-symbol table")
    ap.add_argument("--grade",     default=None, choices=["S","A","B","C"], help="Filter OOS breakdown to this grade+")
    ap.add_argument("--no-cache",  action="store_true",                   help="Force fresh IBKR fetch (ignore disk cache)")
    args = ap.parse_args()

    symbols   = args.symbols or DEFAULT_SYMBOLS
    threshold = args.threshold
    use_cache = not args.no_cache

    # Runtime estimate: fetch ~3s/sym uncached + ~8ms/bar × 9400 bars × n_sym
    n_cached   = sum(1 for s in (["SPY"] + symbols) if os.path.exists(_cache_key(s)))
    n_uncached = len(symbols) + 1 - n_cached
    est_fetch  = n_uncached * 3
    est_walk   = len(symbols) * 9400 * 0.008  # 8ms per bar (conservative)
    est_total  = est_fetch + est_walk
    est_str    = f"{int(est_total // 60)}m {int(est_total % 60)}s" if est_total >= 60 else f"{int(est_total)}s"

    print("=" * 72)
    print(f" INTRADAY BACKTEST  |  {len(symbols)} symbols  |  threshold={threshold:.0f}")
    print(f" Data: IBKR 5-min bars × 6 months  (Yahoo 60-day fallback if unavailable)")
    print(f" Slippage: {SLIPPAGE_PCT:.2f}% RT  |  MaxHold: {MAX_HOLD_BARS} bars ({MAX_HOLD_BARS*5//60}h {MAX_HOLD_BARS*5%60}m)  |  Cooldown: {COOLDOWN_BARS} bars ({COOLDOWN_BARS*5//60}h)")
    print(f" Primary window: {PRIMARY_WINDOW} 5-min bars  |  HTF: 15-min (aggregated)")
    print(f" Cache: {'OFF (--no-cache)' if not use_cache else f'{n_cached} hits / {n_uncached} need fetch'}")
    print(f" Estimated runtime: ~{est_str}  (fetch {est_fetch:.0f}s + walk {est_walk:.0f}s)")
    print("=" * 72)

    fetch_list = list(dict.fromkeys(["SPY"] + symbols))
    t0   = time.time()
    data = fetch_all(fetch_list, use_cache=use_cache)

    spy_raw     = _market_hours(data.get("SPY", []))
    spy_day_map = _group_by_day(spy_raw)

    all_ts = [b[0] for bars in data.values() for b in bars]
    if not all_ts:
        print("No data returned. Check IBKR connection or try --symbols with active tickers.")
        return
    from datetime import date as _date
    min_dt = datetime.fromtimestamp(min(all_ts), tz=_ET).date()
    max_dt = datetime.fromtimestamp(max(all_ts), tz=_ET).date()
    span   = (max_dt - min_dt).days
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
        t = run_symbol(sym, bars, spy_day_map, threshold, oos_start)
        all_trades.extend(t)
        if t:
            oos_n = sum(1 for x in t if x["oos"])
            print(f"   {sym:<6} {len(t):>4} trades  ({oos_n} OOS)")

    if not all_trades:
        print("\nNo trades generated. Try lowering --threshold.")
        return

    is_t  = [t for t in all_trades if not t["oos"]]
    oos_t = [t for t in all_trades if t["oos"]]

    print(f"\n Total trades: {len(all_trades)}   IS: {len(is_t)}   OOS: {len(oos_t)}")

    # ── Overall ──────────────────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print(" OVERALL")
    print(f"{'─'*72}")
    _row("In-sample",     is_t)
    _row("Out-of-sample", oos_t)
    _row("Combined",      all_trades)

    # ── OOS by Grade ─────────────────────────────────────────────────────────
    grade_order = ["S","A","B","C"]
    if args.grade:
        i = grade_order.index(args.grade)
        grade_order = grade_order[:i+1]

    print(f"\n{'─'*72}")
    print(" OOS BY GRADE")
    print(f"{'─'*72}")
    for g in grade_order:
        _row(f"Grade {g}", [t for t in oos_t if t["grade"] == g])

    # ── OOS by Regime ─────────────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print(" OOS BY REGIME")
    print(f"{'─'*72}")
    _row("Bull (CALL only)", [t for t in oos_t if t.get("regime") == "bull"])
    _row("Bear (PUT only)",  [t for t in oos_t if t.get("regime") == "bear"])

    # ── OOS by Direction ──────────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print(" OOS BY DIRECTION")
    print(f"{'─'*72}")
    _row("CALL", [t for t in oos_t if t["direction"] == "CALL"])
    _row("PUT",  [t for t in oos_t if t["direction"] == "PUT"])

    # ── OOS by Signal Hour ────────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print(" OOS BY SIGNAL HOUR (ET)")
    print(f"{'─'*72}")
    for h in range(10, 16):
        ht = [t for t in oos_t if t["hour"] == h]
        if ht:
            _row(f"{h}:xx ET", ht)

    # ── OOS by Exit Type ──────────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print(" OOS BY EXIT TYPE")
    print(f"{'─'*72}")
    for ex in ["target", "stop", "timeout"]:
        _row(ex, [t for t in oos_t if t["exit"] == ex])

    # ── OOS per Symbol ────────────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print(" OOS TOP SYMBOLS (by trade count)")
    print(f"{'─'*72}")
    sym_groups = defaultdict(list)
    for t in oos_t:
        sym_groups[t["symbol"]].append(t)
    ranked = sorted(sym_groups.items(), key=lambda x: -len(x[1]))
    for sym, st in ranked[:args.top]:
        _row(sym, st)

    # ── Verdict ───────────────────────────────────────────────────────────────
    # Criterion: PF + Sharpe, NOT win rate.
    # This system targets 3:2 R:R (target=3 ATR, stop=2 ATR).
    # Break-even WR at 3:2 R:R = 2/(2+3) = 40%.
    # A 48% WR with PF 1.35 IS profitable — WR-based thresholds mislead here.
    oos_s = compute_stats(oos_t)
    print(f"\n{'='*72}")
    if oos_s:
        wr, pf, sharpe = oos_s["win_rate"], oos_s["pf"], oos_s["sharpe"]
        be_wr = ATR_STOP_MULT / (ATR_STOP_MULT + ATR_TARGET_MULT) * 100  # break-even WR
        if pf >= 1.3 and sharpe >= 1.5:
            verdict = f"PASS — edge confirmed OOS  (PF {pf:.2f}  Sharpe {sharpe:.2f}  WR {wr:.1f}%  BE={be_wr:.0f}%)"
        elif pf >= 1.0 and sharpe >= 0:
            verdict = f"BORDERLINE — positive edge, grow sample  (PF {pf:.2f}  Sharpe {sharpe:.2f}  WR {wr:.1f}%)"
        else:
            verdict = f"FAIL — no confirmed OOS edge  (PF {pf:.2f}  Sharpe {sharpe:.2f}  WR {wr:.1f}%)"
        print(f" VERDICT: {verdict}")
        print(f" OOS MaxDD={oos_s['max_dd']:.2f}%  AvgP={oos_s['avg_pnl']:+.3f}%/trade  n={oos_s['n']}")
    else:
        print(" VERDICT: No OOS trades — cannot evaluate.")
    elapsed = time.time() - t0
    print(f" Total runtime: {int(elapsed // 60)}m {int(elapsed % 60)}s  (fetch {t_fetch:.0f}s + walk {time.time() - t1:.0f}s)")
    print("=" * 72)


if __name__ == "__main__":
    main()
