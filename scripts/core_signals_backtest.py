#!/usr/bin/env python3
"""
core_signals_backtest.py  —  Walk-forward backtest for core_signals scoring engine

Replicates evaluate_stock() scoring on 1–2 years of daily bars without live
API calls (no news, no earnings, no 1h RSI). Weekly trend computed by
aggregating daily bars. RS ratio and regime use SPY closes.

Usage:
    python3 scripts/core_signals_backtest.py
    python3 scripts/core_signals_backtest.py --algo spx
    python3 scripts/core_signals_backtest.py --algo largecap --years 2
    python3 scripts/core_signals_backtest.py --hold 7 --min-score 6.0

Results are written to scripts/cs_backtest_results.json
"""

import sys, os, argparse, json, math, pickle, time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import yfinance as yf
    YF_OK = True
except ImportError:
    YF_OK = False
    print("yfinance not installed — run: pip install yfinance")
    sys.exit(1)

# ── Indicator math (mirror of core_signals.py) ────────────────────────────────

def _rsi(c, p=14):
    if len(c) < p + 1: return None
    g = l = 0.0
    for i in range(1, p + 1):
        d = c[i] - c[i-1]
        if d > 0: g += d
        else:     l -= d
    ag, al = g/p, l/p
    for i in range(p+1, len(c)):
        d = c[i] - c[i-1]
        ag = (ag*(p-1) + (d if d > 0 else 0)) / p
        al = (al*(p-1) + (abs(d) if d < 0 else 0)) / p
    rs = ag / (al or 1e-9)
    return round(100 - 100/(1+rs), 1)

def _ema(c, p):
    if len(c) < p: return None
    k, v = 2/(p+1), sum(c[:p])/p
    for x in c[p:]: v = x*k + v*(1-k)
    return round(v, 4)

def _sma(c, p):
    return round(sum(c[-p:])/p, 4) if len(c) >= p else None

def _cmf(h, l, c, v, p=20):
    n = min(len(h), len(l), len(c), len(v))
    if n < p: return None
    mfv = vol = 0.0
    for hi, lo, cl, vo in zip(h[-p:], l[-p:], c[-p:], v[-p:]):
        mfm = ((cl-lo)-(hi-cl))/(hi-lo) if hi != lo else 0.0
        mfv += mfm*vo; vol += vo
    return round(mfv/vol, 3) if vol else None

def _obv_trend(closes, vols):
    n = min(len(closes), len(vols))
    if n < 11: return 0
    obv, obvs = 0.0, []
    for i in range(1, n):
        if closes[i] > closes[i-1]:   obv += vols[i]
        elif closes[i] < closes[i-1]: obv -= vols[i]
        obvs.append(obv)
    recent = sum(obvs[-5:]) / 5
    prior  = sum(obvs[-10:-5]) / 5
    if prior == 0: return 0
    if recent > prior * 1.02: return  1
    if recent < prior * 0.98: return -1
    return 0

def _mfi(h, l, c, v, p=14):
    n = min(len(h), len(l), len(c), len(v))
    if n < p + 1: return None
    pos = neg = 0.0
    for i in range(n-p, n):
        tp      = (h[i] + l[i] + c[i]) / 3
        tp_prev = (h[i-1] + l[i-1] + c[i-1]) / 3
        mf = tp * v[i]
        if tp > tp_prev: pos += mf
        else:            neg += mf
    return round(100 - 100/(1 + pos/(neg or 1e-9)), 1)

def _bb_compressed(closes, p=20):
    if len(closes) < p + 30: return False
    def _w(sl):
        m = sum(sl)/len(sl)
        std = (sum((x-m)**2 for x in sl)/len(sl))**0.5
        return (4*std/m) if m else 0
    cur  = _w(closes[-p:])
    hist = [_w(closes[i:i+p]) for i in range(len(closes)-p-30, len(closes)-p)]
    hist = [w for w in hist if w > 0]
    if not hist: return False
    return cur <= sorted(hist)[int(len(hist)*0.2)]

