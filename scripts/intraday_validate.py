"""
intraday_validate.py — Validate open day trades and print accuracy breakdown.

Usage:
    python3 scripts/intraday_validate.py               # report only
    python3 scripts/intraday_validate.py --close-eod   # also close past-date open trades
"""
import sys, os, json, re, urllib.request
from datetime import datetime, timezone
from collections import defaultdict, Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import day_trading as _dt

DATA_DIR       = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
TRADES_FILE    = os.path.join(DATA_DIR, "day_trades.json")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
STATS_FILE     = os.path.join(DATA_DIR, "accuracy_stats.json")

MIN_SAMPLE    = 10    # warn when a bucket has fewer trades than this
SLIPPAGE_PCT  = 0.10  # round-trip slippage estimate (0.05% entry + 0.05% exit)


def _update_portfolio(pnl_dollar, symbol, reason):
    """Credit/debit realized P&L back to portfolio.json cash."""
    try:
        with open(PORTFOLIO_FILE) as f:
            pf = json.load(f)
        pf["cash"] = round(pf.get("cash", 0) + pnl_dollar, 6)
        pf.setdefault("history", []).append({
            "action":       "DAY_TRADE_CLOSE",
            "symbol":       symbol,
            "close_reason": reason,
            "pnl_dollar":   round(pnl_dollar, 2),
            "date":         datetime.now(tz=timezone.utc).isoformat(),
        })
        with open(PORTFOLIO_FILE, "w") as f:
            json.dump(pf, f, indent=2)
        return True
    except Exception as e:
        print(f"  Warning: could not update portfolio.json — {e}")
        return False


def _slot(time_str):
    try:
        h = int(time_str.split(":")[0])
        if h < 10:  return "opening (9:30-10:00)"
        if h < 12:  return "morning (10:00-12:00)"
        if h < 14:  return "midday  (12:00-14:00)"
        return              "afternoon(14:00-16:00)"
    except Exception:
        return "unknown"


def _current_price(symbol):
    try:
        bars, _ = _dt.fetch_symbol_data(symbol)
        return bars[-1][4] if bars else None
    except Exception:
        return None


def _eod_sim(trade):
    """
    Reconstruct exit for a past-date trade.

    Method 1 — 5-min bar replay (accurate):
        Fetches 5-min bars, filters to the trade date + bars at/after signal
        fire time, then steps through in chronological order. First level
        touched wins — eliminates the target-or-stop ordering ambiguity.
        Available for trades up to ~5 calendar days old (Yahoo Free limit).

    Method 2 — daily OHLC fallback (slightly optimistic):
        Used when 5-min bars are unavailable (trade too old). Can't determine
        which level was hit first; assumes target if both were touched.

    Returns (exit_price, reason, exit_time, sim_method)
        or  (None, None, None, None) on total failure.
    """
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")

    symbol     = trade["symbol"]
    trade_date = trade["date"]
    target     = trade["target"]
    direction  = trade["direction"]
    entry      = trade.get("entry", 0)
    fire_time  = trade.get("time", "09:35")   # HH:MM the signal fired
    # Breakeven-aware replay: start from the ORIGINAL stop (stop_initial) and
    # re-derive the +1R ratchet bar-by-bar, mirroring check_open_trades().
    # Using the already-raised trade["stop"] on pre-arming bars rewrites history.
    stop       = trade.get("stop_initial", trade["stop"])
    be_at_r    = getattr(_dt, "BREAKEVEN_AT_R", 1.0)

    # ── Method 1: 5-min bar replay ────────────────────────────────────────────
    try:
        bars, _ = _dt.fetch_symbol_data(symbol)
        if bars:
            day_bars = []
            for b in bars:
                bar_dt   = datetime.fromtimestamp(b[0], tz=_ET)
                bar_date = bar_dt.strftime("%Y-%m-%d")
                bar_time = bar_dt.strftime("%H:%M")
                if bar_date == trade_date and bar_time >= fire_time:
                    day_bars.append((bar_time, b))

            if day_bars:
                be_trigger = entry + (entry - stop) * be_at_r if entry else None
                eff_stop   = stop
                be_armed   = False
                for bar_time, b in day_bars:
                    _, _, hi, lo, cl, _ = b
                    if direction == "CALL":
                        if hi >= target:
                            return round(target, 4), "target_hit", bar_time, "5min"
                        if lo <= eff_stop:
                            reason = "breakeven_stop" if be_armed and eff_stop >= entry else "stop_hit"
                            return round(eff_stop, 4), reason, bar_time, "5min"
                        # Arm breakeven for subsequent bars (same rule as live monitor)
                        if not be_armed and be_trigger and hi >= be_trigger:
                            be_armed, eff_stop = True, entry
                    else:
                        if lo <= target: return round(target, 4), "target_hit",  bar_time, "5min"
                        if hi >= stop:   return round(stop,   4), "stop_hit",    bar_time, "5min"
                # Neither level touched — exit at last bar's close
                last_time, last_b = day_bars[-1]
                return round(last_b[4], 4), "eod", last_time, "5min"
    except Exception:
        pass

    # ── Method 2: daily OHLC fallback (trade too old for 5-min data) ─────────
    try:
        import ibkr_data as _id
        bars = _id.get_daily_bars(symbol, "14 D")
        for b in bars:
            if b["date"] != trade_date:
                continue
            hi, lo, cl = b["high"], b["low"], b["close"]
            if direction == "CALL":
                if hi >= target: return round(target, 4), "target_hit", "—", "daily_ohlc"
                if lo <= stop:   return round(stop,   4), "stop_hit",   "—", "daily_ohlc"
                return round(cl, 4), "eod", "—", "daily_ohlc"
            else:
                if lo <= target: return round(target, 4), "target_hit", "—", "daily_ohlc"
                if hi >= stop:   return round(stop,   4), "stop_hit",   "—", "daily_ohlc"
                return round(cl, 4), "eod", "—", "daily_ohlc"
        return None, None, None, None
    except Exception:
        return None, None, None, None


