"""
daily_validate.py — compare close scan BUY signals against EOD prices.
Cron: 0 16 * * 1-5  (4:00 PM Mon–Fri)

Also:
  - Updates accuracy.json with every validated signal (no duplicates)
  - Auto-closes open paper trades that hit target, stop, or 21-day timeout
"""
import sys, json, os
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import largecapsignals as _lc

RESULTS_DIR      = os.path.join(os.path.dirname(__file__), "scan_results")
LOGS_DIR         = os.path.join(os.path.dirname(__file__), "logs")
BASE_DIR         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR        = os.path.join(BASE_DIR, "data")
ACCURACY_PATH    = os.path.join(_DATA_DIR, "accuracy.json")
PAPER_TRADES_PATH= os.path.join(_DATA_DIR, "paper_trades.json")

TRADE_TIMEOUT_DAYS = 21


def _outcome(entry, current, stop, target):
    if current >= target: return "HIT_TARGET"
    if current <= stop:   return "HIT_STOP"
    chg = (current - entry) / entry * 100
    if chg >  0.5: return "UP"
    if chg < -0.5: return "DOWN"
    return "FLAT"


def _load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Fix 1: update accuracy.json ───────────────────────────────────────────────
def _migrate_accuracy_records(signals):
    """Normalize legacy records (buy_price/pnl) to current schema in-place."""
    for r in signals:
        if "outcome" not in r:
            pnl = r.pop("pnl", 0) or 0
            entry = r.pop("buy_price", None)
            if entry is not None:
                r["entry"] = entry
            if "exit_price" not in r:
                r["exit_price"] = None
            if pnl > 0.5:
                r["outcome"] = "UP"
            elif pnl < -0.5:
                r["outcome"] = "DOWN"
            else:
                r["outcome"] = "FLAT"
            r.setdefault("chg_pct", round(pnl, 2))
            r.setdefault("stop",    None)
            r.setdefault("target",  None)


def _update_accuracy(results, today):
    acc = _load_json(ACCURACY_PATH, {"signals": []})
    _migrate_accuracy_records(acc["signals"])

    existing_keys = {
        (r.get("symbol"), r.get("date", "")[:10])
        for r in acc["signals"]
    }

    added = 0
    for r in results:
        key = (r["symbol"], today)
        if key in existing_keys:
            continue
        acc["signals"].append({
            "symbol":     r["symbol"],
            "algo":       r["algo"],
            "date":       today,
            "entry":      r["entry"],
            "exit_price": r["current"],
            "stop":       r["stop"],
            "target":     r["target"],
            "outcome":    r["outcome"],
            "chg_pct":    r["chg_pct"],
        })
        added += 1

    _save_json(ACCURACY_PATH, acc)
    print(f"  accuracy.json — {added} new record(s) added ({len(acc['signals'])} total)")