def _rs_ratio(sc, bc):
    def _f(c, lb): return c[-1]/c[-lb] if len(c) >= lb and c[-lb] else 1.0
    if len(sc) < 64 or len(bc) < 64: return None
    n = min(len(sc), len(bc))
    lb4 = min(253, n)
    s = _f(sc,64)*0.4 + _f(sc,127)*0.2 + _f(sc,190)*0.2 + _f(sc,lb4)*0.2
    b = _f(bc,64)*0.4 + _f(bc,127)*0.2 + _f(bc,190)*0.2 + _f(bc,lb4)*0.2
    return round(s/max(b, 0.001), 2)

def _weekly_trend(closes):
    """Aggregate daily closes to weekly, check EMA9 > EMA21."""
    if len(closes) < 21*5: return None
    weekly = [sum(closes[i:i+5])/5 for i in range(0, len(closes)-4, 5)]
    if len(weekly) < 21: return None
    e9  = _ema(weekly, 9)
    e21 = _ema(weekly, 21)
    if e9 is None or e21 is None: return None
    return e9 > e21

def _trade_levels(price, highs, lows):
    ranges_5d = [(highs[i]-lows[i])/lows[i]*100 for i in range(-5, 0)
                 if abs(i) <= len(highs) and lows[i] > 0]
    vol = round(sum(ranges_5d)/len(ranges_5d), 2) if ranges_5d else 3.0
    stop_pct  = max(4.5, min(9.0, vol*1.2))
    stop      = round(price*(1-stop_pct/100), 2)
    rh30      = highs[-30:] if len(highs) >= 30 else highs
    rl30      = lows[-30:]  if len(lows)  >= 30 else lows
    resistance = round(sum(sorted(rh30, reverse=True)[:3])/3, 2) if len(rh30) >= 3 else max(rh30)
    support    = round(sum(sorted(rl30)[:3])/3, 2)               if len(rl30) >= 3 else min(rl30)
    if support < price:
        stop = round(min(stop, support*0.985), 2)
    reward_pct = max(stop_pct*1.8, 8.0)
    target = round(price*(1+reward_pct/100), 2)
    if resistance > price:
        target = round(min(target, resistance*0.985), 2)
    if target <= price: target = round(price*1.08, 2)
    if stop   >= price: stop   = round(price*0.94, 2)
    rr = round((target-price)/max(price-stop, 0.01), 2)
    return stop, target, rr, support, resistance

# ── Build snapshot dict ────────────────────────────────────────────────────────

def _build_snapshot(closes, highs, lows, opens, vols, spy_closes, D):
    """Build the stock dict `s` for bar index D using data[0:D+1]."""
    c = closes[:D+1]; h = highs[:D+1]; l = lows[:D+1]
    o = opens[:D+1];  v = vols[:D+1];  bc = spy_closes[:D+1]

    price  = c[-1]
    prev   = c[-2] if len(c) >= 2 else price
    change = (price-prev)/prev*100 if prev else 0

    # Volume spike
    vol_today = v[-1] if v else 0
    prior_v   = v[:-1] if len(v) > 1 else v
    avg_vol   = sum(prior_v[-20:])/max(len(prior_v[-20:]), 1) if prior_v else 0
    vol_spike = round(vol_today/avg_vol, 1) if avg_vol > 0 else 1.0

    # Returns
    ret7  = (price-c[-8])/c[-8]*100  if len(c) >= 8  else 0
    ret30 = (price-c[-31])/c[-31]*100 if len(c) >= 31 else 0

    # Gap
    gap_pct = round((o[-1]-prev)/prev*100, 1) if o and prev else 0.0

    # Indicators
    rsi_d  = _rsi(c)
    e9     = _ema(c, 9);  e21 = _ema(c, 21)
    e12    = _ema(c, 12); e26 = _ema(c, 26)
    macd   = round(e12-e26, 4) if e12 and e26 else None
    sma20  = _sma(c, 20);  sma50 = _sma(c, 50); sma200 = _sma(c, 200)
    cmf20  = _cmf(h, l, c, v)
    obv    = _obv_trend(c, v)
    mfi14  = _mfi(h, l, c, v)
    bb_sq  = _bb_compressed(c)
    rs     = _rs_ratio(list(c), list(bc))
    weekly = _weekly_trend(list(c))

    # 52-week high
    hi52   = max(h[-252:]) if len(h) >= 10 else max(h)
    lo52   = min(l[-252:]) if len(l) >= 10 else min(l)

    # Support / resistance / stop / target
    stop, target, rr, support, resistance = _trade_levels(price, list(h), list(l))
    support_gap    = (price-support)/price*100    if support    else None
    resistance_gap = (resistance-price)/price*100 if resistance else None

    return {
        "price": price, "change": change, "vol_spike": vol_spike,
        "ret7": ret7, "ret30": ret30, "gap_pct": gap_pct,
        "rsi_d": rsi_d, "rsi_1h": None,
        "ema9": e9, "ema21": e21, "macd": macd,
        "sma20": sma20, "sma50": sma50, "sma200": sma200,
        "cmf20": cmf20, "obv_trend": obv, "mfi14": mfi14,
        "bb_compressed": bb_sq, "rs_ratio": rs, "weekly_trend": weekly,
        "hi52": hi52, "lo52": lo52,
        "support": support, "resistance": resistance,
        "support_gap": support_gap, "resistance_gap": resistance_gap,
        "stop": stop, "target": target, "rr": rr,
        "sentiment": {"score": 0},   # no news in backtest
        "sector_rsi_delta": None,    # no sector context
        "beta_index": None,
    }