def _stats(group):
    if not group:
        return None
    pnls  = [t.get("pnl_pct") or 0 for t in group]
    wins  = [p for p in pnls if p > 0]
    losses= [p for p in pnls if p <= 0]
    n     = len(pnls)
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf")
    avg_win  = round(sum(wins)   / len(wins),   2) if wins   else 0.0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
    win_rate = round(len(wins) / n * 100, 1)
    # Expectancy: expected P&L per trade
    expectancy = round((win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss), 3)
    return {
        "n":          n,
        "wins":       len(wins),
        "losses":     len(losses),
        "win_rate":   win_rate,
        "avg_pnl":    round(sum(pnls) / n, 2),
        "avg_win":    avg_win,
        "avg_loss":   avg_loss,
        "profit_factor": pf,
        "expectancy": expectancy,
    }


def _bar(stats, width=16):
    filled = int(stats["win_rate"] / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _sample_warn(n):
    return f"  ⚠ only {n} trades — need {MIN_SAMPLE}+ for reliability" if n < MIN_SAMPLE else ""


def _persist_stats(closed):
    """Append a snapshot of Grade S closed trades to accuracy_stats.json."""
    try:
        history = []
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE) as f:
                history = json.load(f)
        grade_s = [t for t in closed if t.get("grade") == "S"]
        s = _stats(grade_s)
        if s:
            history.append({
                "date":          datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
                "n":             s["n"],
                "win_rate":      s["win_rate"],
                "avg_pnl":       s["avg_pnl"],
                "profit_factor": s["profit_factor"],
                "expectancy":    s["expectancy"],
            })
            with open(STATS_FILE, "w") as f:
                json.dump(history, f, indent=2)
    except Exception:
        pass


