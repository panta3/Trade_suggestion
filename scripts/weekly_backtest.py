"""
weekly_backtest.py — run walk-forward backtest on all 3 algo universes.
Cron: 0 20 * * 0  (Sunday 8 PM, local time — before Monday open)

Also reads the week's validation logs and prints a combined accuracy report.
"""
import sys, os, json, subprocess, glob
from datetime import date, timedelta

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR    = os.path.dirname(SCRIPTS_DIR)
LOGS_DIR    = os.path.join(SCRIPTS_DIR, "logs")
PYTHON      = sys.executable

ALGO_TICKERS = {
    "smallcap": [
        "MARA","RIOT","CLSK","HUT","BITF","SOUN","NVAX","PLUG","FCEL","BLNK",
        "CHPT","IONQ","QUBT","RGTI","ACHR","JOBY","LILM",
    ],
    "largecap": [
        "AAPL","MSFT","NVDA","AMZN","META","GOOGL","AMD","AVGO","NOW","CRWD",
        "RY.TO","TD.TO","CNQ.TO","ENB.TO","ABX.TO","SHOP.TO",
    ],
    "spx": [
        "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AMD","AVGO",
        "NOW","CRWD","DDOG","SNOW","ADBE","ZS","NET","COST","NFLX","ISRG",
    ],
}


def _run_backtest(algo, tickers):
    print(f"\n{'='*55}")
    print(f"  BACKTEST: {algo.upper()}")
    print(f"{'='*55}")
    cmd = [
        PYTHON,
        os.path.join(BASE_DIR, "Backtest.py"),
        "--walk-forward",
        "--save",
    ] + tickers
    result = subprocess.run(cmd, cwd=BASE_DIR, capture_output=False, text=True)
    return result.returncode == 0


def _weekly_accuracy_summary():
    today = date.today()
    # Look at the last 7 days of validation logs
    start = today - timedelta(days=7)
    pattern = os.path.join(LOGS_DIR, "*_validate.json")
    files = sorted(glob.glob(pattern))

    week_results = []
    for f in files:
        fname_date = os.path.basename(f).split("_")[0]
        try:
            d = date.fromisoformat(fname_date)
        except ValueError:
            continue
        if d >= start:
            with open(f) as fh:
                week_results.append(json.load(fh))

    if not week_results:
        print("\nNo validation logs found for the past 7 days.")
        return

    total_signals = sum(r["total"]   for r in week_results)
    total_wins    = sum(r["wins"]    for r in week_results)
    total_losses  = sum(r["losses"]  for r in week_results)
    avg_chg       = (sum(r["avg_chg"] * r["total"] for r in week_results)
                     / total_signals if total_signals else 0)
    decided       = total_wins + total_losses      # exclude FLAT from denominator
    win_rate      = total_wins / decided * 100 if decided else 0

    # Per-outcome breakdown
    outcome_counts = {}
    for r in week_results:
        for trade in r.get("results", []):
            oc = trade["outcome"]
            outcome_counts[oc] = outcome_counts.get(oc, 0) + 1

    print(f"\n{'='*55}")
    print(f"  WEEK IN REVIEW  ({start} → {today})")
    print(f"{'='*55}")
    print(f"  Days logged      : {len(week_results)}")
    print(f"  Total BUY signals: {total_signals}")
    print(f"  Win rate (day-1) : {win_rate:.1f}%  ({total_wins}W / {total_losses}L)")
    print(f"  Avg % change     : {avg_chg:+.2f}%")
    print(f"  Breakdown:")
    for oc, count in sorted(outcome_counts.items(), key=lambda x: -x[1]):
        print(f"    {oc:<15} {count}")

    summary_path = os.path.join(LOGS_DIR, f"{today}_weekly_summary.json")
    with open(summary_path, "w") as f:
        json.dump({
            "week_end":       today.isoformat(),
            "days_logged":    len(week_results),
            "total_signals":  total_signals,
            "wins":           total_wins,
            "losses":         total_losses,
            "win_rate":       round(win_rate, 1),
            "avg_chg":        round(avg_chg, 2),
            "outcome_counts": outcome_counts,
        }, f, indent=2)
    print(f"\nSaved → {summary_path}")


def main():
    print(f"=== Weekly Backtest  {date.today()} ===")

    _weekly_accuracy_summary()

    for algo, tickers in ALGO_TICKERS.items():
        ok = _run_backtest(algo, tickers)
        if not ok:
            print(f"  [!] {algo} backtest exited with error")


if __name__ == "__main__":
    main()