# ── Scoring (mirrors evaluate_stock, minus live API calls) ────────────────────

def _score(s, spy_above_50, buy_score=6.5):
    score, reasons, warnings = 0.0, [], []

    if not spy_above_50:
        score -= 3.0; warnings.append("bear regime")

    rs = s.get("rs_ratio")
    if rs is not None:
        if rs >= 1.3:   score += 1.2
        elif rs >= 1.1: score += 0.5
        elif rs < 0.8:  score -= 0.8

    weekly = s.get("weekly_trend")
    if weekly is True:  score += 1.2
    elif weekly is False: score -= 2.0

    rsi = s.get("rsi_d")
    if rsi is not None:
        if 30 <= rsi <= 45: score += 2.0
        elif rsi < 30:      score += 1.2
        elif rsi > 72:      score -= 1.0

    # 1h RSI skip → warning only, no score

    if s.get("ema9") and s.get("ema21"):
        if s["ema9"] > s["ema21"]: score += 1.5
        else:                      score -= 1.0

    dma = 0.0
    p   = s["price"]
    if s.get("sma20")  and p > s["sma20"]:  dma += 0.5
    if s.get("sma50")  and p > s["sma50"]:  dma += 0.7
    if s.get("sma200") and p > s["sma200"]: dma += 0.9
    score += dma
    if s.get("sma20") and s.get("sma50") and s.get("sma200"):
        if s["sma20"] > s["sma50"] > s["sma200"]:  score += 0.9
        elif s["sma20"] < s["sma50"] < s["sma200"]: score -= 0.5

    hi52 = s.get("hi52")
    if hi52 and hi52 > 0:
        pct_from_hi = (hi52-p)/hi52*100
        if pct_from_hi <= 3:    score += 1.2
        elif pct_from_hi <= 15: score += 0.8
        elif pct_from_hi <= 25: score += 0.3
        elif pct_from_hi > 30:  score -= 0.8

    cmf = s.get("cmf20")
    if cmf is not None:
        if cmf >= 0.10:   score += 1.0
        elif cmf <= -0.10: score -= 0.5

    obv = s.get("obv_trend", 0)
    ret = s.get("ret7", 0)
    if obv == 1:
        score += 0.9 if ret <= 0 else 0.4
    elif obv == -1:
        score -= 0.8 if ret >= 0 else 0.3

    mfi = s.get("mfi14")
    if mfi is not None:
        if 25 <= mfi <= 45: score += 0.8
        elif mfi > 75:      score -= 0.6

    sm_count = sum([
        s.get("vol_spike", 0) >= 1.5,
        (s.get("cmf20") or 0) >= 0.10,
        s.get("obv_trend", 0) == 1,
        25 <= (s.get("mfi14") or 0) <= 55,
    ])
    if sm_count >= 4:   score += 2.0
    elif sm_count >= 3: score += 1.0

    if s.get("bb_compressed"):
        score += 1.5 if s.get("obv_trend", 0) == 1 else 0.7

    if s.get("macd") is not None:
        score += 0.8 if s["macd"] > 0 else -0.5

    vs = s.get("vol_spike", 0)
    if vs >= 1.8:   score += 1.6
    elif vs < 1.0:  score -= 0.4

    sg = s.get("support_gap")
    if sg is not None:
        if sg <= 3.0:   score += 1.5
        elif sg >= 8.0: score -= 0.8

    rg = s.get("resistance_gap")
    if rg is not None:
        if rg <= 2.0:   score -= 1.0
        elif rg >= 6.0: score += 0.7

    ch = s.get("change", 0)
    if ch > 8:           score -= 0.5
    elif -2 <= ch <= 4:  score += 0.5

    gap = s.get("gap_pct", 0)
    if gap >= 3.0 and vs >= 1.5: score += 1.2
    elif gap >= 1.5:             score += 0.5
    elif gap <= -3.0:            score -= 0.6

    rr = s.get("rr", 1.0)
    if rr >= 1.8:   score += 0.8
    elif rr < 1.1:  score -= 0.5

    buy_ok = (rr >= 1.5 and rg is not None and rg > 2.0)
    signal = "SKIP"
    if score >= buy_score and buy_ok: signal = "BUY"
    elif score >= buy_score - 2.0:    signal = "WATCH"

    return round(score, 2), signal

