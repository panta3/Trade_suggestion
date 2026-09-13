#!/usr/bin/env python3
"""
scripts/swing_backtest.py — Swing trade backtest (next-day open entry, 5-day hold)

Generates intraday signals using the same scoring engine as intraday_backtest_2yr.py,
then simulates entering at the NEXT trading day's open and holding up to MAX_HOLD_DAYS.

Also simulates OTM option P&L using Black-Scholes:
  - Entry: 1-strike OTM call/put, 10 DTE at signal time
  - Exit: same strike, remaining DTE at exit time (stock P&L drives moneyness)
  - IV held constant (ATR-derived) — conservative estimate

Walk-forward split: first 67% in-sample, last 33% out-of-sample.

Usage:
    python3 scripts/swing_backtest.py
    python3 scripts/swing_backtest.py --symbols NVDA AAPL MSFT AMD
    python3 scripts/swing_backtest.py --hold 7 --threshold 90
    python3 scripts/swing_backtest.py --dte 14
"""

import sys, os, argparse, math, pickle, statistics
from datetime import datetime, timedelta
from collections import defaultdict

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
    _grade,
    _atm_strike,
)

# ── Config ────────────────────────────────────────────────────────────────────
SLIPPAGE_PCT   = 0.10   # round-trip stock slippage %
MAX_HOLD_DAYS  = 5      # close trade after N trading days if not stopped/targeted
OPTION_DTE     = 10     # DTE at entry for option simulation
PRIMARY_WINDOW = 130    # rolling 15-min bars for compute_indicators
HTF_AGG        = 4      # 15-min bars → 1H (4×15 = 60 min)
RISK_FREE      = 0.05

_CACHE_DIR = os.path.join(ROOT, "scripts", "bar_cache")


# ── Black-Scholes helpers ─────────────────────────────────────────────────────
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))

def _bs_call(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)

