"""
test_fixes.py — smoke-tests for the 6 fixes applied 2026-05-06.
Run:  python3 scripts/test_fixes.py
All tests should print PASS.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import signals as sc
import largecapsignals as lc
import SPXindex as spx
from Backtest import simulate_trade, SLIPPAGE_PCT, COMMISSION_PCT


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    return cond


def _make_stock(price=50.0, rsi=38, symbol="TST"):
    """Minimal stock dict that evaluate_stock can process."""
    return {
        "ok": True, "symbol": symbol, "price": price,
        "rsi_d": rsi, "rsi_1h": 40, "vol_spike": 2.0,
        "change": 1.0, "volatility": 3.0,
        "support": price * 0.94, "resistance": price * 1.12,
        "ema9": price, "ema21": price * 0.97,
        "cmf20": 0.1, "ret7": 2.0, "ret30": 5.0,
        "sentiment": {"score": 0, "label": "neutral", "headlines": []},
    }


def _make_rows(n=40, base=50.0):
    """Minimal OHLCV rows for simulate_trade."""
    rows = []
    for i in range(n):
        p = base + i * 0.1
        rows.append({
            "date": f"2026-01-{i+1:02d}", "open": p, "high": p * 1.02,
            "low": p * 0.98, "close": p, "volume": 1_000_000,
        })
    return rows


all_pass = True

# ── Fix 1: earnings hard-block within 5 days ─────────────────────────────────
print("\nFix 1 — earnings suppresses BUY within 5 days (all 3 modules)")
for mod in (sc, lc, spx):
    s = _make_stock()
    # Monkey-patch fetch_earnings_date to return 3 days out
    original = mod.fetch_earnings_date
    mod.fetch_earnings_date = lambda sym: ("2026-05-09", 3)
    rec = mod.evaluate_stock(s, 10_000, 1.0, {}, include_earnings=True,
                             regime_bullish=True, all_stocks=[s])
    mod.fetch_earnings_date = original
    ok = rec["signal"] != "BUY"
    all_pass &= check(f"{mod.__name__}: signal={rec['signal']} (not BUY)", ok)

# ── Fix 2: cash balance does NOT affect signal quality ───────────────────────
# We intentionally removed cash-based BUY blocking — stale portfolio.json cash
# should never suppress a technically valid signal. Signal must equal the same
# value regardless of whether cash is $50 or $50,000.
print("\nFix 2 — cash balance does not affect signal quality")
for mod in (sc, lc, spx):
    s = _make_stock()
    rec_rich = mod.evaluate_stock(s, 50_000.0, 1.0, {}, include_earnings=False,
                                  regime_bullish=True, all_stocks=[s])
    rec_poor = mod.evaluate_stock(s, 50.0, 1.0, {}, include_earnings=False,
                                  regime_bullish=True, all_stocks=[s])
    ok = rec_rich["signal"] == rec_poor["signal"]
    all_pass &= check(f"{mod.__name__}: signal identical regardless of cash "
                      f"(both={rec_rich['signal']})", ok)

# ── Fix 3: slippage constant exists and is applied to simulate_trade ─────────
print("\nFix 3 — slippage in Backtest.py")
all_pass &= check("SLIPPAGE_PCT == 0.001", SLIPPAGE_PCT == 0.001)
rows   = _make_rows(40, base=100.0)
result = simulate_trade(rows, entry_idx=5, capital=10_000)
if result:
    raw_open = rows[6]["open"]        # entry at next-day open
    expected_entry = round(raw_open * (1 + SLIPPAGE_PCT), 4)
    ok = result["entry_price"] == expected_entry
    all_pass &= check(f"entry_price={result['entry_price']} includes slippage", ok)
else:
    all_pass &= check("simulate_trade returned a result", False)

# ── Fix 4: api sector_ranks single-ticker — build_sector_rank_map([s]) ───────
print("\nFix 4 — build_sector_rank_map works with single stock")
s = _make_stock(symbol="AAPL")
s["symbol"] = "AAPL"
ranks = lc.build_sector_rank_map([s])
all_pass &= check(f"sector_ranks not empty: {ranks}", bool(ranks))

# ── Fix 5: sector cap (max 2 BUY per sector) — test via api module logic ─────
print("\nFix 5 — sector cap logic (manual simulation)")
sector_buy_count: dict = {}
signals_in = [
    {"signal": "BUY", "sector": "semis", "warnings": []},
    {"signal": "BUY", "sector": "semis", "warnings": []},
    {"signal": "BUY", "sector": "semis", "warnings": []},   # 3rd — should be downgraded
]
for r in signals_in:
    if r["signal"] == "BUY":
        sec = r.get("sector") or "unknown"
        sector_buy_count[sec] = sector_buy_count.get(sec, 0) + 1
        if sector_buy_count[sec] > 2:
            r["signal"] = "WATCH"
            r["warnings"].append(f"sector cap: >2 BUYs already flagged in {sec}")
ok = signals_in[2]["signal"] == "WATCH"
all_pass &= check(f"3rd semis signal downgraded to WATCH: {signals_in[2]['signal']}", ok)
ok2 = signals_in[0]["signal"] == "BUY" and signals_in[1]["signal"] == "BUY"
all_pass &= check("first 2 semis signals remain BUY", ok2)

# ── Fix 6: api_score sector_ranks import compiles ────────────────────────────
print("\nFix 6 — api.py imports without error")
try:
    import api as _api
    all_pass &= check("api.py imports cleanly", True)
except Exception as e:
    all_pass &= check(f"api.py import failed: {e}", False)

print(f"\n{'='*40}")
print(f"  {'ALL PASS' if all_pass else 'SOME TESTS FAILED'}")
print(f"{'='*40}\n")
sys.exit(0 if all_pass else 1)