# ── Data fetch ────────────────────────────────────────────────────────────────

_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bar_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)

def _fetch_daily(symbol, years=1):
    safe_sym = symbol.replace("/", "-").replace(".", "_")
    cache_file = os.path.join(_CACHE_DIR, f"{safe_sym}_daily_{years}y.pkl")
    if os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < 86400:  # 24h cache
            with open(cache_file, "rb") as f:
                return pickle.load(f)

    try:
        period = f"{years}y"
        df = yf.download(symbol, period=period, interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 50:
            return None

        # Flatten MultiIndex columns if present
        if hasattr(df.columns, 'levels'):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

        result = {
            "dates":  [str(d.date()) for d in df.index],
            "opens":  list(df["Open"].astype(float)),
            "highs":  list(df["High"].astype(float)),
            "lows":   list(df["Low"].astype(float)),
            "closes": list(df["Close"].astype(float)),
            "vols":   list(df["Volume"].astype(float)),
        }
        with open(cache_file, "wb") as f:
            pickle.dump(result, f)
        return result
    except Exception as e:
        print(f"  warn: {symbol} — {e}")
        return None

# ── Walk-forward simulation ────────────────────────────────────────────────────

def _simulate(symbol, data, spy_data, buy_score, hold_days, min_bars=250):
    closes = data["closes"]; highs = data["highs"]
    lows   = data["lows"];   opens = data["opens"]
    vols   = data["vols"];   dates = data["dates"]
    spy_c  = spy_data["closes"]

    trades = []
    last_entry_bar = -999  # cooldown: one trade per symbol at a time

    for D in range(min_bars, len(closes) - hold_days - 1):
        if D - last_entry_bar < 5:  # cooldown 5 bars between signals
            continue

        # Align spy to same length
        spy_slice = spy_c[:D+1]
        spy_above_50 = (len(spy_slice) >= 50 and
                        spy_slice[-1] > sum(spy_slice[-50:])/50)

        s = _build_snapshot(closes, highs, lows, opens, vols, spy_slice, D)

        # Hard volume filter: require a real volume surge on the signal day
        # (reduces timeout trades — setups without volume conviction rarely follow through)
        if s.get("vol_spike", 0) < 1.5:
            continue

        score, signal = _score(s, spy_above_50, buy_score)

        if signal != "BUY":
            continue

        entry = closes[D]
        stop   = s["stop"]
        target = s["target"]

        # Simulate exit
        exit_price = None; exit_day = None; outcome = "timeout"
        for fwd in range(1, hold_days + 1):
            if D + fwd >= len(closes): break
            future_close = closes[D + fwd]
            if future_close <= stop:
                exit_price = future_close; exit_day = fwd; outcome = "stop"; break
            if future_close >= target:
                exit_price = future_close; exit_day = fwd; outcome = "target"; break
        if exit_price is None:
            exit_price = closes[min(D + hold_days, len(closes)-1)]
            exit_day   = hold_days

        pnl_pct = round((exit_price - entry) / entry * 100, 2)

        trades.append({
            "symbol":    symbol,
            "date":      dates[D],
            "score":     score,
            "entry":     round(entry, 2),
            "stop":      round(stop, 2),
            "target":    round(target, 2),
            "exit":      round(exit_price, 2),
            "exit_day":  exit_day,
            "outcome":   outcome,
            "pnl_pct":   pnl_pct,
            "win":       pnl_pct > 0,
        })
        last_entry_bar = D

    return trades

# ── Report ────────────────────────────────────────────────────────────────────

def _report(all_trades, buy_score, hold_days, is_oos_split=0.67):
    if not all_trades:
        print("  No trades generated — try lowering --min-score")
        return {}

    all_trades.sort(key=lambda t: t["date"])
    split_idx = int(len(all_trades) * is_oos_split)
    is_trades  = all_trades[:split_idx]
    oos_trades = all_trades[split_idx:]

    def _stats(trades, label):
        if not trades:
            print(f"  {label}: 0 trades")
            return {}
        wins    = [t for t in trades if t["win"]]
        losses  = [t for t in trades if not t["win"]]
        wr      = len(wins)/len(trades)*100
        avg_win  = sum(t["pnl_pct"] for t in wins)  /max(len(wins),1)
        avg_loss = sum(t["pnl_pct"] for t in losses)/max(len(losses),1)
        avg_pnl  = sum(t["pnl_pct"] for t in trades)/len(trades)
        targets  = sum(1 for t in trades if t["outcome"] == "target")
        stops    = sum(1 for t in trades if t["outcome"] == "stop")
        timeouts = sum(1 for t in trades if t["outcome"] == "timeout")
        expectancy = round((wr/100)*avg_win + (1-wr/100)*avg_loss, 2)

        print(f"\n  {'─'*54}")
        print(f"  {label}  ({len(trades)} trades, hold≤{hold_days}d, score≥{buy_score})")
        print(f"  {'─'*54}")
        print(f"  Win Rate  : {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Avg P&L   : {avg_pnl:+.2f}%  |  Avg Win: {avg_win:+.2f}%  |  Avg Loss: {avg_loss:+.2f}%")
        print(f"  Expectancy: {expectancy:+.2f}% per trade")
        print(f"  Exits     : {targets} targets  {stops} stops  {timeouts} timeouts")

        # By score bucket
        buckets = [(9.0, 99, "≥9.0 (high conf)"),
                   (7.5, 9.0, "7.5–9.0"),
                   (6.5, 7.5, "6.5–7.5"),
                   (5.0, 6.5, "5.0–6.5 (watch)")]
        print(f"\n  Score Bucket        Trades   WR       Avg P&L   Expect")
        print(f"  {'─'*54}")
        for lo, hi, name in buckets:
            bt = [t for t in trades if lo <= t["score"] < hi]
            if not bt: continue
            bw   = [t for t in bt if t["win"]]
            bwr  = len(bw)/len(bt)*100
            bpnl = sum(t["pnl_pct"] for t in bt)/len(bt)
            bw_avg  = sum(t["pnl_pct"] for t in bw)/max(len(bw),1)
            bl_avg  = sum(t["pnl_pct"] for t in bt if not t["win"])/max(len(bt)-len(bw),1)
            bexp    = round((bwr/100)*bw_avg + (1-bwr/100)*bl_avg, 2)
            verdict = "✅" if bwr >= 52 and bexp > 0 else ("⚠️ " if bwr >= 42 else "❌")
            print(f"  {name:<20}  {len(bt):5d}  {bwr:5.1f}%   {bpnl:+6.2f}%   {bexp:+.2f}  {verdict}")

        # Top 10 symbols by win rate (≥5 trades)
        from collections import defaultdict
        sym_trades = defaultdict(list)
        for t in trades:
            sym_trades[t["symbol"]].append(t)
        sym_stats = [(sym, ts) for sym, ts in sym_trades.items() if len(ts) >= 5]
        sym_stats.sort(key=lambda x: -sum(1 for t in x[1] if t["win"])/len(x[1]))
        if sym_stats:
            print(f"\n  Top symbols (≥5 trades)")
            print(f"  {'Symbol':<10}  {'Trades':>6}  {'WR':>6}  {'AvgP&L':>8}")
            print(f"  {'─'*38}")
            for sym, ts in sym_stats[:10]:
                w   = sum(1 for t in ts if t["win"])
                wr2 = w/len(ts)*100
                ap  = sum(t["pnl_pct"] for t in ts)/len(ts)
                print(f"  {sym:<10}  {len(ts):>6}  {wr2:>5.1f}%  {ap:>+7.2f}%")

        return {
            "trades": len(trades), "win_rate": round(wr,1),
            "avg_pnl": round(avg_pnl,2), "expectancy": expectancy,
            "targets": targets, "stops": stops,
        }

    print(f"\n{'═'*58}")
    print(f"  CORE SIGNALS BACKTEST  —  hold ≤{hold_days}d  score ≥{buy_score}")
    print(f"{'═'*58}")

    is_res  = _stats(is_trades,  f"IN-SAMPLE  ({is_trades[0]['date'] if is_trades else '?'}  →  {is_trades[-1]['date'] if is_trades else '?'})")
    oos_res = _stats(oos_trades, f"OUT-OF-SAMPLE  ({oos_trades[0]['date'] if oos_trades else '?'}  →  {oos_trades[-1]['date'] if oos_trades else '?'})")

    # Degradation check
    if is_res and oos_res:
        wr_drop = is_res["win_rate"] - oos_res["win_rate"]
        pnl_drop = is_res["avg_pnl"] - oos_res["avg_pnl"]
        print(f"\n  {'─'*54}")
        print(f"  IS→OOS degradation: WR {wr_drop:+.1f}pp  |  Avg P&L {pnl_drop:+.2f}pp")
        if wr_drop > 10:
            print("  ⚠️  Large WR drop — possible overfit to IS period")
        elif wr_drop > 5:
            print("  ⚠️  Moderate drop — monitor live carefully")
        else:
            print("  ✅  Stable IS→OOS — strategy generalises well")
    print()

    return {"is": is_res, "oos": oos_res, "total": len(all_trades)}

# ── Main ──────────────────────────────────────────────────────────────────────

SYMBOLS = {
    "largecap": [
        # Mirrors day_trading.py _RAW_SYMBOLS exactly (ETF proxies excluded)
        # Mega-cap tech
        "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AVGO",
        "ORCL","CRM","ADBE","NFLX",
        # Semiconductors
        "AMD","QCOM","MU","AMAT","KLAC","TXN","SMCI",
        # Cloud / cybersecurity
        "SNOW","DDOG","WDAY","ZS","NET","CRWD","PANW","FTNT",
        # Financials
        "JPM","BAC","GS","MS","V","MA",
        # Energy
        "XOM","CVX","OXY","SLB",
        # Consumer / travel / health
        "COST","PEP","SBUX","BKNG","ISRG","UBER","ABNB",
        # Canadian large cap (yfinance uses .TO suffix)
        "RY.TO","TD.TO","BNS.TO","BMO.TO","CM.TO",
        "CNQ.TO","ENB.TO","SU.TO","CVE.TO",
        "ABX.TO","AEM.TO","FNV.TO","WPM.TO",
        "SHOP.TO","CSU.TO","BAM.TO","CP.TO","CNR.TO",
    ],
    "spx": [
        # Removed: NOW (PF 0.37), MRVL (PF 0.62), LLY (PF 0.44), MRNA (PF 0.61)
        "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AVGO",
        "AMD","QCOM","MU","AMAT","KLAC","TXN","ON","NXPI",
        "SNOW","DDOG","WDAY","ZS","NET","CRWD","PANW","FTNT","ADBE","INTU",
        "NFLX","BKNG","ABNB","UBER","COIN",
        "COST","PEP","KO","WMT","SBUX","MCD","NKE","HD",
        "ISRG","ABBV","UNH","AMGN","GILD",
        "JPM","GS","MS","BAC","V","MA","PYPL",
        "XOM","CVX","COP","SLB","OXY",
        "CAT","DE","HON","GE","RTX","BA","UPS",
    ],
    "smallcap": [
        # Removed: PLTR (PF 0.61), SNAP (PF 0.54), PENN (PF 0.49)
        "RBLX","PINS","RDDT","SOFI","COIN","MSTR",
        "LCID","RIVN","SPCE","JOBY",
        "SOUN","BBAI","IONQ","RGTI","QUBT",
        "HOOD","AFRM","UPST","PYPL",
        "DKNG","MGM","LVS",
    ],
}


def main():
    ap = argparse.ArgumentParser(description="Core signals walk-forward backtest")
    ap.add_argument("--algo",      default="largecap", choices=["largecap","spx","smallcap"])
    ap.add_argument("--years",     type=int,   default=1,   help="Years of history (1 or 2)")
    ap.add_argument("--hold",      type=int,   default=10,  help="Max hold days (default 10)")
    ap.add_argument("--min-score", type=float, default=9.0, help="BUY signal threshold (default 9.0)")
    ap.add_argument("--no-cache",  action="store_true",     help="Force re-download all data")
    args = ap.parse_args()

    symbols   = SYMBOLS[args.algo]
    buy_score = args.min_score
    hold_days = args.hold
    years     = args.years

    if args.no_cache:
        for f in os.listdir(_CACHE_DIR):
            if f.endswith("_daily_") or f.endswith(".pkl"):
                os.remove(os.path.join(_CACHE_DIR, f))

    print(f"\n{'═'*58}")
    print(f"  Core Signals Backtest")
    print(f"  Algo: {args.algo}  |  Symbols: {len(symbols)}  |  Years: {years}")
    print(f"  Hold: ≤{hold_days} days  |  Buy score: ≥{buy_score}")
    print(f"{'═'*58}")

    # Fetch SPY (benchmark + regime)
    print("\n  Downloading SPY benchmark...")
    spy_data = _fetch_daily("SPY", years)
    if not spy_data:
        print("  ERROR: could not fetch SPY — check internet connection")
        sys.exit(1)

    # Fetch all symbols
    print(f"  Downloading {len(symbols)} symbols ({years}y daily bars)...")
    stock_data = {}
    for i, sym in enumerate(symbols, 1):
        d = _fetch_daily(sym, years)
        if d and len(d["closes"]) >= 260:
            stock_data[sym] = d
        pct = i / len(symbols) * 100
        print(f"  [{i:3d}/{len(symbols)}] {sym:<12} {'✓' if d else '✗'}  {pct:.0f}%", end="\r")
    print(f"\n  {len(stock_data)}/{len(symbols)} symbols loaded")

    # Run walk-forward simulation
    all_trades = []
    for sym, data in stock_data.items():
        trades = _simulate(sym, data, spy_data, buy_score, hold_days)
        all_trades.extend(trades)

    # Report
    results = _report(all_trades, buy_score, hold_days)

    # Save JSON results
    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cs_backtest_results.json")
    payload  = {
        "algo":      args.algo,
        "years":     years,
        "hold_days": hold_days,
        "buy_score": buy_score,
        "symbols":   len(stock_data),
        "results":   results,
        "trades":    all_trades[-200:],  # last 200 for inspection
        "run_at":    datetime.now().isoformat(),
    }
    with open(out_file, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"  Full results → {out_file}\n")


if __name__ == "__main__":
    main()