# ── Fix 2: auto-close paper trades ───────────────────────────────────────────
def _replay_daily_bars(t, today_dt):
    """
    Walk daily OHLC bars since entry, checking stop/target against each day's
    high/low — not just a single price snapshot. Catches intraday breaches and
    backfills days the validator didn't run (NEM 2026-05-17 lost -14.95% on a
    'stop_hit' because snapshot-only checking ate 24 days of unmonitored decay).

    Gap handling: if a day OPENS beyond the level, exit at the open (you can't
    fill at a level the market never traded back to). Both levels same day →
    stop wins (conservative). Also computes MFE/MAE over the hold.

    Returns (exit_price, reason, exit_date, mfe_pct, mae_pct)
        or  (None, None, None, mfe, mae) if still open.
    """
    entry, stop, target = t["entry"], t["stop"], t["target"]
    trade_dt = date.fromisoformat(t["date"])
    mfe = mae = 0.0
    try:
        import ibkr_data as _id
        bars = _id.get_daily_bars(t["symbol"], "60 D") or []
    except Exception:
        bars = []

    held = [b for b in bars
            if trade_dt < date.fromisoformat(str(b["date"])[:10]) <= today_dt]
    for b in held:
        o, h, l, c = b["open"], b["high"], b["low"], b["close"]
        mfe = max(mfe, (h - entry) / entry * 100)
        mae = min(mae, (l - entry) / entry * 100)
        if o <= stop:                       # gapped below stop — exit at open
            return o, "stop_hit", str(b["date"])[:10], round(mfe, 2), round(mae, 2)
        if o >= target:                     # gapped above target — exit at open
            return o, "target_hit", str(b["date"])[:10], round(mfe, 2), round(mae, 2)
        if l <= stop:                       # intraday breach (stop wins ties)
            return stop, "stop_hit", str(b["date"])[:10], round(mfe, 2), round(mae, 2)
        if h >= target:
            return target, "target_hit", str(b["date"])[:10], round(mfe, 2), round(mae, 2)

    age_days = (today_dt - trade_dt).days
    if age_days >= TRADE_TIMEOUT_DAYS and held:
        return held[-1]["close"], "timeout", str(held[-1]["date"])[:10], round(mfe, 2), round(mae, 2)
    return None, None, None, round(mfe, 2), round(mae, 2)


def _update_paper_trades(price_map, today):
    data = _load_json(PAPER_TRADES_PATH, {"trades": []})
    today_dt = date.fromisoformat(today)
    closed_count = 0

    for t in data["trades"]:
        if t["status"] != "open":
            continue

        sym      = t["symbol"]
        entry    = t["entry"]
        stop     = t["stop"]
        target   = t["target"]
        trade_dt = date.fromisoformat(t["date"])
        age_days = (today_dt - trade_dt).days

        # ── Primary: daily-bar replay since entry ─────────────────────────────
        exit_px, reason, exit_date, mfe, mae = _replay_daily_bars(t, today_dt)
        t["mfe_pct"], t["mae_pct"] = mfe, mae   # persist even while open

        # ── Fallback: snapshot check (bars unavailable — e.g. delisted) ───────
        if exit_px is None:
            current = price_map.get(sym)
            if current is None:
                try:
                    stock   = _lc.fetch_stock(sym, include_hourly=False)
                    current = stock.get("price") if stock.get("ok") else None
                except Exception:
                    current = None
            if current is None:
                continue
            if current >= target:
                exit_px, reason, exit_date = current, "target_hit", today
            elif current <= stop:
                exit_px, reason, exit_date = current, "stop_hit", today
            elif age_days >= TRADE_TIMEOUT_DAYS:
                exit_px, reason, exit_date = current, "timeout", today
            else:
                continue  # still open, nothing to do

        pnl_pct = round((exit_px - entry) / entry * 100, 2)
        t["status"]       = "closed"
        t["exit_price"]   = round(exit_px, 4)
        t["exit_date"]    = exit_date
        t["pnl_pct"]      = pnl_pct
        t["close_reason"] = reason
        closed_count += 1
        print(f"  CLOSED {sym:<10} [{reason}]  entry={entry:.2f} → exit={exit_px:.2f}  "
              f"pnl={pnl_pct:+.1f}%  (mfe={mfe:+.1f}% mae={mae:+.1f}%)")

        # Timeout is the one exit IBKR's GTC bracket can't handle itself —
        # flatten the paper position and cancel the resting bracket children.
        # target/stop closes are already filled by IBKR's own OCA orders.
        if reason == "timeout" and t.get("ibkr_bracket"):
            try:
                import ibkr_data as _id
                res = _id.close_stock_position(sym, t.get("shares", 1))
                print(f"    📋 IBKR position flattened: {res['shares']}x {sym} "
                      f"({res['cancelled_orders']} bracket orders cancelled)")
            except Exception as e:
                print(f"    ⚠️  IBKR flatten failed for {sym}: {e} — close manually in TWS")

    _save_json(PAPER_TRADES_PATH, data)
    open_count = sum(1 for t in data["trades"] if t["status"] == "open")
    print(f"  paper_trades.json — {closed_count} closed, {open_count} still open")