def _bs_put(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

def _otm_strike(price, direction):
    if price < 20:    inc = 0.5
    elif price < 50:  inc = 1.0
    elif price < 200: inc = 2.5
    else:             inc = 5.0
    atm = _atm_strike(price)
    return round(atm + inc if direction == "CALL" else atm - inc, 2)

def _option_pnl(entry_price, exit_price, direction, entry_dte, exit_dte, atr):
    """Simulate OTM option P&L using B-S with ATR-derived IV."""
    sigma = max(0.20, min((atr / entry_price) * math.sqrt(252), 2.0))
    K = _otm_strike(entry_price, direction)
    T_entry = entry_dte / 365.0
    T_exit  = max(exit_dte, 0) / 365.0
    if direction == "CALL":
        opt_entry = _bs_call(entry_price, K, T_entry, RISK_FREE, sigma)
        opt_exit  = _bs_call(exit_price,  K, T_exit,  RISK_FREE, sigma)
    else:
        opt_entry = _bs_put(entry_price, K, T_entry, RISK_FREE, sigma)
        opt_exit  = _bs_put(exit_price,  K, T_exit,  RISK_FREE, sigma)
    if opt_entry <= 0.01:
        return None  # option essentially worthless at entry — skip
    return round((opt_exit - opt_entry) / opt_entry * 100, 2)


# ── Data fetching ─────────────────────────────────────────────────────────────
def _cache_key(symbol):
    today = datetime.now().strftime("%Y%m%d")
    return os.path.join(_CACHE_DIR, f"{symbol.upper()}_15m_{today}.pkl")

def _fetch_15min(symbol, use_cache=True):
    path = _cache_key(symbol)
    if use_cache and os.path.exists(path):
        with open(path, "rb") as f:
            return pickle.load(f)
    import ibkr_data as _id
    bars = _id.get_intraday_bars(symbol, interval="15m", period="730d")
    if use_cache and bars:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(bars, f)
    return bars

def fetch_all(symbols, use_cache=True):
    result = {}
    for i, sym in enumerate(symbols, 1):
        print(f"  [{i}/{len(symbols)}] {sym}", end="\r", flush=True)
        bars = _fetch_15min(sym, use_cache)
        if bars and len(bars) >= PRIMARY_WINDOW:
            result[sym] = bars
    print()
    return result


# ── Signal generation (same as intraday_backtest_2yr) ─────────────────────────
def _agg_htf(bars_15m):
    """Aggregate 4 × 15-min bars into 1 × 60-min bar."""
    result = []
    for i in range(0, len(bars_15m) - HTF_AGG + 1, HTF_AGG):
        chunk = bars_15m[i:i + HTF_AGG]
        ts    = chunk[-1][0]
        o     = chunk[0][1]
        h     = max(b[2] for b in chunk)
        l     = min(b[3] for b in chunk)
        c     = chunk[-1][4]
        v     = sum(b[5] for b in chunk)
        result.append((ts, o, h, l, c, v))
    return result

def _bar_et(ts):
    return datetime.fromtimestamp(ts, tz=_ET)

def _is_trading_hours(ts):
    dt = _bar_et(ts)
    h, m = dt.hour, dt.minute
    return (h > 11 or (h == 11 and m >= 0)) and (h < 15 or (h == 15 and m < 30))

def generate_signals(all_bars, threshold, regime_bull=True):
    """
    Walk bar-by-bar, generate signals at each bar using only past data.
    Returns list of signal dicts with bar timestamp, symbol, direction, price, atr.
    """
    signals = []
    cooldowns = {}   # symbol → last signal bar index

    for symbol, bars in all_bars.items():
        htf_all  = _agg_htf(bars)
        htf_idx  = 0

        for i in range(PRIMARY_WINDOW, len(bars)):
            ts = bars[i][0]
            if not _is_trading_hours(ts):
                continue

            window = bars[i - PRIMARY_WINDOW:i + 1]

            # Advance HTF pointer to match current time
            while htf_idx < len(htf_all) - 1 and htf_all[htf_idx][0] <= ts:
                htf_idx += 1
            htf_window = htf_all[max(0, htf_idx - 13):htf_idx]

            ind = compute_indicators(window, htf_window)
            if ind is None or not ind["in_session"]:
                continue
            if ind["rel_vol"] < 0.5:
                continue
            if ind["atr"] / ind["price"] < 0.005:
                continue

            bull_raw, bear_raw, bull_r, bear_r, bull_w, bear_w = score_intraday(ind)
            bull_prob = min(bull_raw, 100)
            bear_prob = min(bear_raw, 100)

            if regime_bull:
                bear_prob = 0.0
            else:
                bull_prob = 0.0

            if bull_prob >= threshold and bull_prob > bear_prob:
                direction, score = "CALL", bull_prob
            elif bear_prob >= threshold and bear_prob > bull_prob:
                direction, score = "PUT", bear_prob
            else:
                continue

            # Cooldown: one signal per symbol per day
            bar_date = _bar_et(ts).date()
            if cooldowns.get(symbol) == bar_date:
                continue
            cooldowns[symbol] = bar_date

            price = ind["price"]
            atr   = ind["atr"]
            stop   = round(price - atr * ATR_STOP_MULT, 4)   if direction == "CALL" else round(price + atr * ATR_STOP_MULT, 4)
            target = round(price + atr * ATR_TARGET_MULT, 4) if direction == "CALL" else round(price - atr * ATR_TARGET_MULT, 4)

            signals.append({
                "symbol":    symbol,
                "direction": direction,
                "grade":     _grade(score),
                "score":     round(score, 1),
                "ts":        ts,
                "date":      bar_date,
                "bar_idx":   i,
                "price":     price,
                "atr":       atr,
                "stop":      stop,
                "target":    target,
            })

    return signals


# ── Swing trade simulation ────────────────────────────────────────────────────
def simulate_swing(signals, all_bars, hold_days, option_dte):
    """
    For each signal, enter at NEXT trading day's open.
    Exit when stop/target hit or after hold_days trading days.
    Returns list of trade result dicts.
    """
    # Build a date → bars lookup per symbol
    bars_by_sym_date = defaultdict(lambda: defaultdict(list))
    for sym, bars in all_bars.items():
        for b in bars:
            d = _bar_et(b[0]).date()
            bars_by_sym_date[sym][d].append(b)

    results = []

    for sig in signals:
        sym   = sig["symbol"]
        entry_date = sig["date"]

        # Find all trading days after signal date
        all_dates = sorted(bars_by_sym_date[sym].keys())
        try:
            signal_date_idx = all_dates.index(entry_date)
        except ValueError:
            continue

        # Next trading day
        if signal_date_idx + 1 >= len(all_dates):
            continue
        entry_day = all_dates[signal_date_idx + 1]
        entry_bars = bars_by_sym_date[sym][entry_day]
        if not entry_bars:
            continue

        # Entry = open of next day's first bar
        entry_price = entry_bars[0][1]  # open
        direction   = sig["direction"]
        stop        = sig["stop"]
        target      = sig["target"]

        # Adjust stop/target to new entry price maintaining same ATR distance
        atr = sig["atr"]
        stop   = round(entry_price - atr * ATR_STOP_MULT,   4) if direction == "CALL" else round(entry_price + atr * ATR_STOP_MULT, 4)
        target = round(entry_price + atr * ATR_TARGET_MULT, 4) if direction == "CALL" else round(entry_price - atr * ATR_TARGET_MULT, 4)

        # Apply entry slippage
        if direction == "CALL":
            entry_price *= (1 + SLIPPAGE_PCT / 200)
        else:
            entry_price *= (1 - SLIPPAGE_PCT / 200)

        # Walk forward up to hold_days trading days
        exit_price  = None
        exit_reason = "hold_expire"
        days_held   = 0

        future_dates = all_dates[signal_date_idx + 1: signal_date_idx + 1 + hold_days + 1]
        for day in future_dates:
            day_bars = bars_by_sym_date[sym][day]
            for b in day_bars:
                hi, lo = b[2], b[3]
                if direction == "CALL":
                    if lo <= stop:
                        exit_price  = stop
                        exit_reason = "stop"
                        break
                    if hi >= target:
                        exit_price  = target
                        exit_reason = "target"
                        break
                else:
                    if hi >= stop:
                        exit_price  = stop
                        exit_reason = "stop"
                        break
                    if lo <= target:
                        exit_price  = target
                        exit_reason = "target"
                        break
            if exit_price is not None:
                break
            days_held += 1
            if days_held >= hold_days:
                # Close at last bar's close
                exit_price  = day_bars[-1][4] if day_bars else entry_price
                exit_reason = "hold_expire"
                break

        if exit_price is None:
            exit_price  = entry_price
            exit_reason = "no_data"

        # Apply exit slippage
        if direction == "CALL":
            exit_price *= (1 - SLIPPAGE_PCT / 200)
        else:
            exit_price *= (1 + SLIPPAGE_PCT / 200)

        pnl_pct = (exit_price - entry_price) / entry_price * 100
        if direction == "PUT":
            pnl_pct = -pnl_pct

        # Option P&L simulation
        exit_dte    = max(option_dte - days_held, 0)
        opt_pnl_pct = _option_pnl(entry_price, exit_price, direction, option_dte, exit_dte, atr)

        results.append({
            "symbol":      sym,
            "direction":   direction,
            "grade":       sig["grade"],
            "score":       sig["score"],
            "signal_date": str(sig["date"]),
            "entry_date":  str(entry_day),
            "entry":       round(entry_price, 4),
            "exit":        round(exit_price, 4),
            "stop":        round(stop, 4),
            "target":      round(target, 4),
            "pnl_pct":     round(pnl_pct, 3),
            "opt_pnl_pct": opt_pnl_pct,
            "days_held":   days_held,
            "exit_reason": exit_reason,
            "win":         pnl_pct > 0,
        })

    return results


# ── Reporting ─────────────────────────────────────────────────────────────────
def report(label, trades):
    if not trades:
        print(f"  {label}: no trades")
        return
    wins     = [t for t in trades if t["win"]]
    losses   = [t for t in trades if not t["win"]]
    pnls     = [t["pnl_pct"] for t in trades]
    opt_pnls = [t["opt_pnl_pct"] for t in trades if t["opt_pnl_pct"] is not None]

    wr   = len(wins) / len(trades) * 100
    avg  = sum(pnls) / len(pnls)
    avg_w = sum(t["pnl_pct"] for t in wins)  / max(len(wins), 1)
    avg_l = sum(t["pnl_pct"] for t in losses) / max(len(losses), 1)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss   = abs(sum(p for p in pnls if p < 0))
    pf           = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    targets = sum(1 for t in trades if t["exit_reason"] == "target")
    stops   = sum(1 for t in trades if t["exit_reason"] == "stop")
    expires = sum(1 for t in trades if t["exit_reason"] == "hold_expire")

    print(f"\n  ── {label} ({len(trades)} trades) ──")
    print(f"  WR: {wr:.1f}%   Avg P&L: {avg:+.2f}%   PF: {pf:.2f}")
    print(f"  Avg winner: {avg_w:+.2f}%   Avg loser: {avg_l:+.2f}%")
    print(f"  Exits — target: {targets}  stop: {stops}  expire: {expires}")
    if opt_pnls:
        avg_opt = sum(opt_pnls) / len(opt_pnls)
        opt_wr  = sum(1 for p in opt_pnls if p > 0) / len(opt_pnls) * 100
        print(f"  Option P&L (OTM {args.dte}d): avg {avg_opt:+.1f}%   WR {opt_wr:.1f}%   n={len(opt_pnls)}")

    # By hold duration
    by_days = defaultdict(list)
    for t in trades:
        by_days[t["days_held"]].append(t["pnl_pct"])
    print(f"  By days held:", end="")
    for d in sorted(by_days):
        avg_d = sum(by_days[d]) / len(by_days[d])
        print(f"  {d}d:{avg_d:+.1f}%({len(by_days[d])})", end="")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--threshold", type=float, default=90.0)
    parser.add_argument("--hold",      type=int,   default=MAX_HOLD_DAYS, help="max trading days to hold")
    parser.add_argument("--dte",       type=int,   default=OPTION_DTE,    help="option DTE at entry")
    parser.add_argument("--no-cache",  action="store_true")
    args = parser.parse_args()

    syms = args.symbols or [s for s in DEFAULT_SYMBOLS if not s.endswith(".TO")]
    print(f"\nSwing Backtest  |  symbols={len(syms)}  threshold={args.threshold}  hold={args.hold}d  dte={args.dte}")
    print(f"Entry: next trading day open  |  Exit: target/stop/{args.hold}-day expire")
    print(f"Option: 1-strike OTM, {args.dte} DTE at entry\n")

    print("Fetching 15-min bars (2 years)…")
    all_bars = fetch_all(syms, use_cache=not args.no_cache)
    print(f"Loaded {len(all_bars)} symbols with sufficient history\n")

    print("Generating signals…")
    signals = generate_signals(all_bars, threshold=args.threshold)
    print(f"Total signals: {len(signals)}")

    if not signals:
        print("No signals found — try lowering --threshold")
        sys.exit(0)

    # Walk-forward split
    all_dates = sorted({s["date"] for s in signals})
    split_idx = int(len(all_dates) * 0.67)
    split_date = all_dates[split_idx]
    is_sigs  = [s for s in signals if s["date"] <  split_date]
    oos_sigs = [s for s in signals if s["date"] >= split_date]
    print(f"Walk-forward split: IS n={len(is_sigs)}  OOS n={len(oos_sigs)}  split={split_date}\n")

    print("Simulating swing trades…")
    is_trades  = simulate_swing(is_sigs,  all_bars, args.hold, args.dte)
    oos_trades = simulate_swing(oos_sigs, all_bars, args.hold, args.dte)

    print("\n=== IN-SAMPLE ===")
    report("All grades", is_trades)
    for g in ["S", "A", "B"]:
        g_trades = [t for t in is_trades if t["grade"] == g]
        if g_trades:
            report(f"Grade {g}", g_trades)

    print("\n=== OUT-OF-SAMPLE (what matters) ===")
    report("All grades", oos_trades)
    for g in ["S", "A", "B"]:
        g_trades = [t for t in oos_trades if t["grade"] == g]
        if g_trades:
            report(f"Grade {g}", g_trades)

    # Symbol breakdown (OOS only)
    if oos_trades:
        print("\n=== OOS BY SYMBOL ===")
        by_sym = defaultdict(list)
        for t in oos_trades:
            by_sym[t["symbol"]].append(t)
        rows = []
        for sym, trades in by_sym.items():
            if len(trades) < 2:
                continue
            wr  = sum(1 for t in trades if t["win"]) / len(trades) * 100
            avg = sum(t["pnl_pct"] for t in trades) / len(trades)
            rows.append((sym, len(trades), wr, avg))
        rows.sort(key=lambda x: -x[3])
        for sym, n, wr, avg in rows[:15]:
            bar = "█" * max(0, int(avg * 3))
            print(f"  {sym:<8} n={n:>2}  WR={wr:.0f}%  avg={avg:+.2f}%  {bar}")

    print()
