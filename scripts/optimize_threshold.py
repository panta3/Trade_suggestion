"""
optimize_threshold.py — Walk-forward SIGNAL_THRESHOLD optimizer.

Usage:
    python3 scripts/optimize_threshold.py

Reads data/day_trades.json (closed trades only) and tests every threshold
value 60-85.  Picks the value that maximizes expectancy × √n (balances
signal quality vs. sample size).  Recommends the optimal threshold and
shows how to apply it in day_trading.py.
"""
import sys, os, json, math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR    = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
TRADES_FILE = os.path.join(DATA_DIR, "day_trades.json")
SLIPPAGE    = 0.10   # 0.05% per side round-trip
MIN_SAMPLE  = 5      # skip buckets with fewer than this many trades


def _stats(group):
    if len(group) < MIN_SAMPLE:
        return None
    pnls   = [(t.get("pnl_pct") or 0) - SLIPPAGE for t in group]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n      = len(pnls)
    gw     = sum(wins)
    gl     = abs(sum(losses))
    pf     = round(gw / gl, 2) if gl > 0 else float("inf")
    wr     = len(wins) / n
    aw     = round(sum(wins)   / len(wins),   2) if wins   else 0.0
    al     = round(sum(losses) / len(losses), 2) if losses else 0.0
    exp    = round((wr * aw) + ((1 - wr) * al), 3)
    return {"n": n, "wr": round(wr * 100, 1), "pf": pf, "exp": exp, "aw": aw, "al": al}


def main():
    if not os.path.exists(TRADES_FILE):
        print("No day_trades.json found — run the scanner first.")
        return

    with open(TRADES_FILE) as f:
        data = json.load(f)

    closed = [
        t for t in data["trades"]
        if t["status"] == "closed"
        and t.get("pnl_pct") is not None
        and t.get("score") is not None
    ]

    if not closed:
        print("No closed trades with scores yet — run intraday_validate.py first.")
        return

    print(f"\n{'─'*76}")
    print(f"  WALK-FORWARD THRESHOLD OPTIMIZER  ({len(closed)} closed trades, "
          f"slippage={SLIPPAGE:.2f}% RT)")
    print(f"{'─'*76}")
    print(f"  {'Threshold':<12} {'N':>5} {'WinRate':>8} {'PF':>7} "
          f"{'Expectancy':>12} {'Score':>9}")
    print(f"{'─'*76}")

    results = []
    for th in range(60, 86):
        group = [t for t in closed if (t.get("score") or 0) >= th]
        s = _stats(group)
        if not s:
            continue
        composite = s["exp"] * math.sqrt(s["n"])
        results.append((th, s, composite))

    if not results:
        print(f"  Need {MIN_SAMPLE}+ closed trades per threshold level.\n")
        return

    best_th, _, best_comp = max(results, key=lambda x: x[2])

    for th, s, comp in results:
        marker  = "◄ BEST" if th == best_th else ""
        pf_str  = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "∞"
        print(f"  {th:<12} {s['n']:>5} {s['wr']:>7.1f}% {pf_str:>7} "
              f"{s['exp']:>+11.3f}% {comp:>9.2f}  {marker}")

    best_s = next(s for t, s, _ in results if t == best_th)
    print(f"\n  ✓  Recommended SIGNAL_THRESHOLD = {best_th}")
    print(f"     {best_s['n']} trades · {best_s['wr']}% WR · "
          f"PF={best_s['pf']} · E={best_s['exp']:+.3f}% (slip-adj)")
    print(f"\n  To apply: edit day_trading.py  →  SIGNAL_THRESHOLD = {best_th}")
    print()


if __name__ == "__main__":
    main()
