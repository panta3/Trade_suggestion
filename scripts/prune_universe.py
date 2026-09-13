#!/usr/bin/env python3
"""
scripts/prune_universe.py — Live performance pruner.

Reads data/day_trades.json, computes per-symbol stats for the last N days,
and outputs which symbols have real edge vs which to remove.

After 2+ weeks of scanning a large universe (e.g. NASDAQ-100), run this to
get a lean, backtested-AND-live-confirmed symbol list.

Usage:
    python3 scripts/prune_universe.py
    python3 scripts/prune_universe.py --days 14 --min-trades 2 --wr 50 --pf 1.0
    python3 scripts/prune_universe.py --apply   # patches DEFAULT_SYMBOLS in day_trading.py
"""

import sys, os, json, argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE  = os.path.join(ROOT, "data", "day_trades.json")
DT_FILE    = os.path.join(ROOT, "day_trading.py")

# ── Thresholds (overridden by CLI args) ──────────────────────────────────────
DEFAULT_DAYS       = 14    # look-back window
DEFAULT_MIN_TRADES = 2     # need at least this many to have an opinion
DEFAULT_MIN_WR     = 50.0  # win rate %
DEFAULT_MIN_PF     = 1.0   # profit factor (gross winners / gross losers)


def _stats(trades):
    closed = [t for t in trades if t.get("pnl_pct") is not None]
    if not closed:
        return None
    wins   = [t["pnl_pct"] for t in closed if t["pnl_pct"] > 0]
    losses = [t["pnl_pct"] for t in closed if t["pnl_pct"] <= 0]
    n      = len(closed)
    wr     = len(wins) / n * 100
    avg    = sum(t["pnl_pct"] for t in closed) / n
    gross_w = sum(wins)   if wins   else 0
    gross_l = abs(sum(losses)) if losses else 0
    pf     = gross_w / gross_l if gross_l > 0 else (float("inf") if gross_w > 0 else 0)
    exp    = avg  # expectancy per trade (simple)
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "wr": round(wr, 1), "avg_pnl": round(avg, 3),
        "pf": round(pf, 2), "expectancy": round(exp, 3),
        "best": round(max(t["pnl_pct"] for t in closed), 2),
        "worst": round(min(t["pnl_pct"] for t in closed), 2),
    }


def _grade_color(verdict):
    return {"KEEP": "\033[92m", "WATCH": "\033[93m", "REMOVE": "\033[91m"}.get(verdict, "")

RESET = "\033[0m"