def main():
    close_eod = "--close-eod" in sys.argv

    if not os.path.exists(TRADES_FILE):
        print("No day_trades.json found — run the scanner first.")
        return

    with open(TRADES_FILE) as f:
        data = json.load(f)

    trades = data["trades"]
    today  = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    # ── Auto-close open trades ────────────────────────────────────────────────
    changed = False
    print("Checking open trades…\n")
    for t in trades:
        if t["status"] != "open":
            continue

        entry      = t["entry"]
        past_date  = t["date"] < today
        exit_time  = "—"
        sim_method = "live"

        # ── Past-date OR same-day with --close-eod: replay 5-min bars ──────────
        if close_eod:
            exit_px, reason, exit_time, sim_method = _eod_sim(t)
            if exit_px is None:
                exit_px    = _current_price(t["symbol"])
                reason     = "eod"
                exit_time  = "—"
                sim_method = "live_fallback"
            if exit_px is None:
                print(f"  {t['symbol']:<8} — could not fetch data, skipping")
                continue
            pnl_raw = (exit_px - entry) / entry * 100
            pnl_pct = round(pnl_raw if t["direction"] == "CALL" else -pnl_raw, 2)
        else:
            # ── Live trade: bar replay to catch intraday touches, snapshot for pnl ──
            _br_px, _br_reason, _br_time, _br_method = _eod_sim(t)
            if _br_reason in ("target_hit", "stop_hit"):
                # bar replay found a level touch — use it
                exit_px, reason, exit_time, sim_method = _br_px, _br_reason, _br_time, _br_method
                pnl_raw = (_br_px - entry) / entry * 100
                pnl_pct = round(pnl_raw if t["direction"] == "CALL" else -pnl_raw, 2)
            else:
                # no trigger yet — use current snapshot for live pnl display only
                exit_px = _current_price(t["symbol"])
                if exit_px is None:
                    print(f"  {t['symbol']:<8} — could not fetch price, skipping")
                    continue
                reason     = None
                exit_time  = "—"
                sim_method = "live"
                pnl_raw = (exit_px - entry) / entry * 100
                pnl_pct = round(pnl_raw if t["direction"] == "CALL" else -pnl_raw, 2)

        if reason:
            t["status"]       = "closed"
            t["exit_price"]   = exit_px
            t["pnl_pct"]      = pnl_pct
            t["close_reason"] = reason
            _et_val  = exit_time
            _sm_val  = sim_method
            t["exit_time"]  = _et_val
            t["sim_method"] = _sm_val
            changed = True

            shares     = t.get("shares", 0)
            pnl_dollar = round(pnl_pct / 100 * entry * shares, 2) if shares > 0 else 0.0
            if shares > 0:
                t["pnl_dollar"] = pnl_dollar
                _update_portfolio(pnl_dollar, t["symbol"], reason)

            icon       = "✓" if pnl_pct >= 0 else "✗"
            method_tag = f"[{_sm_val}]" if _sm_val not in ("live", "—") else ""
            time_tag   = f"@{_et_val}" if _et_val and _et_val != "—" else ""
            pnl_str    = f"  pnl={pnl_pct:+.1f}%"
            if shares > 0:
                pnl_str += f"  (${pnl_dollar:+.2f})"
            print(f"  {icon} CLOSED {t['symbol']:<8} [{reason:<12}] {time_tag:<7} "
                  f"entry=${entry:.2f} → exit=${exit_px:.2f}{pnl_str}  {method_tag}")
        else:
            pnl_live = round(pnl_raw if t["direction"] == "CALL" else -pnl_raw, 2)
            print(f"  … OPEN   {t['symbol']:<8} [still open]    "
                  f"entry=${entry:.2f}  now=${exit_px:.2f}  live pnl={pnl_live:+.1f}%")

    if changed:
        with open(TRADES_FILE, "w") as f:
            json.dump(data, f, indent=2)

    # ── Accuracy breakdown ────────────────────────────────────────────────────
    closed = [t for t in trades if t["status"] == "closed" and t.get("pnl_pct") is not None]
    open_n = sum(1 for t in trades if t["status"] == "open")

    print(f"\n{'─'*66}")
    print(f"  INTRADAY ACCURACY REPORT")
    print(f"  {len(closed)} closed  ·  {open_n} still open  ·  {len(trades)} total logged")
    print(f"{'─'*66}")

    if not closed:
        print("\n  No closed trades to analyze yet.\n")
        return

    _persist_stats(closed)

    # ── Overall ───────────────────────────────────────────────────────────────
    s = _stats(closed)
    print(f"\n  OVERALL  {_bar(s)}  {s['win_rate']}%  "
          f"({s['wins']}W/{s['losses']}L)  "
          f"avg={s['avg_pnl']:+.2f}%  PF={s['profit_factor']}  E={s['expectancy']:+.3f}%\n")
    print(f"  avg winner: +{s['avg_win']:.2f}%   avg loser: {s['avg_loss']:.2f}%   "
          f"ratio: {abs(s['avg_win']/s['avg_loss']):.2f}x\n" if s['avg_loss'] != 0 else "")

    # Slippage-adjusted expectancy (0.05% per side = 0.10% round-trip)
    slip_pnls  = [max(-100, (t.get("pnl_pct") or 0) - SLIPPAGE_PCT) for t in closed]
    slip_wins  = [p for p in slip_pnls if p > 0]
    slip_loss  = [p for p in slip_pnls if p <= 0]
    slip_wr    = round(len(slip_wins) / len(slip_pnls) * 100, 1) if slip_pnls else 0
    slip_aw    = round(sum(slip_wins) / len(slip_wins),   2) if slip_wins else 0.0
    slip_al    = round(sum(slip_loss) / len(slip_loss),   2) if slip_loss else 0.0
    slip_exp   = round((slip_wr / 100 * slip_aw) + ((1 - slip_wr / 100) * slip_al), 3)
    slip_avg   = round(sum(slip_pnls) / len(slip_pnls), 2)
    slip_gw    = sum(slip_wins)
    slip_gl    = abs(sum(slip_loss))
    slip_pf    = round(slip_gw / slip_gl, 2) if slip_gl > 0 else float("inf")
    print(f"  slippage-adj ({SLIPPAGE_PCT:.2f}% RT): {_bar(s)}  {slip_wr}%  "
          f"avg={slip_avg:+.2f}%  PF={slip_pf}  E={slip_exp:+.3f}%\n")

    # ── By time slot ──────────────────────────────────────────────────────────
    print("  BY TIME SLOT")
    slot_groups = defaultdict(list)
    for t in closed:
        slot_groups[_slot(t.get("time", ""))].append(t)
    for slot in ["opening (9:30-10:00)", "morning (10:00-12:00)",
                 "midday  (12:00-14:00)", "afternoon(14:00-16:00)"]:
        g = slot_groups.get(slot, [])
        if not g:
            continue
        s = _stats(g)
        print(f"  {slot}  {_bar(s, 12)}  {s['win_rate']}%  "
              f"n={s['n']}  avg={s['avg_pnl']:+.2f}%  PF={s['profit_factor']}"
              + _sample_warn(s["n"]))

    # ── By direction ──────────────────────────────────────────────────────────
    print("\n  BY DIRECTION")
    for direction, arrow in [("CALL", "▲"), ("PUT", "▼")]:
        g = [t for t in closed if t["direction"] == direction]
        if not g:
            continue
        s = _stats(g)
        print(f"  {arrow} {direction:<4}  {_bar(s, 12)}  {s['win_rate']}%  "
              f"n={s['n']}  avg={s['avg_pnl']:+.2f}%  PF={s['profit_factor']}"
              + _sample_warn(s["n"]))

    # ── By grade (individual) ─────────────────────────────────────────────────
    print("\n  BY GRADE")
    grade_stats = {}
    for grade in ["S", "A", "B", "C"]:
        g = [t for t in closed if t.get("grade") == grade]
        if not g:
            continue
        s = _stats(g)
        grade_stats[grade] = s
        print(f"  [{grade}]  {_bar(s, 12)}  {s['win_rate']}%  "
              f"n={s['n']}  avg={s['avg_pnl']:+.2f}%  PF={s['profit_factor']}"
              + _sample_warn(s["n"]))

    # ── Grade calibration verdict ─────────────────────────────────────────────
    print("\n  GRADE CALIBRATION  (does higher grade → better outcome?)")
    high_g = [t for t in closed if t.get("grade") in ("S", "A")]
    low_g  = [t for t in closed if t.get("grade") in ("B", "C")]
    sh = _stats(high_g)
    sl = _stats(low_g)
    if sh and sl:
        gap = round(sh["win_rate"] - sl["win_rate"], 1)
        pf_gap = round(sh["profit_factor"] - sl["profit_factor"], 2) if sl["profit_factor"] != float("inf") else "∞"
        print(f"  S/A  {_bar(sh, 12)}  {sh['win_rate']}%  n={sh['n']}  "
              f"avg={sh['avg_pnl']:+.2f}%  PF={sh['profit_factor']}")
        print(f"  B/C  {_bar(sl, 12)}  {sl['win_rate']}%  n={sl['n']}  "
              f"avg={sl['avg_pnl']:+.2f}%  PF={sl['profit_factor']}")
        if sh["n"] < MIN_SAMPLE or sl["n"] < MIN_SAMPLE:
            print(f"  ⚠  need {MIN_SAMPLE}+ trades per tier before this is reliable "
                  f"(have {sh['n']} S/A, {sl['n']} B/C)")
        elif gap >= 15:
            print(f"  ✓  grades ARE predictive  (+{gap}pp gap, PF diff {pf_gap}) — "
                  f"trust S/A signals more")
        elif gap >= 5:
            print(f"  ~  weak grade signal  (+{gap}pp gap) — "
                  f"grades trending right but not decisive yet")
        else:
            print(f"  ✗  grades NOT predictive  ({gap:+.1f}pp gap) — "
                  f"signal weights need recalibration")

    # ── By score bucket ───────────────────────────────────────────────────────
    print("\n  BY SCORE BUCKET  (does higher score → higher win rate?)")
    buckets = [
        ("68–74",  lambda t: 68 <= (t.get("score") or 0) < 75),
        ("75–84",  lambda t: 75 <= (t.get("score") or 0) < 85),
        ("85–90",  lambda t: 85 <= (t.get("score") or 0) < 91),
        ("91–100", lambda t: (t.get("score") or 0) >= 91),
    ]
    bucket_stats = []
    for label, fn in buckets:
        g = [t for t in closed if fn(t)]
        if not g:
            continue
        s = _stats(g)
        bucket_stats.append((label, s))
        print(f"  {label}  {_bar(s, 12)}  {s['win_rate']}%  "
              f"n={s['n']}  avg={s['avg_pnl']:+.2f}%  PF={s['profit_factor']}"
              + _sample_warn(s["n"]))

    # Score monotonicity verdict
    if len(bucket_stats) >= 2:
        rates = [s["win_rate"] for _, s in bucket_stats]
        monotone = all(rates[i] <= rates[i+1] for i in range(len(rates)-1))
        if all(s["n"] >= MIN_SAMPLE for _, s in bucket_stats):
            if monotone:
                print(f"  ✓  higher score = higher win rate — scoring engine is calibrated")
            else:
                print(f"  ✗  score buckets NOT monotone — some weights likely miscalibrated")
        else:
            print(f"  ⚠  need {MIN_SAMPLE}+ trades per bucket to draw conclusions")

    # ── By exit reason ────────────────────────────────────────────────────────
    print("\n  BY EXIT REASON")
    for reason_key, label in [("target_hit","Target hit ✓"),("stop_hit","Stop hit ✗"),("eod","EOD close")]:
        g = [t for t in closed if t.get("close_reason","").startswith(reason_key.split("_")[0]+"_"+reason_key.split("_")[1] if "_" in reason_key else reason_key)]
        if not g:
            continue
        s = _stats(g)
        print(f"  {label:<14}  {_bar(s,12)}  {s['win_rate']}%  "
              f"n={s['n']}  avg={s['avg_pnl']:+.2f}%")

    # ── Data quality: sim method breakdown ────────────────────────────────────
    five_min  = [t for t in closed if t.get("sim_method") == "5min"]
    daily_ohc = [t for t in closed if t.get("sim_method") == "daily_ohlc"]
    print(f"\n  DATA QUALITY")
    print(f"  5-min replay  : {len(five_min):>3} trades  (accurate — first trigger wins)")
    print(f"  daily OHLC    : {len(daily_ohc):>3} trades  (slightly optimistic — ordering unknown)")
    if daily_ohc:
        print(f"  ⚠  {len(daily_ohc)} trades used daily fallback — run validator within 5 days of signal for full accuracy")
    if five_min and daily_ohc:
        s5 = _stats(five_min);  sd = _stats(daily_ohc)
        bias = round(sd["win_rate"] - s5["win_rate"], 1)
        print(f"  Optimism bias estimate: {bias:+.1f}pp  "
              f"(5-min {s5['win_rate']}% vs daily {sd['win_rate']}%)")

    # ── By exit time slot ─────────────────────────────────────────────────────
    exited_timed = [t for t in closed if t.get("exit_time") and t["exit_time"] != "—"]
    if exited_timed:
        print("\n  EXIT TIME DISTRIBUTION  (when do trades resolve?)")
        exit_slots = defaultdict(list)
        for t in exited_timed:
            exit_slots[_slot(t["exit_time"])].append(t)
        for slot in ["opening (9:30-10:00)", "morning (10:00-12:00)",
                     "midday  (12:00-14:00)", "afternoon(14:00-16:00)"]:
            g = exit_slots.get(slot, [])
            if not g:
                continue
            wins_here = sum(1 for t in g if (t.get("pnl_pct") or 0) > 0)
            print(f"  {slot}  n={len(g):>3}  "
                  f"WR={wins_here/len(g)*100:.0f}%  "
                  f"(target {sum(1 for t in g if 'target' in t.get('close_reason',''))}"
                  f" / stop {sum(1 for t in g if 'stop' in t.get('close_reason',''))}"
                  f" / eod {sum(1 for t in g if t.get('close_reason') == 'eod')})")

    # ── By symbol (top 10) ────────────────────────────────────────────────────
    print("\n  BY SYMBOL  (top 10 by trade count)")
    sym_groups = defaultdict(list)
    for t in closed:
        sym_groups[t["symbol"]].append(t)
    top = sorted(sym_groups, key=lambda k: -len(sym_groups[k]))[:10]
    for sym in top:
        g = sym_groups[sym]
        s = _stats(g)
        print(f"  {sym:<8}  {_bar(s, 12)}  {s['win_rate']}%  "
              f"n={s['n']}  avg={s['avg_pnl']:+.2f}%  PF={s['profit_factor']}"
              + _sample_warn(s["n"]))

    # ── By signal tag ─────────────────────────────────────────────────────────
    def _norm_tag(r):
        """Strip variable parts (prices, percentages, RSI values) for grouping."""
        s = re.sub(r'\s*\([\$\d\.,%x\+\-]+[^\)]*\)', '', r)   # "(204.90)" / "(1.5x)"
        s = re.sub(r'\s*\$[\d\.]+', '', s)                     # "$623.39"
        s = re.sub(r'\s*[+-]?\d+\.\d+%', '', s)                # "+4.4%"
        s = re.sub(r'\s+', ' ', s).strip()
        return s

    tag_groups = defaultdict(list)
    for t in closed:
        seen = set()
        for raw_r in (t.get("reasons") or []):
            tag = _norm_tag(raw_r)
            if tag and tag not in seen:
                tag_groups[tag].append(t)
                seen.add(tag)

    MIN_TAG_TRADES = 3
    ranked = sorted(
        [(tag, _stats(grp)) for tag, grp in tag_groups.items() if len(grp) >= MIN_TAG_TRADES],
        key=lambda x: -x[1]["profit_factor"] if x[1]["profit_factor"] != float("inf") else 999,
    )
    if ranked:
        print(f"\n  BY SIGNAL TAG  (≥{MIN_TAG_TRADES} trades, ranked by PF)")
        for tag, s in ranked:
            icon = "+" if s["profit_factor"] >= 1.5 else ("~" if s["profit_factor"] >= 1.0 else "-")
            pf_str = f"{s['profit_factor']}" if s["profit_factor"] != float("inf") else "∞"
            print(f"  [{icon}] {tag:<46}  n={s['n']:>3}  {s['win_rate']:>5.1f}%  "
                  f"avg={s['avg_pnl']:+.2f}%  PF={pf_str}")

    # ── Trend over time (last 5 runs from accuracy_stats.json) ───────────────
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE) as f:
                history = json.load(f)
            if len(history) >= 2:
                print(f"\n  TREND  (last {min(5, len(history))} logged runs)")
                for row in history[-5:]:
                    bar_filled = int(row["win_rate"] / 100 * 12)
                    b = "█" * bar_filled + "░" * (12 - bar_filled)
                    print(f"  {row['date']}  {b}  {row['win_rate']}%  "
                          f"n={row['n']}  avg={row['avg_pnl']:+.2f}%  PF={row['profit_factor']}")
        except Exception:
            pass

    print()


if __name__ == "__main__":
    main()
