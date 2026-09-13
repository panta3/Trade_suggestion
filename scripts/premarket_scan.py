#!/usr/bin/env python3
"""
scripts/premarket_scan.py — Pre-market catalyst scanner.

Run at 9:00–9:20 AM ET before the open. Scans DEFAULT_SYMBOLS for stocks
with significant pre-market gaps and elevated volume, ranks them by catalyst
strength, and prints a "stocks in play" watchlist for the session.

This solves the momentum-without-catalyst problem:
  - A 10% gap + 5× pre-market volume = informed money moved overnight (earnings,
    news, upgrade). By the time your 10 AM CALL signal fires on this stock,
    you're trading confirmed institutional continuation — not detecting random noise.
  - Without this scan you're chasing any EMA cross. With it, you're waiting for
    pattern confirmation on a stock you already know has a reason to move.

Usage:
    python3 scripts/premarket_scan.py
    python3 scripts/premarket_scan.py --min-gap 3 --min-vol 1.5 --top 10
    python3 scripts/premarket_scan.py --symbols AAPL TSLA NVDA MSFT
"""

import sys, os, argparse, json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")

from day_trading import DEFAULT_SYMBOLS
import ibkr_data as _id


def _catalyst_rank(row: dict) -> float:
    """Score for sorting — gap magnitude × volume ratio."""
    return abs(row["gap_pct"]) * row["vol_ratio"]


def scan(symbols: list[str], min_gap: float, min_vol: float) -> list[dict]:
    results = []

    def _fetch(sym):
        try:
            d = _id.get_premarket_gap(sym)
            if not d:
                return
            gap = d.get("gap_pct", 0.0)
            vol = d.get("vol_ratio", 0.0)
            if abs(gap) < min_gap and vol < min_vol:
                return
            results.append({
                "symbol":      sym,
                "gap_pct":     gap,
                "pm_price":    d.get("pm_price"),
                "prev_close":  d.get("prev_close"),
                "pm_volume":   d.get("pm_volume", 0),
                "vol_ratio":   vol,
                "avg_vol_20d": d.get("avg_vol_20d", 0),
            })
        except Exception:
            pass

    print(f"  Scanning {len(symbols)} symbols for pre-market catalyst…", end="", flush=True)
    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(_fetch, symbols))
    print(f" done — {len(results)} candidates")

    results.sort(key=_catalyst_rank, reverse=True)
    return results


def _bar(val: float, max_val: float, width: int = 12) -> str:
    filled = int(min(1.0, abs(val) / max(max_val, 0.01)) * width)
    char   = "▲" if val >= 0 else "▼"
    return char * filled + "·" * (width - filled)


def print_report(rows: list[dict], top: int) -> None:
    now = datetime.now(tz=_ET)
    print()
    print("=" * 72)
    print(f"  PRE-MARKET CATALYST SCAN  —  {now.strftime('%Y-%m-%d  %H:%M ET')}")
    print(f"  Top {min(top, len(rows))} stocks in play for today's session")
    print("=" * 72)

    if not rows:
        print("  No catalyst candidates found. Proceed with DEFAULT_SYMBOLS unfiltered.")
        print("=" * 72)
        return

    max_gap = max(abs(r["gap_pct"]) for r in rows) or 1.0
    max_vol = max(r["vol_ratio"] for r in rows) or 1.0

    print(f"  {'SYMBOL':<7} {'GAP%':>6}  {'GAP STRENGTH':<14} {'VOL RATIO':>9}  {'VOL STRENGTH':<14}  PRIORITY")
    print(f"  {'─'*7} {'─'*6}  {'─'*14} {'─'*9}  {'─'*14}  {'─'*8}")

    for i, r in enumerate(rows[:top]):
        gap_bar = _bar(r["gap_pct"], max_gap)
        vol_bar = _bar(r["vol_ratio"], max_vol)
        gap_str = f"{r['gap_pct']:>+.1f}%"
        vol_str = f"{r['vol_ratio']:>5.1f}×"
        rank = "★★★" if i < 3 else ("★★ " if i < 6 else "★  ")
        print(f"  {r['symbol']:<7} {gap_str}  {gap_bar:<14} {vol_str}  {vol_bar:<14}  {rank}")

    print()
    print("  WATCHLIST (copy-paste):")
    syms = [r["symbol"] for r in rows[:top]]
    print("  " + "  ".join(syms))
    print()
    print("  WHAT THIS MEANS:")
    print("  ─ Gap UP  + high vol → earnings beat, upgrade, or sector flow. Watch for")
    print("    CALL setups after 10 AM when price holds above VWAP.")
    print("  ─ Gap DOWN + high vol → earnings miss or macro shock. Watch for PUT")
    print("    setups if price fails to reclaim the gap by 10:30.")
    print("  ─ High vol without gap → block trade or sector rotation. Less reliable.")
    print()
    print("  SPEED NOTE:")
    print("  You identified these stocks at 9:15 AM. By 10 AM when your scanner")
    print("  fires a signal on one of them, you're not chasing noise — you're")
    print("  confirming a move with a known reason. That's the edge, not being faster.")
    print("=" * 72)

    return syms


def main():
    ap = argparse.ArgumentParser(description="Pre-market catalyst scanner")
    ap.add_argument("--symbols",  nargs="+", default=None,   help="Override symbol list")
    ap.add_argument("--min-gap",  type=float, default=2.0,   help="Min abs gap %% to include (default 2.0)")
    ap.add_argument("--min-vol",  type=float, default=1.2,   help="Min PM vol ratio to include (default 1.2)")
    ap.add_argument("--top",      type=int,   default=15,    help="Max symbols to show (default 15)")
    ap.add_argument("--save",     action="store_true",        help="Save watchlist to scan_results/watchlist_YYYYMMDD.json")
    args = ap.parse_args()

    symbols = args.symbols or DEFAULT_SYMBOLS
    rows    = scan(symbols, args.min_gap, args.min_vol)
    syms    = print_report(rows, args.top)

    if args.save and rows:
        out_dir = os.path.join(ROOT, "scripts", "scan_results")
        os.makedirs(out_dir, exist_ok=True)
        fname = os.path.join(out_dir, f"watchlist_{datetime.now(tz=_ET).strftime('%Y%m%d')}.json")
        payload = {
            "date":      datetime.now(tz=_ET).strftime("%Y-%m-%d"),
            "generated": datetime.now(tz=_ET).strftime("%H:%M ET"),
            "watchlist": syms or [],
            "details":   rows[:args.top],
        }
        with open(fname, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"  Saved → {fname}")


if __name__ == "__main__":
    main()