def main():
    ap = argparse.ArgumentParser(description="Prune scanner universe by live performance")
    ap.add_argument("--days",       type=int,   default=DEFAULT_DAYS,
                    help="Look-back window in calendar days (default 14)")
    ap.add_argument("--min-trades", type=int,   default=DEFAULT_MIN_TRADES,
                    help="Min closed trades to form a verdict (default 2)")
    ap.add_argument("--wr",         type=float, default=DEFAULT_MIN_WR,
                    help="Min win rate %% to KEEP (default 50)")
    ap.add_argument("--pf",         type=float, default=DEFAULT_MIN_PF,
                    help="Min profit factor to KEEP (default 1.0)")
    ap.add_argument("--grade",      default=None, choices=["S","A","B","C"],
                    help="Filter to a specific signal grade")
    ap.add_argument("--apply",      action="store_true",
                    help="Patch DEFAULT_SYMBOLS in day_trading.py with the KEEP list")
    args = ap.parse_args()

    if not os.path.exists(DATA_FILE):
        print("No day_trades.json found. Run the scanner for a few days first.")
        sys.exit(1)

    with open(DATA_FILE) as f:
        raw = json.load(f)
    trades = raw.get("trades", raw) if isinstance(raw, dict) else raw

    # Filter to look-back window
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    trades = [t for t in trades if t.get("date", "") >= cutoff]
    if args.grade:
        trades = [t for t in trades if t.get("grade") == args.grade]

    if not trades:
        print(f"No trades found in the last {args.days} days.")
        sys.exit(0)

    # Group by symbol
    by_sym = defaultdict(list)
    for t in trades:
        if t.get("symbol"):
            by_sym[t["symbol"]].append(t)

    # Compute stats + verdict
    rows = []
    for sym, sym_trades in sorted(by_sym.items()):
        s = _stats(sym_trades)
        if not s:
            continue
        if s["n"] < args.min_trades:
            verdict = "WATCH"  # not enough data yet
        elif s["wr"] >= args.wr and s["pf"] >= args.pf:
            verdict = "KEEP"
        elif s["wr"] >= args.wr * 0.9 or s["pf"] >= args.pf * 0.9:
            verdict = "WATCH"
        else:
            verdict = "REMOVE"
        rows.append((sym, s, verdict))

    # Sort: KEEP first, then WATCH, then REMOVE; within group by WR desc
    order = {"KEEP": 0, "WATCH": 1, "REMOVE": 2}
    rows.sort(key=lambda r: (order[r[2]], -r[1]["wr"]))

    # ── Print table ───────────────────────────────────────────────────────────
    since = (datetime.now() - timedelta(days=args.days)).strftime("%b %d")
    print("=" * 80)
    print(f"  LIVE PERFORMANCE AUDIT  |  Last {args.days} days ({since}→today)")
    grade_str = f"  Grade {args.grade} only" if args.grade else "  All grades"
    print(f"  Thresholds: WR≥{args.wr:.0f}%  PF≥{args.pf:.1f}  MinTrades≥{args.min_trades}{grade_str}")
    print("=" * 80)
    print(f"  {'Symbol':<8} {'N':>4} {'WR':>6} {'AvgP&L':>8} {'PF':>5} {'Best':>7} {'Worst':>7}  Verdict")
    print("  " + "-" * 68)
    for sym, s, verdict in rows:
        col   = _grade_color(verdict)
        mark  = "✓" if verdict == "KEEP" else ("~" if verdict == "WATCH" else "✗")
        print(f"  {sym:<8} {s['n']:>4} {s['wr']:>5.1f}% {s['avg_pnl']:>+7.2f}%"
              f" {s['pf']:>5.2f} {s['best']:>+6.1f}% {s['worst']:>+6.1f}%"
              f"  {col}{mark} {verdict}{RESET}")

    # ── Summary ───────────────────────────────────────────────────────────────
    keep   = [r[0] for r in rows if r[2] == "KEEP"]
    watch  = [r[0] for r in rows if r[2] == "WATCH"]
    remove = [r[0] for r in rows if r[2] == "REMOVE"]

    print()
    print(f"  {_grade_color('KEEP')}KEEP   ({len(keep)}): {', '.join(keep) or '—'}{RESET}")
    print(f"  {_grade_color('WATCH')}WATCH  ({len(watch)}): {', '.join(watch) or '—'}{RESET}")
    print(f"  {_grade_color('REMOVE')}REMOVE ({len(remove)}): {', '.join(remove) or '—'}{RESET}")
    print()

    total_trades = sum(r[1]["n"] for r in rows)
    total_pnl    = sum(r[1]["avg_pnl"] * r[1]["n"] for r in rows) / max(total_trades, 1)
    print(f"  Universe: {len(rows)} symbols  |  {total_trades} total trades  |  Avg P&L {total_pnl:+.3f}%/trade")
    print("=" * 80)

    if not keep:
        print("\n  ⚠  No symbols meet thresholds yet. Run scanner longer or lower --wr/--pf.")
        return

    # ── Copy-paste ready DEFAULT_SYMBOLS ─────────────────────────────────────
    print("\n  Recommended DEFAULT_SYMBOLS (KEEP list):")
    chunks = [keep[i:i+10] for i in range(0, len(keep), 10)]
    print("  DEFAULT_SYMBOLS = [")
    for chunk in chunks:
        print("    " + ", ".join(f'"{s}"' for s in chunk) + ",")
    print("  ]")

    # ── --apply: patch day_trading.py ─────────────────────────────────────────
    if args.apply:
        print()
        _patch_default_symbols(keep)


def _patch_default_symbols(keep: list):
    """Replace the DEFAULT_SYMBOLS list in day_trading.py with the pruned KEEP list."""
    import re
    with open(DT_FILE) as f:
        src = f.read()

    # Find the DEFAULT_SYMBOLS = [...] block (handles multi-line)
    pattern = re.compile(
        r'(DEFAULT_SYMBOLS\s*=\s*\[)[^\]]*(\])',
        re.DOTALL
    )
    if not pattern.search(src):
        print("  ⚠  Could not find DEFAULT_SYMBOLS in day_trading.py — patch skipped.")
        return

    chunks   = [keep[i:i+8] for i in range(0, len(keep), 8)]
    inner    = "\n" + "".join(
        "    " + ", ".join(f'"{s}"' for s in chunk) + ",\n"
        for chunk in chunks
    )
    new_src  = pattern.sub(r'\g<1>' + inner + r'\2', src)

    backup = DT_FILE + ".bak"
    with open(backup, "w") as f:
        f.write(src)
    with open(DT_FILE, "w") as f:
        f.write(new_src)

    print(f"  ✓  DEFAULT_SYMBOLS updated in day_trading.py ({len(keep)} symbols)")
    print(f"  ✓  Backup saved to day_trading.py.bak")


if __name__ == "__main__":
    main()
