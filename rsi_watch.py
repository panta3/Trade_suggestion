"""
rsi_watch.py — monitors all open paper trades; alerts when ALL entry gates clear.

Entry gates (same as the main engine):
  1. 1H RSI > 35
  2. MACD >= 0
  3. Vol spike >= 1.0

Usage:
    python rsi_watch.py          # check every 60 min
    python rsi_watch.py 30       # check every 30 min
"""

import sys, time, json, subprocess, platform
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent))
from core_signals import fetch_stock

PAPER_TRADES = Path(__file__).parent / "data" / "paper_trades.json"

PASS = "✓"
FAIL = "✗"


def load_open_symbols():
    data = json.loads(PAPER_TRADES.read_text())
    seen, symbols = set(), []
    for t in data.get("trades", []):
        sym = t.get("symbol", "").upper()
        if t.get("status") == "open" and sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    return symbols


def check_gates(sym):
    s = fetch_stock(sym, include_hourly=True)
    if not s:
        return sym, None

    rsi_1h    = s.get("rsi_1h")
    macd      = s.get("macd")
    vol_spike = s.get("vol_spike", 1.0)
    price     = s.get("price")

    gates = {
        "rsi_1h":    (rsi_1h,    rsi_1h    is not None and rsi_1h    >  35),
        "macd":      (macd,      macd      is not None and macd      >= 0),
        "vol_spike": (vol_spike, vol_spike is not None and vol_spike >= 1.0),
    }
    all_clear = all(v[1] for v in gates.values())

    # extra context for the alert card
    extra = {
        "rsi_d":      s.get("rsi_d"),
        "ema_trend":  s.get("ema9") and s.get("ema21") and s["ema9"] > s["ema21"],
        "price":      price,
    }
    return sym, {"gates": gates, "all_clear": all_clear, "extra": extra}


def desktop_notify(symbol, rsi_1h):
    title = f"BUY ENTRY READY — {symbol}"
    msg   = f"All entry gates clear (1H RSI {rsi_1h:.1f}, MACD +, vol ok)"
    try:
        if platform.system() == "Darwin":
            subprocess.run(["osascript", "-e",
                f'display notification "{msg}" with title "{title}"'], check=False)
        elif platform.system() == "Linux":
            subprocess.run(["notify-send", "-u", "critical", title, msg], check=False)
    except Exception:
        pass


def run_cycle(symbols, alerted):
    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(check_gates, s): s for s in symbols}
        for f in as_completed(futures):
            sym, data = f.result()
            results[sym] = data

    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{now}]  {len(symbols)} open trades")
    print(f"  {'Symbol':<12}  {'RSI1h':>6}  {'MACD':>6}  {'Vol':>5}  {'Status'}")
    print(f"  {'-'*56}")

    newly_clear = []
    for sym in symbols:
        d = results.get(sym)
        if d is None:
            print(f"  {sym:<12}  {'no data':>6}")
            continue

        g = d["gates"]
        rsi_val  = g["rsi_1h"][0]
        macd_val = g["macd"][0]
        vol_val  = g["vol_spike"][0]

        rsi_str  = f"{rsi_val:.1f}"  if rsi_val  is not None else "n/a"
        macd_str = f"{macd_val:+.3f}" if macd_val is not None else "n/a"
        vol_str  = f"{vol_val:.2f}x"  if vol_val  is not None else "n/a"

        r_flag = PASS if g["rsi_1h"][1]    else FAIL
        m_flag = PASS if g["macd"][1]       else FAIL
        v_flag = PASS if g["vol_spike"][1]  else FAIL

        if d["all_clear"]:
            status = "ALL GATES CLEAR — BUY"
            if not alerted.get(sym):
                newly_clear.append((sym, rsi_val))
                alerted[sym] = True
        else:
            failing = []
            if not g["rsi_1h"][1]:    failing.append(f"RSI1h {rsi_str}<35")
            if not g["macd"][1]:      failing.append(f"MACD neg")
            if not g["vol_spike"][1]: failing.append(f"vol weak")
            status = "wait — " + " | ".join(failing)
            alerted[sym] = False

        print(f"  {sym:<12}  {r_flag}{rsi_str:>5}  {m_flag}{macd_str:>5}  {v_flag}{vol_str:>4}  {status}")

    for sym, rsi in newly_clear:
        print(f"\n  >>> {sym}: all entry gates clear — check now <<<")
        desktop_notify(sym, rsi)

    return alerted


def main():
    interval = int(sys.argv[1]) * 60 if len(sys.argv) > 1 else 3600

    print(f"RSI Watch — reading open trades from paper_trades.json")
    print(f"Gates: 1H RSI >35  |  MACD >=0  |  vol spike >=1.0")
    print(f"Checking every {interval // 60} min. Ctrl+C to stop.\n")

    alerted = {}
    while True:
        try:
            symbols = load_open_symbols()
            if not symbols:
                print(f"[{datetime.now():%H:%M:%S}] No open trades found.")
            else:
                alerted = run_cycle(symbols, alerted)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] Error: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
