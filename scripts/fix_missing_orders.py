"""
One-shot: place paper options orders for today's signals that have options_ibkr=NONE.
Run once, then delete.
"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Ensure asyncio event loop exists before importing ib_insync-backed modules
asyncio.set_event_loop(asyncio.new_event_loop())

import ibkr_data as _id

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "day_trades.json")
TODAY = "2026-05-20"

with open(DATA_FILE) as f:
    data = json.load(f)

trades = data.get("trades", [])
missing = [t for t in trades if t.get("date") == TODAY and t.get("options_ibkr") is None and t.get("status") == "open"]

if not missing:
    print("No open trades with missing options orders today.")
    sys.exit(0)

print(f"Found {len(missing)} trade(s) needing options orders:\n")

for t in missing:
    sym  = t["symbol"]
    dirn = t["direction"]
    entry = t.get("entry", 0.0)
    print(f"  Placing {dirn} order for {sym} (entry ${entry:.2f}) ...")
    try:
        result = _id.place_options_order(sym, direction=dirn, qty=1, dte_min=1, spot=entry)
        if result:
            t["options_ibkr"] = result
            print(f"    ✓ BUY 1 {result['symbol']} {result['direction']} "
                  f"${result['strike']} exp {result['expiry']}  est ${result['est_price']:.2f}")
        else:
            print(f"    ℹ️  No liquid contract found for {sym} — skipped")
    except Exception as e:
        print(f"    ⚠️  Failed: {e}")

with open(DATA_FILE, "w") as f:
    json.dump(data, f, indent=2, default=str)

print("\nday_trades.json updated.")