def main():
    today = date.today().isoformat()

    # Load all available scans for today and merge their signals
    candidates = [
        os.path.join(RESULTS_DIR, f"{today}_close.json"),
        os.path.join(RESULTS_DIR, f"{today}_open.json"),
        os.path.join(RESULTS_DIR, f"{today}.json"),
    ]
    scans = []
    for p in candidates:
        if os.path.exists(p):
            with open(p) as f:
                scans.append(json.load(f))

    if not scans:
        print(f"No scan file for {today}. Run daily_scan.py first.")
        return

    # Merge algos across all scans (later slots override earlier for same symbol)
    merged_algos: dict = {}
    for scan in scans:
        for algo, signals in scan["algos"].items():
            merged_algos.setdefault(algo, {})
            for s in signals:
                if s["signal"] == "BUY":
                    merged_algos[algo][s["symbol"]] = s

    scan = {"algos": {k: list(v.values()) for k, v in merged_algos.items()}}
    slots = [s.get("slot", "legacy") for s in scans]
    print(f"=== EOD Validation {today} (slots: {', '.join(slots)}) ===\n")

    all_results = []
    price_map   = {}   # sym → current price (reused for paper trade update)
    seen        = set()

    for algo, signals in scan["algos"].items():
        buys = [s for s in signals if s["signal"] == "BUY"]
        if not buys:
            continue
        print(f"[{algo.upper()}] {len(buys)} BUY signals")

        for sig in buys:
            sym = sig["symbol"]
            if sym in seen:
                continue
            seen.add(sym)

            try:
                stock   = _lc.fetch_stock(sym, include_hourly=False)
                current = stock.get("price") if stock.get("ok") else None
            except Exception:
                current = None

            if not current:
                print(f"  {sym:<10} — could not fetch price")
                continue

            price_map[sym] = current
            entry  = sig["price"]
            stop   = sig["stop"]
            target = sig["target"]
            chg    = (current - entry) / entry * 100 if entry else 0
            oc     = _outcome(entry, current, stop, target)

            print(f"  {sym:<10} entry={entry:.2f}  now={current:.2f}  "
                  f"chg={chg:+.1f}%  [{oc}]  stop={stop:.2f}  target={target:.2f}")

            all_results.append({
                "symbol":  sym,
                "algo":    algo,
                "entry":   entry,
                "current": current,
                "stop":    stop,
                "target":  target,
                "chg_pct": round(chg, 2),
                "outcome": oc,
            })

    if not all_results:
        print("No BUY signals to validate today.")
    else:
        wins     = sum(1 for r in all_results if r["outcome"] in ("HIT_TARGET", "UP"))
        losses   = sum(1 for r in all_results if r["outcome"] in ("HIT_STOP",   "DOWN"))
        total    = len(all_results)
        decided  = wins + losses                  # exclude FLAT from denominator
        win_rate = wins / decided * 100 if decided else 0
        avg_chg  = sum(r["chg_pct"] for r in all_results) / total if total else 0

        print(f"\n  Day-1 win rate : {win_rate:.0f}%  ({wins}W / {losses}L / {total-wins-losses}F)")
        print(f"  Avg % change   : {avg_chg:+.2f}%\n")

        # Save daily log
        val_path = os.path.join(LOGS_DIR, f"{today}_validate.json")
        report = {
            "date": today, "total": total, "wins": wins,
            "losses": losses, "win_rate": round(win_rate, 1),
            "avg_chg": round(avg_chg, 2), "results": all_results,
        }
        with open(val_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"  Saved log → {val_path}\n")

        # Fix 1: push to accuracy.json
        print("[accuracy.json]")
        _update_accuracy(all_results, today)

    # Fix 2: auto-close paper trades (runs even if no BUY signals today)
    print("\n[paper_trades.json]")
    _update_paper_trades(price_map, today)


if __name__ == "__main__":
    main()
