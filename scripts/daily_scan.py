"""
daily_scan.py — run all 3 algos at market open/close and save BUY/WATCH signals.
Cron (open):  30 9  * * 1-5  python3 daily_scan.py --slot open
Cron (close): 45 15 * * 1-5  python3 daily_scan.py --slot close
"""
import sys, json, os, argparse
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signals         as _sc
import largecapsignals as _lc
import SPXindex        as _spx

ALGOS = {
    "smallcap": _sc,
    "largecap":  _lc,
    "spx":       _spx,
}

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "scan_results")
LOGS_DIR    = os.path.join(os.path.dirname(__file__), "logs")


def _run_algo(name, mod):
    print(f"\n[{name.upper()}] scanning...", flush=True)
    try:
        stocks, headlines, _ = mod.scan_all()
        if not stocks:
            return []
        portfolio    = mod.load_portfolio()
        usd_cad      = mod.fetch_usdcad_rate()
        sector_ranks = mod.build_sector_rank_map(stocks)
        regime_bull, regime_label = mod.market_regime()

        recs = sorted(
            [mod.evaluate_stock(s, portfolio["cash"], usd_cad, sector_ranks,
                                include_earnings=True, regime_bullish=regime_bull,
                                all_stocks=stocks)
             for s in stocks],
            key=lambda x: x["score"], reverse=True,
        )

        signals_out = []
        buy_recs    = []
        for r in recs:
            if r["signal"] != "BUY":
                continue
            buy_recs.append(r)
            signals_out.append({
                "symbol":       r["symbol"],
                "signal":       r["signal"],
                "score":        r["score"],
                "price":        r["stock"].get("price"),
                "stop":         r["stop"],
                "target":       r["target"],
                "risk_reward":  r["risk_reward"],
                "sector":       r.get("sector"),
                "earnings_days":r.get("earnings_days"),
                "reasons":      r["reasons"],
                "warnings":     r["warnings"],
            })
        print(f"  [+] {len(signals_out)} BUY  (regime: {regime_label})")

        # ── Auto-execute top 3 BUYs (mirrors run_analysis console behavior) ───
        # log_paper_trade dedupes per symbol and, with AUTO_EXECUTE_SWING=1
        # (default), places a GTC bracket on IBKR paper — entry, target and
        # stop are then all handled by IBKR's servers with nothing running.
        for r in buy_recs[:3]:
            try:
                mod.log_paper_trade(r)
            except Exception as e:
                print(f"  [!] paper/IBKR log failed for {r['symbol']}: {e}")

        return signals_out
    except Exception as e:
        print(f"  [!] {name} failed: {e}")
        return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", default="open", choices=["open", "close"],
                        help="Which scan slot: open (9:30) or close (15:45)")
    args = parser.parse_args()

    today = date.today().isoformat()
    out_path = os.path.join(RESULTS_DIR, f"{today}_{args.slot}.json")

    if os.path.exists(out_path):
        print(f"Scan already exists for {today} ({args.slot}): {out_path}")
        return

    print(f"=== Daily Scan {today} [{args.slot}] at {datetime.now().strftime('%H:%M')} ===")
    result = {
        "date":       today,
        "slot":       args.slot,
        "scanned_at": datetime.now().isoformat(),
        "algos":      {},
    }

    for name, mod in ALGOS.items():
        result["algos"][name] = _run_algo(name, mod)

    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    total_buys = sum(
        sum(1 for s in sigs if s["signal"] == "BUY")
        for sigs in result["algos"].values()
    )
    print(f"\nSaved → {out_path}  ({total_buys} total BUY signals) [{args.slot}]")

    # EOD: reconcile bracket log fills from paper account
    if args.slot == "close":
        try:
            import ibkr_data as _id
            print("\nUpdating bracket log...")
            _id.update_bracket_log()
        except Exception as e:
            print(f"  ⚠️  Bracket log update failed: {e}")


if __name__ == "__main__":
    main()
