"""
core_signals.py — shared AlgoEngine class used by signals.py, largecapsignals.py, SPXindex.py.

Each wrapper file instantiates AlgoEngine with its own config (price range, seeds, strategy
thresholds) and exposes module-level shims so daily_scan.py / api.py / daily_validate.py
continue to work without changes.
"""

import sys, subprocess, requests, json, os, re, time, threading, random
import xml.etree.ElementTree as ET
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    from rich.progress import Progress, SpinnerColumn, TextColumn
    RICH = True
except ImportError:
    RICH = False

console = Console() if RICH else None

# ── Module-level shared state ─────────────────────────────────────────────────
REQUEST_HEADERS  = {"User-Agent": "Mozilla/5.0"}
QUOTE_CACHE      = {}
QUOTE_CACHE_TTL  = 300
LAST_REQUEST_AT  = 0.0
_BENCHMARK_CACHE = {}
_cache_lock      = threading.Lock()

BULLISH_WORDS = [
    "surge","soar","rally","jump","gain","beat","upgrade","buy","bullish",
    "record","high","strong","growth","profit","revenue","deal","contract",
    "partnership","approved","breakthrough","launch","wins","rises","climbs",
]
BEARISH_WORDS = [
    "crash","drop","fall","plunge","miss","downgrade","sell","bearish","loss",
    "low","weak","decline","lawsuit","investigation","delay","cut","warns",
    "disappoints","sinks","tumbles","layoff","bankruptcy","fraud","recall",
]
KNOWN_COMMANDS = {
    "buy","sell","import","setstop","settarget","portfolio","price",
    "idea","why","review","ask","scan","news","accuracy","validate","quit",
}

# ── Indicator math ────────────────────────────────────────────────────────────
def _calc_rsi(c, period=14):
    """Wilder's smoothed RSI — seeds on first `period` bars, then exponentially smooths."""
    if len(c) < period + 1:
        return None
    g = l = 0.0
    for i in range(1, period + 1):
        d = c[i] - c[i - 1]
        if d > 0: g += d
        else:     l -= d
    avg_g, avg_l = g / period, l / period
    for i in range(period + 1, len(c)):
        d = c[i] - c[i - 1]
        avg_g = (avg_g * (period - 1) + (d if d > 0 else 0)) / period
        avg_l = (avg_l * (period - 1) + (abs(d) if d < 0 else 0)) / period
    rs = avg_g / (avg_l or 1e-9)
    return round(100 - 100 / (1 + rs), 1)

def _calc_ema(c, p):
    if len(c)<p: return None
    k,v = 2/(p+1), sum(c[:p])/p
    for x in c[p:]: v=x*k+v*(1-k)
    return round(v,4)

def _calc_sma(c, p):
    return round(sum(c[-p:])/p,4) if len(c)>=p else None

def _calc_cmf(h,l,c,v,period=20):
    n=min(len(h),len(l),len(c),len(v))
    if n<period: return None
    mfv=vol=0.0
    for hi,lo,cl,vo in zip(h[-period:],l[-period:],c[-period:],v[-period:]):
        mfm=((cl-lo)-(hi-cl))/(hi-lo) if hi!=lo else 0.0
        mfv+=mfm*vo; vol+=vo
    return round(mfv/vol,3) if vol else None

def _calc_obv_trend(closes, vols):
    """OBV slope: +1=rising, -1=falling, 0=flat."""
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
    if recent > prior * 1.02:  return 1
    if recent < prior * 0.98:  return -1
    return 0

def _calc_mfi(h, l, c, v, period=14):
    """Money Flow Index — volume-weighted RSI, 0-100."""
    n = min(len(h), len(l), len(c), len(v))
    if n < period + 1: return None
    pos = neg = 0.0
    for i in range(n - period, n):
        tp      = (h[i] + l[i] + c[i]) / 3
        tp_prev = (h[i-1] + l[i-1] + c[i-1]) / 3
        mf = tp * v[i]
        if tp > tp_prev: pos += mf
        else:            neg += mf
    return round(100 - 100 / (1 + pos / (neg or 1e-9)), 1)

def _calc_rs_ratio(sc, bc):
    """IBD-style weighted RS vs benchmark. >1 = outperforming."""
    def _f(c, lb): return c[-1] / c[-lb] if len(c) >= lb and c[-lb] else 1.0
    if len(sc) < 64 or len(bc) < 64: return None
    n = min(len(sc), len(bc))
    lb4 = min(253, n)
    s = _f(sc,64)*0.4 + _f(sc,127)*0.2 + _f(sc,190)*0.2 + _f(sc,lb4)*0.2
    b = _f(bc,64)*0.4 + _f(bc,127)*0.2 + _f(bc,190)*0.2 + _f(bc,lb4)*0.2
    return round(s / max(b, 0.001), 2)

def _calc_bb_compression(closes, period=20):
    """Bollinger Band width. Returns (compressed: bool, width_pct: float)."""
    if len(closes) < period + 30: return False, None
    def _w(c_slice):
        m = sum(c_slice) / len(c_slice)
        std = (sum((x-m)**2 for x in c_slice) / len(c_slice)) ** 0.5
        return (4 * std / m) if m else 0
    cur  = _w(closes[-period:])
    hist = [_w(closes[i:i+period]) for i in range(len(closes)-period-30, len(closes)-period)]
    hist = [w for w in hist if w > 0]
    if not hist: return False, round(cur*100, 2)
    cutoff = sorted(hist)[int(len(hist) * 0.2)]
    return cur <= cutoff, round(cur * 100, 2)

def _detect_rsi_divergence(closes, period=14, lookback=20):
    """
    Scan last `lookback` bars for RSI divergence using a single Wilder-smoothed pass.
    Returns 'bullish' (price lower-low + RSI higher-low) or 'bearish' (price
    higher-high + RSI lower-high), or None when neither condition is met.
    The 1% price threshold and 3-pt RSI gap filter out noise.
    """
    n = len(closes)
    if n < period + lookback + 2:
        return None

    window = closes[-(period + lookback + 2):]
    # Build RSI series in one Wilder pass
    g = l = 0.0
    for i in range(1, period + 1):
        d = window[i] - window[i - 1]
        if d > 0: g += d
        else:     l -= d
    ag, al = g / period, l / period
    rsi_series = []
    for i in range(period + 1, len(window)):
        d  = window[i] - window[i - 1]
        ag = (ag * (period - 1) + (d if d > 0 else 0)) / period
        al = (al * (period - 1) + (abs(d) if d < 0 else 0)) / period
        rsi_series.append(round(100 - 100 / (1 + ag / (al or 1e-9)), 1))

    if len(rsi_series) < lookback + 1:
        return None

    rsi_w   = rsi_series[-(lookback + 1):]
    price_w = list(window[-(lookback + 1):])
    cur_p, cur_r = price_w[-1], rsi_w[-1]
    prev_p, prev_r = price_w[:-1], rsi_w[:-1]

    min_p = min(prev_p)
    min_i = prev_p.index(min_p)
    if cur_p < min_p * 0.99 and cur_r > prev_r[min_i] + 3:
        return "bullish"

    max_p = max(prev_p)
    max_i = prev_p.index(max_p)
    if cur_p > max_p * 1.01 and cur_r < prev_r[max_i] - 3:
        return "bearish"

    return None

# ── Module-level helpers ──────────────────────────────────────────────────────
def cprint(msg, style=""):
    if RICH: console.print(msg, style=style)
    else:    print(msg)

def throttle_request(min_gap=0.35):
    global LAST_REQUEST_AT
    now  = time.time()
    wait = min_gap - (now - LAST_REQUEST_AT)
    if wait > 0:
        time.sleep(wait + random.uniform(0.05, 0.15))
    LAST_REQUEST_AT = time.time()

def safe_get(url, timeout=8, tries=4):
    last_err = None
    for attempt in range(tries):
        try:
            throttle_request()
            r = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
            if r.status_code == 429:
                time.sleep((attempt + 1) * 2.0 + random.uniform(0.3, 0.8))
                last_err = RuntimeError(f"429 on {url}")
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            time.sleep((attempt + 1) * 1.3 + random.uniform(0.1, 0.4))
    raise last_err

def cache_get(key):
    with _cache_lock:
        item = QUOTE_CACHE.get(key)
        if not item or time.time() - item["ts"] > QUOTE_CACHE_TTL:
            return None
        return item["value"]

def cache_set(key, value):
    with _cache_lock:
        QUOTE_CACHE[key] = {"ts": time.time(), "value": value}

def format_shares(v):
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:.4f}".rstrip("0").rstrip(".")
    except Exception:
        return str(v)

def parse_trade_cmd(cmd):
    """Returns (sym, shares, price) or raises ValueError."""
    if len(cmd) < 4:
        raise ValueError("Usage: buy/sell TICKER SHARES PRICE  e.g. buy MARA 5 10.50")
    sym = cmd[1].upper().strip()
    if not sym:
        raise ValueError("Ticker cannot be empty.")
    try:
        shares = float(cmd[2])
    except ValueError:
        raise ValueError(f"Shares must be a number, got: {cmd[2]!r}")
    if shares <= 0:
        raise ValueError("Shares must be positive.")
    try:
        price = float(cmd[3])
    except ValueError:
        raise ValueError(f"Price must be a number, got: {cmd[3]!r}")
    if price <= 0:
        raise ValueError("Price must be positive.")
    return sym, shares, price

def score_sentiment(text):
    t = text.lower()
    bull = sum(1 for w in BULLISH_WORDS if w in t)
    bear = sum(1 for w in BEARISH_WORDS if w in t)
    if bull > bear: return "bullish", bull-bear
    if bear > bull: return "bearish", bear-bull
    return "neutral", 0

# ── Module-level fetch functions (shared; no config dependency) ───────────────
def fetch_stock(symbol, include_hourly=True, long_history=False):
    """60d by default. long_history=True for 200DMA/beta."""
    yahoo_sym = symbol.upper().strip()
    cache_key = f"stock::{yahoo_sym}::h{include_hourly}::lh{long_history}"
    cached = cache_get(cache_key)
    if cached is not None:
        return dict(cached)

    result   = {"symbol": symbol, "ok": False}

    try:
        import ibkr_data as _id
        duration = "1 Y" if long_history else "60 D"
        _bars = _id.get_daily_bars(yahoo_sym, duration)
        if not _bars:
            result["error"] = "no data for any range"
            cache_set(cache_key, dict(result))
            return result
        closes = [b["close"]  for b in _bars]
        vols   = [b["volume"] for b in _bars]
        highs  = [b["high"]   for b in _bars]
        lows   = [b["low"]    for b in _bars]
        opens  = [b["open"]   for b in _bars]
    except Exception as _e:
        result["error"] = str(_e)
        cache_set(cache_key, dict(result))
        return result

    try:
        if not closes: raise ValueError("no price")

        price  = closes[-1]
        prev   = closes[-2] if len(closes) >= 2 else price
        change = (price-prev)/prev*100 if prev else 0
        gap_pct = round((opens[-1] - prev) / prev * 100, 1) if opens and prev else 0.0

        vol_today  = vols[-1] if vols else 0
        _prior     = vols[:-1] if len(vols) > 1 else vols
        avg_vol    = sum(_prior[-20:]) / max(len(_prior[-20:]), 1) if _prior else 0
        vol_spike  = round(vol_today / avg_vol, 1) if avg_vol > 0 else 1.0

        ret7  = (price-closes[-8]) /closes[-8] *100 if len(closes)>=8  else 0
        ret30 = (price-closes[-31])/closes[-31]*100 if len(closes)>=31 \
                else ((price-closes[0])/closes[0]*100 if closes else 0)

        rsi_d = _calc_rsi(closes)
        e9    = _calc_ema(closes,9);  e21 = _calc_ema(closes,21)
        e12   = _calc_ema(closes,12); e26 = _calc_ema(closes,26)
        macd  = round(e12-e26,4) if e12 and e26 else None
        sma20 = _calc_sma(closes,20); sma50 = _calc_sma(closes,50)
        sma200= _calc_sma(closes,200)
        cmf20         = _calc_cmf(highs,lows,closes,vols,20)
        obv_trend     = _calc_obv_trend(closes, vols)
        mfi14         = _calc_mfi(highs, lows, closes, vols, 14)
        bb_compressed, bb_width = _calc_bb_compression(closes)

        rh30 = highs[-30:] if len(highs)>=30 else highs
        rl30 = lows[-30:]  if len(lows) >=30 else lows
        resistance = round(sum(sorted(rh30,reverse=True)[:3])/3,2) if len(rh30)>=3 \
                     else (round(max(rh30),2) if rh30 else None)
        support    = round(sum(sorted(rl30)[:3])/3,2) if len(rl30)>=3 \
                     else (round(min(rl30),2) if rl30 else None)
        dist_res = round((resistance-price)/price*100,1) if resistance else None
        dist_sup = round((price-support)   /price*100,1) if support    else None

        ranges_5d  = [(highs[i]-lows[i])/lows[i]*100
                      for i in range(-5,0) if i<len(highs) and lows[i]>0]
        volatility = round(sum(ranges_5d)/len(ranges_5d),2) if ranges_5d else None

        rsi_1h = None
        if include_hourly:
            try:
                import ibkr_data as _id
                hc = _id.get_hourly_closes(yahoo_sym, 14)
                if len(hc) >= 15:
                    rsi_1h = _calc_rsi(hc, 14)
            except Exception: pass

        result.update({
            "price":round(float(price),2), "change":round(float(change),2),
            "avg_vol":int(avg_vol), "vol_spike":vol_spike,
            "ret7":round(ret7,1), "ret30":round(ret30,1),
            "rsi_d":rsi_d, "rsi_1h":rsi_1h,
            "ema9":e9, "ema21":e21, "macd":macd,
            "sma20":sma20, "sma50":sma50, "sma200":sma200, "cmf20":cmf20,
            "obv_trend":obv_trend, "mfi14":mfi14,
            "gap_pct":gap_pct, "bb_compressed":bb_compressed, "bb_width":bb_width,
            "support":support, "resistance":resistance,
            "dist_support":dist_sup, "dist_resistance":dist_res,
            "volatility":volatility,
            "hi52":round(max(highs),2) if highs else None,
            "lo52":round(min(lows), 2) if lows  else None,
            "close_series":closes[-252:],
            "ok":True,
        })
    except Exception as e:
        result["error"] = str(e)

    cache_set(cache_key, dict(result))
    return result

def fetch_prices(symbols):
    prices, failed = {}, []
    for sym in symbols:
        s = fetch_stock(sym, include_hourly=False)
        if s.get("ok") and s.get("price") is not None:
            prices[sym] = s["price"]
        else:
            failed.append(sym)
    if failed:
        print(f"  ⚠ Quote unavailable: {', '.join(failed)}")
    return prices

def fetch_earnings_date(symbol):
    try:
        url = (f"https://query1.finance.yahoo.com/v11/finance/quoteSummary/{symbol}"
               f"?modules=calendarEvents")
        r = safe_get(url, timeout=3, tries=1)
        dates = (r.json()["quoteSummary"]["result"][0]["calendarEvents"]
                 .get("earnings",{}).get("earningsDate",[]))
        if dates:
            dt = datetime.fromtimestamp(dates[0].get("raw",0))
            return dt.strftime("%b %d"), (dt-datetime.now()).days
    except Exception: pass
    return None, None

def price_to_support_pct(s):
    if not s.get("support") or not s.get("price"): return None
    return (s["price"]-s["support"])/s["price"]*100

def price_to_resistance_pct(s):
    if not s.get("resistance") or not s.get("price"): return None
    return (s["resistance"]-s["price"])/s["price"]*100

def compute_trade_levels(s):
    price = s["price"]; vol = s.get("volatility") or 3.0
    stop_pct  = max(4.5, min(9.0, vol*1.2))
    stop      = round(price*(1-stop_pct/100),2)
    if s.get("support") and s["support"]<price:
        # Widen to just below support, but never beyond 12% absolute risk.
        # NEM 2026-05-17: unbounded support-widening produced a ~15% stop —
        # R:R gate passed (target scaled too) but absolute risk was mis-sized.
        stop = round(max(min(stop, s["support"]*0.985), price*0.88),2)
    # Reward derives from the EFFECTIVE stop width (post-override), not the
    # base stop_pct — otherwise widening the stop silently degrades R:R.
    stop_pct_eff = (price-stop)/price*100
    reward_pct= max(stop_pct_eff*1.8, 8.0)
    target    = round(price*(1+reward_pct/100),2)
    if s.get("resistance") and s["resistance"]>price:
        target = round(min(target, s["resistance"]*0.985),2)
    if target<=price: target = round(price*1.08,2)
    if stop  >=price: stop   = round(price*0.94,2)
    return stop, target, round(stop_pct_eff,1)

def momentum_12_1(closes):
    """
    12-1 month momentum: cumulative return from t-12 months to t-1 month,
    SKIPPING the most recent month (it mean-reverts). Jegadeesh & Titman 1993;
    replicated 30+ years out-of-sample. Returns % or None.
    """
    if not closes or len(closes) < 230:
        return None
    base = closes[-252] if len(closes) >= 252 else closes[0]
    ref  = closes[-21]
    if not base or base <= 0 or not ref:
        return None
    return (ref - base) / base * 100


def compute_beta(sc, bc):
    if not sc or not bc: return None
    n = min(len(sc), len(bc))
    if n<30: return None
    sr = [(sc[i]/sc[i-1])-1 for i in range(1,n) if sc[i-1]]
    br = [(bc[i]/bc[i-1])-1 for i in range(1,n) if bc[i-1]]
    m  = min(len(sr),len(br))
    if m<20: return None
    sr,br = sr[-m:],br[-m:]
    ms,mb = sum(sr)/m, sum(br)/m
    cov   = sum((a-ms)*(b-mb) for a,b in zip(sr,br))/m
    vb    = sum((b-mb)**2 for b in br)/m
    return round(cov/vb,2) if vb>1e-12 else None

def _get_benchmark(symbol):
    with _cache_lock:
        if symbol in _BENCHMARK_CACHE:
            return _BENCHMARK_CACHE[symbol]
    data = fetch_stock(symbol, include_hourly=False, long_history=True)
    result = data.get("close_series", []) if data.get("ok") else []
    with _cache_lock:
        _BENCHMARK_CACHE[symbol] = result
    return result

def fetch_weekly_trend(symbol):
    """True = weekly EMA9>EMA21 (uptrend), False = downtrend, None = insufficient data."""
    cache_key = f"weekly::{symbol}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        import ibkr_data as _id
        ib = _id._get_ib()
        contract, what = _id._contract_info(symbol.upper())
        bars = ib.reqHistoricalData(
            contract, endDateTime="", durationStr="1 Y",
            barSizeSetting="1 week", whatToShow=what,
            useRTH=True, formatDate=1,
        )
        closes = [round(float(b.close), 4) for b in bars]
        if len(closes) < 21: return None
        trend = (_calc_ema(closes,9) or 0) > (_calc_ema(closes,21) or 0)
        cache_set(cache_key, trend)
        return trend
    except Exception:
        return None

def market_regime():
    """
    Returns (bullish: bool, label: str).

    Primary gate: 200-day SMA (Faber 2007, SSRN 962461 — Sharpe 0.32→0.55 on
    a century of data; genuinely out-of-sample since 2006). The old 50DMA gate
    whipsaws through ordinary pullbacks; 200DMA is the published rule.
    Falls back to 50DMA when fewer than 200 bars are available.
    """
    def above_sma(closes, period):
        return len(closes) >= period and closes[-1] > sum(closes[-period:]) / period

    def is_bull(closes):
        if len(closes) >= 200:
            return above_sma(closes, 200)
        return above_sma(closes, 50)

    spy_bull = is_bull(_get_benchmark("SPY"))
    xiu_bull = is_bull(_get_benchmark("XIU.TO"))

    if spy_bull and xiu_bull:
        return True,  "🟢 bull (SPY+XIU above 200DMA)"
    if spy_bull or xiu_bull:
        return True,  "🟡 mixed (one market above 200DMA)"
    return False, "🔴 bear (SPY+XIU both below 200DMA)"

def cad_cost_for_trade(symbol, shares, price, usd_cad):
    return round(shares*price*(1 if symbol.endswith((".TO",".V",".CN")) else usd_cad), 2)

def _options_flag(rec, s):
    """Assess whether a stock signal warrants an options play."""
    import math as _m
    sym    = rec["symbol"].upper()
    signal = rec["signal"]
    score  = rec["score"]
    price  = s.get("price") or 0

    if sym.endswith((".TO", ".V", ".CN")):
        return {"eligible": False, "reason": "Canadian listing — no options market"}
    if signal != "BUY":
        return {"eligible": False, "reason": f"signal is {signal}"}
    if score < 7.0:
        return {"eligible": False, "reason": f"score {score:.1f} below 7.0"}

    direction = "CALL"
    gates = []
    rsi_1h    = s.get("rsi_1h")
    macd      = s.get("macd")
    vol_spike = s.get("vol_spike", 1.0)

    if rsi_1h is not None and rsi_1h < 35:
        gates.append(f"1h RSI {rsi_1h:.1f} < 35")
    if macd is not None and macd < 0:
        gates.append("MACD negative")
    if vol_spike < 1.0:
        gates.append(f"vol spike {vol_spike:.1f}x weak")

    strike = _m.ceil(price * 2) / 2
    strike_hint = f"${strike:.2f} slight OTM"

    if gates:
        return {"eligible": False, "direction": direction,
                "strike_hint": strike_hint, "dte_min": 30,
                "reason": "wait — " + " | ".join(gates)}
    return {"eligible": True, "direction": direction,
            "strike_hint": strike_hint, "dte_min": 30,
            "reason": "all gates clear"}

def print_trade_card(rec, usd_cad):
    s = rec["stock"]
    market     = ("TSX" if s["symbol"].endswith(".TO") else
                  "TSXV" if s["symbol"].endswith(".V") else
                  "CSE"  if s["symbol"].endswith(".CN") else "US")
    confidence = ("high" if rec["score"]>=8.5 else
                  "medium" if rec["score"]>=7 else "low")
    opts = rec.get("options", {})
    opts_line = ""
    if opts.get("direction"):
        if opts.get("eligible"):
            contract = opts.get("contract", {})
            if contract:
                opts_line = (f"Options   : {opts['direction']}  strike {contract.get('strike')}  "
                             f"mid ${contract.get('mid')}  DTE {contract.get('dte')}  "
                             f"IV {contract.get('iv')}%  spread {contract.get('spread_pct')}%")
            else:
                opts_line = f"Options   : {opts['direction']}  {opts['strike_hint']}  min {opts['dte_min']} DTE  — {opts['reason']}"
        else:
            opts_line = f"Options   : {opts['direction']} — {opts['reason']}"

    block = "\n".join(filter(None, [
        f"{s['symbol']}  ${s['price']:.2f}  [{market}]",
        f"Signal    : {rec['signal']} [{confidence}]  Score {rec['score']:.2f}",
        f"Shares    : {rec['shares']}  (~${rec['cost_cad']:.2f} CAD)",
        f"Target    : ${rec['target']:.2f}  |  Stop: ${rec['stop']:.2f}  |  R/R: {rec['risk_reward']:.2f}R",
        f"Entry tip : {rec['entry_tip']}",
        f"Reason    : {'; '.join(rec['reasons']) or 'mixed setup'}",
        (f"Risk      : {'; '.join(rec['warnings'])}" if rec["warnings"] else ""),
        opts_line,
    ]))
    if RICH:
        style={"BUY":"green","WATCH":"yellow"}.get(rec["signal"],"dim")
        console.print(Panel(block.strip(), border_style=style))
    else:
        print("\n"+block.strip()+"\n"+"─"*60)

def print_holdings_review(portfolio, live_prices):
    if not portfolio["holdings"]: return
    cprint("\n[bold]🧾 Holdings review[/]" if RICH else "\n🧾 Holdings review")
    for sym, pos in portfolio["holdings"].items():
        price = live_prices.get(sym)
        sl  = portfolio.get("stop_losses",{}).get(sym)
        tgt = portfolio.get("targets",{}).get(sym)
        if price is None:
            cprint(f"  {'[cyan]'+sym+'[/]' if RICH else sym}: live quote unavailable, avg ${pos['avg_cost']:.2f}")
            continue
        pct  = (price-pos["avg_cost"])/pos["avg_cost"]*100
        note = "HOLD"
        if sl  and price<=sl:  note="SELL — stop hit"
        elif tgt and price>=tgt: note="TRIM/SELL — target hit"
        elif pct>=10:            note="Review for profit-taking"
        elif pct<=-6:            note="Review risk immediately"
        cprint(f"  {'[cyan]'+sym+'[/]' if RICH else sym}: "
               f"${price:.2f} vs avg ${pos['avg_cost']:.2f} ({pct:+.1f}%) → "
               f"{'[bold]'+note+'[/]' if RICH else note}")

def print_claude_response(title, text):
    text=(text or "").strip() or "No response."
    if RICH: console.print(Panel(text, title=title, border_style="magenta"))
    else:    print(f"\n[{title}]\n{text}\n")

def build_context(portfolio, scanned_stocks, headlines, recs=None):
    lp = {s["symbol"]:s["price"] for s in scanned_stocks}
    h_lines = [f"- {sym}: {pos['shares']}sh avg ${pos['avg_cost']:.2f}"
               + (f" now ~${lp[sym]:.2f} P&L ${(lp[sym]-pos['avg_cost'])*pos['shares']:+.2f}"
                  if sym in lp else " (no live quote)")
               + (f" stop ${portfolio['stop_losses'][sym]}" if sym in portfolio.get("stop_losses",{}) else "")
               + (f" target ${portfolio['targets'][sym]}"   if sym in portfolio.get("targets",{})     else "")
               for sym,pos in portfolio.get("holdings",{}).items()]
    r_lines = [f"- {r['symbol']}: {r['signal']} score {r['score']:.2f} "
               f"stop ${r['stop']:.2f} target ${r['target']:.2f}"
               for r in (recs or [])[:8]]
    return (f"Cash: ${portfolio.get('cash',0):.2f} CAD\n"
            f"Holdings:\n" + "\n".join(h_lines or ["- none"]) + "\n\n"
            f"Headlines:\n" + "\n".join(f"- {h}" for h in headlines[:8]) + "\n\n"
            f"Signals:\n"   + "\n".join(r_lines or ["- none"]))

def ask_claude(prompt):
    try:
        r = subprocess.run(["claude","--print",prompt], text=True, timeout=180)
        return r.returncode==0
    except FileNotFoundError:
        cprint("[red]❌ 'claude' not found.[/]" if RICH else "❌ 'claude' not found.")
        return False
    except subprocess.TimeoutExpired:
        cprint("[yellow]⏱ Claude timed out.[/]" if RICH else "⏱ Timed out.")
        return False

def ask_claude_capture(prompt):
    try:
        r = subprocess.run(["claude","--print",prompt],
                           capture_output=True, text=True, timeout=180)
        return (r.stdout or "").strip() if r.returncode==0 else None
    except Exception: return None

def portfolio_summary(portfolio, live_prices=None):
    started = portfolio.get("started", 109)
    if RICH:
        table=Table(title=f"Portfolio — {datetime.now().strftime('%b %d %H:%M')}",
                    box=box.SIMPLE, show_lines=False)
        for col in ["Symbol","Shares","Avg","Now","P&L $","P&L %","Stop","Target"]:
            table.add_column(col, justify="right" if col not in ("Symbol",) else "left",
                             style="cyan bold" if col=="Symbol" else "")
        total=portfolio["cash"]
        for sym,pos in portfolio["holdings"].items():
            cur=(live_prices or {}).get(sym)
            sl =portfolio.get("stop_losses",{}).get(sym,"—")
            tgt=portfolio.get("targets",{}).get(sym,"—")
            pnl=pct=None
            if cur is not None:
                pnl=(cur-pos["avg_cost"])*pos["shares"]
                pct=(cur-pos["avg_cost"])/pos["avg_cost"]*100
                total+=cur*pos["shares"]
            table.add_row(sym, str(pos["shares"]),
                          f"${pos['avg_cost']:.2f}",
                          f"${cur:.2f}" if cur else "N/A",
                          Text(f"${pnl:+.2f}","green" if pnl and pnl>=0 else "red") if pnl is not None else Text("N/A"),
                          Text(f"{pct:+.1f}%","green" if pct and pct>=0 else "red") if pct is not None else Text("N/A"),
                          f"${sl}" if isinstance(sl,float) else str(sl),
                          f"${tgt}" if isinstance(tgt,float) else str(tgt))
        console.print(table)
        pnl_t=total-started
        console.print(f"  Cash: [cyan]${portfolio['cash']:.2f}[/]  "
                      f"Total: [bold]${total:.2f}[/]  "
                      f"P&L: [{'green' if pnl_t>=0 else 'red'}]${pnl_t:+.2f} ({pnl_t/started*100:+.1f}%)[/]")
    else:
        total=portfolio["cash"]
        print(f"\n💼 Portfolio — {datetime.now().strftime('%b %d %H:%M')}")
        print(f"   Cash: ${portfolio['cash']:.2f}")
        for sym,pos in portfolio["holdings"].items():
            cur=(live_prices or {}).get(sym)
            if cur is None: print(f"   • {sym:<10} {pos['shares']}sh  avg ${pos['avg_cost']:.2f} → N/A")
            else:
                pnl=(cur-pos["avg_cost"])*pos["shares"]
                pct=(cur-pos["avg_cost"])/pos["avg_cost"]*100
                total+=cur*pos["shares"]
                print(f"   • {sym:<10} {pos['shares']}sh  avg ${pos['avg_cost']:.2f} → ${cur:.2f}  "
                      f"{pnl:+.2f} ({pct:+.1f}%)")
        pnl_t=total-started
        print(f"   Total: ${total:.2f}  P&L: ${pnl_t:+.2f} ({pnl_t/started*100:+.1f}%)")

# ── AlgoEngine ────────────────────────────────────────────────────────────────
class AlgoEngine:
    """
    All config-dependent logic in one class. Instantiate with per-universe config;
    thin wrapper files expose module-level shims for backward compatibility.
    """

    def __init__(self, min_price, max_price, sectors, us_seeds, ca_seeds,
                 strategy, news_feeds,
                 portfolio_file=None,
                 accuracy_file=None,
                 paper_trades_file=None,
                 telegram_token="", telegram_chat_id="",
                 scan_title=None, extra_stopwords=None,
                 banner_rich=None, banner_plain=None):
        self.MIN_PRICE        = min_price
        self.MAX_PRICE        = max_price
        self.SECTORS          = sectors
        self.US_SEEDS         = us_seeds
        self.CA_SEEDS         = ca_seeds
        self.STRATEGY         = strategy
        self.NEWS_FEEDS       = news_feeds
        _data = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        self.PORTFOLIO_FILE   = portfolio_file    or os.path.join(_data, "portfolio.json")
        self.ACCURACY_FILE    = accuracy_file     or os.path.join(_data, "accuracy.json")
        self.PAPER_TRADES_FILE= paper_trades_file or os.path.join(_data, "paper_trades.json")
        self.TELEGRAM_TOKEN   = telegram_token
        self.TELEGRAM_CHAT_ID = telegram_chat_id
        self._scan_title      = scan_title or f"Stocks ${min_price}–${max_price}"
        self._extra_stopwords = extra_stopwords or set()
        self._banner_rich     = banner_rich or (
            "[bold cyan]📈 TRADING SIGNAL ASSISTANT[/]\n"
            "[dim]Fixes: thin-stock quotes · .V/.CN tickers · rate-limit guard\n"
            "benchmark cache · input validation · lazy analysis[/]"
        )
        self._banner_plain    = banner_plain or [
            "╔══════════════════════════════════════════════════════╗",
            "║  📈 TRADING SIGNAL ASSISTANT                         ║",
            "╚══════════════════════════════════════════════════════╝",
        ]

    # ── Portfolio ─────────────────────────────────────────────────────────────
    def load_portfolio(self):
        if os.path.exists(self.PORTFOLIO_FILE):
            with open(self.PORTFOLIO_FILE) as f:
                p = json.load(f)
            for k in ("holdings","history","stop_losses","targets"):
                p.setdefault(k, {})
            p.setdefault("cash",    p.get("started", 109))
            p.setdefault("started", p.get("cash", 109))
            return p
        return {"holdings":{},"cash":109,"history":[],"started":109,
                "stop_losses":{},"targets":{}}

    def save_portfolio(self, p):
        with open(self.PORTFOLIO_FILE,"w") as f:
            json.dump(p, f, indent=2)

    def load_accuracy(self):
        if os.path.exists(self.ACCURACY_FILE):
            with open(self.ACCURACY_FILE) as f: return json.load(f)
        return {"signals":[]}

    def save_accuracy(self, a):
        with open(self.ACCURACY_FILE,"w") as f: json.dump(a, f, indent=2)

    def load_paper_trades(self):
        if os.path.exists(self.PAPER_TRADES_FILE):
            with open(self.PAPER_TRADES_FILE) as f: return json.load(f)
        return {"trades": []}

    def save_paper_trades(self, pt):
        with open(self.PAPER_TRADES_FILE, "w") as f: json.dump(pt, f, indent=2)

    def log_paper_trade(self, rec):
        """Auto-called by run_analysis for every BUY signal.
        With AUTO_EXECUTE_SWING=1 (default) also places a GTC bracket order on
        the IBKR paper account — IBKR's servers then handle the exit at target
        or stop with no script running. Set AUTO_EXECUTE_SWING=0 to disable."""
        pt = self.load_paper_trades()
        entry = {
            "symbol":     rec["symbol"],
            "date":       datetime.now().strftime("%Y-%m-%d"),
            "score":      rec["score"],
            "entry":      rec["stock"]["price"],
            "stop":       rec["stop"],
            "target":     rec["target"],
            "status":     "open",
            "exit_price": None,
            "exit_date":  None,
            "pnl_pct":    None,
        }
        today = entry["date"]
        already = any(t["symbol"]==rec["symbol"] and t["date"]==today
                      for t in pt["trades"])
        # Block re-entry while ANY open trade exists on the symbol (not just today's)
        open_dup = any(t["symbol"]==rec["symbol"] and t.get("status")=="open"
                       for t in pt["trades"])
        if not already and not open_dup:
            # ── Auto-execute on IBKR paper (1% risk sizing, same as intraday) ──
            if os.environ.get("AUTO_EXECUTE_SWING", "1") == "1":
                try:
                    import ibkr_data as _id
                    cash  = self.load_portfolio().get("cash", 0)
                    risk  = entry["entry"] - entry["stop"]
                    shares = max(1, int(cash * 0.01 / risk)) if risk > 0 else 1
                    br = _id.place_bracket_order(
                        rec["symbol"], shares,
                        target=entry["target"], stop=entry["stop"],
                        entry=entry["entry"])
                    entry["ibkr_bracket"] = br
                    entry["shares"]       = shares
                    cprint(f"  [cyan]📋 IBKR bracket: BUY {shares}x {rec['symbol']} — "
                           f"GTC target ${entry['target']} / stop ${entry['stop']}[/]"
                           if RICH else
                           f"  📋 IBKR bracket: BUY {shares}x {rec['symbol']} "
                           f"tgt ${entry['target']} stop ${entry['stop']}")
                except Exception as _e:
                    cprint(f"  [yellow]⚠️  IBKR bracket failed ({_e}) — paper-only[/]"
                           if RICH else f"  ⚠️  IBKR bracket failed: {_e}")
            pt["trades"].append(entry)
            self.save_paper_trades(pt)
            cprint(f"  [dim]📝 Paper trade logged: {rec['symbol']} entry ${entry['entry']:.2f}[/]"
                   if RICH else f"  📝 Paper logged: {rec['symbol']} ${entry['entry']:.2f}")

    def validate_paper_trades(self):
        """Fetch live prices for open paper trades and mark outcomes."""
        pt = self.load_paper_trades()
        open_trades = [t for t in pt["trades"] if t["status"] == "open"]
        if not open_trades:
            cprint("[yellow]No open paper trades to validate.[/]" if RICH
                   else "No open paper trades to validate.")
            return

        syms = list({t["symbol"] for t in open_trades})
        prices = {s: fetch_stock(s, include_hourly=False).get("price") for s in syms}
        today  = datetime.now().strftime("%Y-%m-%d")

        updated = 0
        for t in pt["trades"]:
            if t["status"] != "open": continue
            price = prices.get(t["symbol"])
            if price is None: continue
            if price >= t["target"]:
                t.update(status="target_hit", exit_price=round(price,2),
                         exit_date=today,
                         pnl_pct=round((price-t["entry"])/t["entry"]*100,2))
            elif price <= t["stop"]:
                t.update(status="stop_hit", exit_price=round(price,2),
                         exit_date=today,
                         pnl_pct=round((price-t["entry"])/t["entry"]*100,2))
            else:
                days_open = (datetime.now() - datetime.strptime(t["date"],"%Y-%m-%d")).days
                if days_open >= 21:
                    t.update(status="timeout", exit_price=round(price,2),
                             exit_date=today,
                             pnl_pct=round((price-t["entry"])/t["entry"]*100,2))
            updated += 1

        self.save_paper_trades(pt)

        closed     = [t for t in pt["trades"] if t["status"] != "open"]
        still_open = [t for t in pt["trades"] if t["status"] == "open"]
        wins   = [t for t in closed if (t["pnl_pct"] or 0) > 0]
        losses = [t for t in closed if (t["pnl_pct"] or 0) <= 0]
        win_rate = len(wins)/len(closed)*100 if closed else 0
        avg_pnl  = sum(t["pnl_pct"] for t in closed)/len(closed) if closed else 0

        if RICH:
            tbl = Table(title="📋 Paper Trade Validation", box=box.SIMPLE_HEAVY)
            for col in ["Symbol","Date","Entry","Target","Stop","Status","P&L %"]:
                tbl.add_column(col, justify="right" if col not in ("Symbol","Status","Date") else "left")
            for t in sorted(pt["trades"], key=lambda x: x["date"], reverse=True)[:20]:
                status_color = {"target_hit":"green","stop_hit":"red",
                                "timeout":"yellow","open":"cyan"}.get(t["status"],"white")
                pnl_str = f"{t['pnl_pct']:+.1f}%" if t["pnl_pct"] is not None else "—"
                tbl.add_row(
                    t["symbol"], t["date"],
                    f"${t['entry']:.2f}", f"${t['target']:.2f}", f"${t['stop']:.2f}",
                    Text(t["status"].replace("_"," "), status_color),
                    Text(pnl_str, "green" if (t["pnl_pct"] or 0)>0 else "red"),
                )
            console.print(tbl)
            console.print(
                f"\n  [bold]Closed:[/] {len(closed)} trades  "
                f"[green]Wins: {len(wins)}[/]  [red]Losses: {len(losses)}[/]  "
                f"Win rate: [{'green' if win_rate>=50 else 'red'}]{win_rate:.1f}%[/]  "
                f"Avg P&L: [{'green' if avg_pnl>=0 else 'red'}]{avg_pnl:+.2f}%[/]\n"
                f"  [cyan]Still open: {len(still_open)}[/]"
            )
            if len(closed) >= 10:
                console.print(
                    f"\n  [bold]vs backtest baseline[/] — "
                    f"win rate {'✅' if win_rate>=45 else '⚠️ ' if win_rate>=35 else '❌'} "
                    f"(backtest: ~47–52%)  "
                    f"{'✅ On track' if win_rate>=45 else '⚠️  Monitor closely' if win_rate>=35 else '❌ Strategy may be overfit'}"
                )
        else:
            print(f"\n📋 Paper Trade Validation")
            for t in sorted(pt["trades"], key=lambda x: x["date"], reverse=True)[:20]:
                pnl = f"{t['pnl_pct']:+.1f}%" if t["pnl_pct"] is not None else "open"
                print(f"  {t['symbol']:<8} {t['date']}  entry ${t['entry']:.2f}  "
                      f"{t['status']:<12} {pnl}")
            print(f"\n  Closed: {len(closed)}  Wins: {len(wins)}  Win rate: {win_rate:.1f}%  Avg P&L: {avg_pnl:+.2f}%")
            if len(closed) >= 10:
                verdict = "✅ On track" if win_rate>=45 else ("⚠️  Monitor" if win_rate>=35 else "❌ Overfit")
                print(f"  vs backtest (47–52%): {verdict}")

    # ── Telegram ──────────────────────────────────────────────────────────────
    def send_telegram(self, msg):
        if not self.TELEGRAM_TOKEN or not self.TELEGRAM_CHAT_ID: return
        try:
            requests.post(f"https://api.telegram.org/bot{self.TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id":self.TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"Markdown"},
                          timeout=5)
        except Exception: pass

    def setup_telegram(self):
        token = input("Paste your Telegram bot token: ").strip()
        print("Send any message to your bot on Telegram, then press Enter...")
        input()
        try:
            r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=8)
            updates = r.json().get("result",[])
            if updates:
                chat_id = updates[-1]["message"]["chat"]["id"]
                print(f"\nYour chat ID: {chat_id}")
                print(f'TELEGRAM_TOKEN   = "{token}"')
                print(f'TELEGRAM_CHAT_ID = "{chat_id}"')
            else:
                print("No messages found. Send a message to your bot first.")
        except Exception as e:
            print(f"Error: {e}")

    # ── News + sentiment ──────────────────────────────────────────────────────
    def fetch_news(self):
        headlines, ticker_news, news_tickers = [], defaultdict(list), set()
        for feed in self.NEWS_FEEDS:
            try:
                r = requests.get(feed, headers=REQUEST_HEADERS, timeout=6)
                if r.status_code != 200: continue
                for item in ET.fromstring(r.content).findall(".//item")[:10]:
                    title = item.findtext("title") or ""
                    desc  = item.findtext("description") or ""
                    text  = f"{title} {desc}"
                    if title:
                        headlines.append(title.strip())
                        sent, score_val = score_sentiment(text)
                        for t in re.findall(r"\b([A-Z]{2,5})\b", text):
                            news_tickers.add(t)
                            ticker_news[t].append((title.strip(), sent, score_val))
            except Exception: continue
        try:
            import ibkr_data as _id
            for sym in _id.get_top_gainers(25):
                news_tickers.add(sym)
                ticker_news[sym].append(("In today's top gainers", "bullish", 2))
        except Exception: pass
        ticker_sentiment = {}
        for ticker, nl in ticker_news.items():
            bull = sum(s for _,sent,s in nl if sent=="bullish")
            bear = sum(s for _,sent,s in nl if sent=="bearish")
            net  = bull - bear
            ticker_sentiment[ticker] = {
                "score": net,
                "label": "🟢 bullish" if net>0 else ("🔴 bearish" if net<0 else "⚪ neutral"),
                "headlines": [h for h,_,_ in nl[:3]],
            }
        return headlines[:25], ticker_sentiment, news_tickers

    # ── Strategy helpers ──────────────────────────────────────────────────────
    def detect_sector_rotation(self, stocks):
        sym_sec = {t:s for s,tickers in self.SECTORS.items() for t in tickers}
        sc = defaultdict(list)
        for s in stocks:
            sec = sym_sec.get(s["symbol"])
            if sec: sc[sec].append(s["change"])
        return sorted(((s, round(sum(v)/len(v),2)) for s,v in sc.items()),
                      key=lambda x: x[1], reverse=True)

    def sector_for_symbol(self, symbol):
        base = symbol.replace(".TO","").replace(".V","").replace(".CN","")
        for sector, names in self.SECTORS.items():
            if base in {n.replace(".TO","").replace(".V","").replace(".CN","") for n in names}:
                return sector
        return None

    def sector_momentum(self, sector, all_stocks):
        """Average 7-day return of peers in the same sector."""
        peers = [s for s in all_stocks if self.sector_for_symbol(s["symbol"]) == sector]
        if not peers:
            return 0.0, 0
        rets = [s.get("ret7", 0) for s in peers]
        return round(sum(rets) / len(rets), 2), len(peers)

    def calc_position_size(self, budget, price, volatility, max_pct=0.35):
        vol = volatility or 3.0
        pct = max(0.08, min(max_pct, max_pct-(vol-1)*0.04))
        shares = max(1, int(budget*pct/price))
        return shares, round(shares*price,2), round(pct*100,1)

    def build_sector_rank_map(self, stocks):
        return {sec:i+1 for i,(sec,_) in enumerate(self.detect_sector_rotation(stocks))}

    def fetch_usdcad_rate(self):
        try:
            fx = fetch_stock("USDCAD=X", include_hourly=False)
            if fx.get("ok") and fx.get("price"):
                return float(fx["price"])
        except Exception: pass
        return self.STRATEGY["usd_cad_fallback"]

    # ── Relative metrics ──────────────────────────────────────────────────────
    def attach_relative_metrics(self, stocks):
        if not stocks: return stocks
        us_closes = _get_benchmark("SPY")
        ca_closes = _get_benchmark("XIU.TO")
        sec_rsi   = defaultdict(list)
        for s in stocks:
            sec = self.sector_for_symbol(s["symbol"])
            if sec and s.get("rsi_d") is not None:
                sec_rsi[sec].append(s["rsi_d"])
        enriched = []
        for s in stocks:
            sec = self.sector_for_symbol(s["symbol"])
            avg = round(sum(sec_rsi[sec])/len(sec_rsi[sec]),1) if sec_rsi.get(sec) else None
            delta = round(s["rsi_d"]-avg,1) if s.get("rsi_d") and avg else None
            is_ca = any(s["symbol"].endswith(sfx) for sfx in (".TO",".V",".CN"))
            bench = ca_closes if is_ca else us_closes
            beta  = compute_beta(s.get("close_series",[]), bench)
            rs    = _calc_rs_ratio(s.get("close_series",[]), bench)
            enriched.append({**s, "sector_rsi_avg":avg, "sector_rsi_delta":delta,
                             "beta_index":beta, "rs_ratio":rs})
        return enriched

    # ── Scan ──────────────────────────────────────────────────────────────────
    def scan_all(self, extra_tickers=None):
        print("\n📡 Fetching real-time market news...")
        headlines, ticker_sentiment, news_tickers = self.fetch_news()

        cprint(f"\n[bold cyan]📰 Headlines ({datetime.now().strftime('%H:%M')})[/]"
               if RICH else f"\n📰 Headlines ({datetime.now().strftime('%H:%M')}):")
        for h in headlines[:6]:
            cprint(f"  [dim]• {h[:95]}[/]" if RICH else f"  • {h[:95]}")

        stopwords = {
            "CEO","CFO","IPO","ETF","USA","GDP","FED","SEC","NYSE","TSX","CAD","USD",
            "ESG","AI","EV","EPS","THE","FOR","AND","BUT","NOT","INC","LTD","LLC",
            "US","CA","UK","EU","BTC","ETH","NFT","TFSA","RRSP","SPAC","REIT",
        } | self._extra_stopwords
        extra_news = sorted(
            (t for t in news_tickers if t not in stopwords and 2<=len(t)<=5)
        )[:20]
        all_tickers = list(set(self.US_SEEDS + self.CA_SEEDS + extra_news + (extra_tickers or [])))

        cprint(f"\n[bold]🔍 Scanning {len(all_tickers)} stocks...[/]"
               if RICH else f"\n🔍 Scanning {len(all_tickers)} stocks...")

        min_p = self.MIN_PRICE
        max_p = self.MAX_PRICE

        def first_pass(sym):
            s = fetch_stock(sym, include_hourly=False)
            if not s.get("ok") or not (min_p <= s["price"] <= max_p):
                return None
            base = sym.replace(".TO","").replace(".V","").replace(".CN","")
            s["sentiment"] = (ticker_sentiment.get(sym)
                              or ticker_sentiment.get(base)
                              or {"score":0,"label":"⚪ neutral","headlines":[]})
            return s

        affordable = []
        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = {ex.submit(first_pass, sym): sym for sym in sorted(all_tickers)}
            for fut in as_completed(futures):
                try:
                    s = fut.result()
                    if s: affordable.append(s)
                except Exception: pass

        affordable.sort(key=lambda x:(x.get("vol_spike",0),
                                       abs(x.get("change",0)),
                                       abs(x.get("sentiment",{}).get("score",0))),
                        reverse=True)

        def fetch_hourly(s):
            h = fetch_stock(s["symbol"], include_hourly=True)
            if h.get("ok"):
                return {**s, "rsi_1h": h.get("rsi_1h", s.get("rsi_1h"))}
            return s

        with ThreadPoolExecutor(max_workers=3) as ex:
            enriched = list(ex.map(fetch_hourly, affordable))
        affordable = self.attach_relative_metrics(enriched)

        if RICH and affordable:
            table = Table(title=self._scan_title,
                          box=box.SIMPLE_HEAVY, show_lines=False)
            for col, kw in [
                ("Ticker","bold cyan"),("Price",""),("Today",""),("7d",""),("30d",""),
                ("RSI D",""),("RSI 1h",""),("Vol",""),("Support",""),("Resist",""),("Sentiment",""),
            ]:
                table.add_column(col, style=kw, justify="right" if col not in ("Ticker","Sentiment") else "left")
            for s in sorted(affordable, key=lambda x:x["vol_spike"], reverse=True):
                flag = "🇨🇦" if any(s["symbol"].endswith(sfx) for sfx in (".TO",".V",".CN")) else "🇺🇸"
                table.add_row(
                    f"{flag} {s['symbol']}",
                    f"${s['price']}",
                    Text(f"{s['change']:+.1f}%","green" if s["change"]>=0 else "red"),
                    Text(f"{s['ret7']:+.1f}%",  "green" if s["ret7"]  >=0 else "red"),
                    Text(f"{s['ret30']:+.1f}%", "green" if s["ret30"] >=0 else "red"),
                    Text(str(s["rsi_d"] or "n/a"),
                         "green" if s["rsi_d"] and s["rsi_d"]<40 else
                         ("red"   if s["rsi_d"] and s["rsi_d"]>65 else "white")),
                    str(s.get("rsi_1h") or "n/a"),
                    Text(f"{s['vol_spike']}x","yellow bold" if s["vol_spike"]>=2 else "white"),
                    f"${s['support']}"    if s["support"]    else "n/a",
                    f"${s['resistance']}" if s["resistance"] else "n/a",
                    s["sentiment"]["label"],
                )
            console.print(table)
        else:
            for s in sorted(affordable, key=lambda x:x["vol_spike"], reverse=True):
                flag = "🇨🇦" if any(s["symbol"].endswith(sfx) for sfx in (".TO",".V",".CN")) else "🇺🇸"
                print(f"  {flag} {s['symbol']:<12} ${s['price']:<7} {s['change']:+.1f}%  "
                      f"vol:{s['vol_spike']}x  {s['sentiment']['label']}")

        rotation = self.detect_sector_rotation(affordable)
        if rotation:
            cprint("\n[bold]📊 Sector Rotation:[/]" if RICH else "\n📊 Sector Rotation:")
            for sec,avg in rotation[:5]:
                bar = "▲" if avg>0 else "▼"
                cprint(f"  [{'green' if avg>0 else 'red'}]{bar} {sec:<12} {avg:+.2f}%[/]"
                       if RICH else f"  {bar} {sec:<12} {avg:+.2f}%")

        cprint(f"\n[green]✅ {len(affordable)} stocks found[/]"
               if RICH else f"\n✅ {len(affordable)} stocks found")
        return affordable, headlines, ticker_sentiment

    # ── Scoring ───────────────────────────────────────────────────────────────
    def evaluate_stock(self, s, portfolio_cash, usd_cad, sector_ranks,
                       include_earnings=True, regime_bullish=True, all_stocks=None):
        score, reasons, warnings = 0.0, [], []
        support_gap    = price_to_support_pct(s)
        resistance_gap = price_to_resistance_pct(s)
        sector = self.sector_for_symbol(s["symbol"])
        stop, target, stop_pct = compute_trade_levels(s)

        if not regime_bullish:
            score -= 3.0
            warnings.append("bear market: SPY+XIU below 50DMA")

        rs = s.get("rs_ratio")
        if rs is not None:
            if rs >= 1.3:   score += 1.2; reasons.append(f"RS leader vs market ({rs:.2f}x)")
            elif rs >= 1.1: score += 0.5; reasons.append(f"RS above market ({rs:.2f}x)")
            elif rs < 0.8:  score -= 0.8; warnings.append(f"RS lagging market ({rs:.2f}x)")

        # ── 12-1 cross-sectional momentum (Jegadeesh & Titman 1993) ──────────
        # The edge is RELATIVE: rank this stock's 12-1 return against the
        # scanned universe. Top quintile wins persist; bottom quintile lags.
        mom = momentum_12_1(s.get("close_series", []))
        if mom is not None:
            peers = []
            if all_stocks:
                for _o in all_stocks:
                    _m = momentum_12_1(_o.get("close_series", []))
                    if _m is not None:
                        peers.append(_m)
            if len(peers) >= 10:
                mrank = sum(1 for _m in peers if _m < mom) / len(peers)
                if mrank >= 0.8:
                    score += 1.2; reasons.append(f"12-1 momentum top quintile ({mom:+.0f}%)")
                elif mrank <= 0.2:
                    score -= 1.0; warnings.append(f"12-1 momentum bottom quintile ({mom:+.0f}%)")
            else:   # universe too thin to rank — absolute fallback
                if mom > 20:    score += 0.8; reasons.append(f"strong 12-1 momentum ({mom:+.0f}%)")
                elif mom < -10: score -= 0.8; warnings.append(f"negative 12-1 momentum ({mom:+.0f}%)")

        # 1-month reversal: names up sharply in the last 21 days mean-revert —
        # the reason 12-1 momentum skips the final month. Don't chase.
        _cs = s.get("close_series", [])
        if len(_cs) >= 22 and _cs[-21]:
            _ret21 = (_cs[-1] - _cs[-21]) / _cs[-21] * 100
            if _ret21 > 15:
                score -= 0.6; warnings.append(f"extended +{_ret21:.0f}% in 21d — reversal risk")

        weekly = fetch_weekly_trend(s["symbol"])
        if weekly is True:
            score += 1.2; reasons.append("weekly trend up (EMA9>EMA21)")
        elif weekly is False:
            score -= 2.0; warnings.append("weekly trend down — fighting higher TF")

        if s.get("rsi_d") is not None:
            if 30<=s["rsi_d"]<=45:    score+=2.0; reasons.append(f"daily RSI healthy ({s['rsi_d']})")
            elif 45<s["rsi_d"]<=65:   score+=0.8; reasons.append(f"daily RSI uptrend zone ({s['rsi_d']})")
            elif s["rsi_d"]<30:       score+=1.2; warnings.append(f"RSI very oversold ({s['rsi_d']})")
            elif s["rsi_d"]>72:       score-=1.0; warnings.append(f"RSI overheated ({s['rsi_d']})")

        rel = s.get("sector_rsi_delta")
        if rel is not None:
            if rel>=4:   score+=1.0; reasons.append(f"RSI stronger than sector +{rel}")
            elif rel<=-4:score-=0.5; warnings.append(f"RSI weaker than sector {rel}")

        if s.get("rsi_1h") is not None:
            if 35<=s["rsi_1h"]<=60:  score+=1.3; reasons.append(f"1h RSI supports timing ({s['rsi_1h']})")
            elif 60<s["rsi_1h"]<=78: score+=0.5; reasons.append(f"1h RSI momentum zone ({s['rsi_1h']})")
            elif s["rsi_1h"]<28:     score+=0.3; warnings.append(f"1h RSI still weak ({s['rsi_1h']})")
            elif s["rsi_1h"]>78:     score-=0.6; warnings.append(f"1h RSI stretched ({s['rsi_1h']})")
        else:
            warnings.append("missing 1h RSI")

        _div = _detect_rsi_divergence(s.get("close_series", []))
        if _div == "bullish":
            score += 1.5; reasons.append("bullish RSI divergence (higher RSI low)")
        elif _div == "bearish":
            score -= 1.0; warnings.append("bearish RSI divergence (lower RSI high)")

        if s.get("ema9") and s.get("ema21"):
            if s["ema9"]>s["ema21"]: score+=1.5; reasons.append("short-term trend up")
            else:                    score-=1.0; warnings.append("EMA trend down")

        dma=0.0
        if s.get("sma20")  and s["price"]>s["sma20"]:  dma+=0.5
        if s.get("sma50")  and s["price"]>s["sma50"]:  dma+=0.7
        if s.get("sma200") and s["price"]>s["sma200"]: dma+=0.9
        score+=dma
        if dma>=1.6: reasons.append("price above key DMAs")
        if s.get("sma20") and s.get("sma50") and s.get("sma200"):
            if s["sma20"]>s["sma50"]>s["sma200"]:  score+=0.9; reasons.append("bullish DMA stack")
            elif s["sma20"]<s["sma50"]<s["sma200"]:score-=0.5; warnings.append("bearish DMA stack")

        # 52-week high proximity — breakout setups consolidating near highs have
        # documented edge; stocks >30% off highs tend to be in structural downtrends.
        hi52 = s.get("hi52"); price = s.get("price")
        if hi52 and price and hi52 > 0:
            pct_from_hi = (hi52 - price) / hi52 * 100
            if pct_from_hi <= 3:
                score += 1.2; reasons.append(f"at 52-wk high ({pct_from_hi:.1f}% away)")
            elif pct_from_hi <= 15:
                score += 0.8; reasons.append(f"near 52-wk high ({pct_from_hi:.1f}% below)")
            elif pct_from_hi <= 25:
                score += 0.3
            elif pct_from_hi > 30:
                score -= 0.8; warnings.append(f"far from 52-wk high ({pct_from_hi:.0f}% below)")

        cmf=s.get("cmf20")
        if cmf is not None:
            if cmf>=0.10:  score+=1.0; reasons.append(f"CMF positive ({cmf:+.2f})")
            elif cmf<=-0.10:score-=0.5; warnings.append(f"CMF weak ({cmf:+.2f})")

        obv = s.get("obv_trend", 0)
        ret = s.get("ret7", 0)
        if obv == 1:
            if ret <= 0:
                score += 0.9; reasons.append("OBV rising vs flat price (hidden accumulation)")
            else:
                score += 0.4; reasons.append("OBV confirming uptrend")
        elif obv == -1:
            if ret >= 0:
                score -= 0.8; warnings.append("OBV declining vs rising price (distribution)")
            else:
                score -= 0.3

        mfi = s.get("mfi14")
        if mfi is not None:
            if 25 <= mfi <= 45:  score += 0.8; reasons.append(f"MFI oversold recovery ({mfi:.0f})")
            elif mfi > 75:       score -= 0.6; warnings.append(f"MFI overbought ({mfi:.0f})")

        sm_count = sum([
            s.get("vol_spike", 0) >= 1.5,
            (s.get("cmf20") or 0) >= 0.10,
            s.get("obv_trend", 0) == 1,
            25 <= (s.get("mfi14") or 0) <= 55,
        ])
        if sm_count >= 4:
            score += 2.0; reasons.append("full smart money alignment (vol+CMF+OBV+MFI)")
        elif sm_count >= 3:
            score += 1.0; reasons.append(f"smart money confluence ({sm_count}/4)")

        if s.get("bb_compressed"):
            if s.get("obv_trend", 0) == 1:
                score += 1.5; reasons.append("BB squeeze + accumulation (coiled spring)")
            else:
                score += 0.7; reasons.append("BB squeeze (volatility compressing)")

        beta=s.get("beta_index")
        if beta is not None:
            if 0.8<=beta<=2.2: score+=0.4
            elif beta>3.0:     score-=0.3; warnings.append(f"high beta ({beta:.2f})")
            elif beta<0.4:     score-=0.2; warnings.append(f"low beta ({beta:.2f})")

        if s.get("macd") is not None:
            if s["macd"]>0: score+=0.8; reasons.append("MACD positive")
            else:           score-=0.5

        if s.get("vol_spike",0)>=1.8:   score+=1.6; reasons.append(f"vol spike {s['vol_spike']}x")
        elif s.get("vol_spike",0)<1.0:  score-=0.4; warnings.append("volume not confirming")

        if support_gap is not None:
            if support_gap<=3.0:  score+=1.5; reasons.append(f"near support ({support_gap:.1f}% above)")
            elif support_gap>=8.0:score-=0.8
        if resistance_gap is not None:
            if resistance_gap<=2.0:score-=1.0; warnings.append(f"close to resistance ({resistance_gap:.1f}% away)")
            elif resistance_gap>=6.0:score+=0.7; reasons.append("room to target")

        sent=s.get("sentiment",{}).get("score",0)
        if sent>0:  score+=min(1.5,0.5+0.4*sent);   reasons.append(f"positive news ({sent:+d})")
        elif sent<0:score-=min(1.2,0.4+0.3*abs(sent));warnings.append(f"negative news ({sent:+d})")

        if s.get("change",0)>8:  warnings.append("extended today"); score-=0.5
        elif -2<=s.get("change",0)<=4: score+=0.5

        gap = s.get("gap_pct", 0)
        vs  = s.get("vol_spike", 1.0)
        if gap >= 3.0 and vs >= 1.5:  score += 1.2; reasons.append(f"gap-up {gap:+.1f}% on volume")
        elif gap >= 1.5:               score += 0.5; reasons.append(f"gap-up {gap:+.1f}%")
        elif gap <= -3.0:              score -= 0.6; warnings.append(f"gap-down {gap:+.1f}%")

        ed, edays = (None,None)
        if include_earnings:
            ed, edays = fetch_earnings_date(s["symbol"])
            if edays is not None and 0<=edays<=5:
                score-=2.5; warnings.append(f"earnings in {edays}d ({ed}) — BUY blocked")

        if sector:
            rank=sector_ranks.get(sector)
            if rank==1:   score+=1.0; reasons.append(f"top sector ({sector})")
            elif rank==2: score+=0.5
            elif rank and rank>=5: score-=0.3; warnings.append(f"lagging sector ({sector})")

            if all_stocks:
                sec_mom, n_peers = self.sector_momentum(sector, all_stocks)
                if n_peers >= 2:
                    if sec_mom >= 2.0:
                        score += 1.2; reasons.append(f"sector momentum +{sec_mom}%")
                    elif sec_mom <= -2.0:
                        score -= 2.0; warnings.append(f"sector selling off ({sec_mom}%)")

        rr=round((target-s["price"])/max(s["price"]-stop,0.01),2)
        if rr>=1.8:  score+=0.8
        elif rr<1.1: score-=0.5; warnings.append(f"weak R/R ({rr}R)")

        _avail = max(portfolio_cash*(1-self.STRATEGY["cash_buffer_pct"]), s["price"]*2)
        shares,local_cost,pct=self.calc_position_size(
            _avail, s["price"], s.get("volatility"), self.STRATEGY["max_position_pct"])
        cad_cost=cad_cost_for_trade(s["symbol"],shares,s["price"],usd_cad)
        while shares>1 and cad_cost>_avail:
            shares-=1
            cad_cost=cad_cost_for_trade(s["symbol"],shares,s["price"],usd_cad)
        if shares<=0:
            shares=1; cad_cost=cad_cost_for_trade(s["symbol"],1,s["price"],usd_cad)

        buy_ok=(rr>=1.5
                and not any("earnings" in w for w in warnings)
                and not any("close to resistance" in w for w in warnings))

        signal="SKIP"
        if   score>=self.STRATEGY["buy_score"]   and buy_ok: signal="BUY"
        elif score>=self.STRATEGY["watch_score"]:             signal="WATCH"

        entry_tip=f"Prefer entries ${max(stop,s['price']*0.985):.2f}–${s['price']:.2f}"
        if support_gap and support_gap<=3.0:
            entry_tip=f"Best entry near support ${s['support']:.2f}"
        elif s.get("rsi_1h") and s["rsi_1h"]<35:
            entry_tip="Wait for 1h RSI >35 before entering"
        elif resistance_gap and resistance_gap<=3.0:
            entry_tip="Wait for pullback; too close to resistance"

        rec = {"symbol":s["symbol"],"score":round(score,2),"signal":signal,
               "reasons":reasons[:6],"warnings":warnings[:6],
               "stop":stop,"target":target,"stop_pct":stop_pct,"risk_reward":rr,
               "shares":shares,"cost_local":local_cost,"cost_cad":cad_cost,
               "position_pct":pct,"entry_tip":entry_tip,
               "earnings_days":edays,"earnings_date":ed,"sector":sector,"stock":s}
        rec["options"] = _options_flag(rec, s)
        return rec

    # ── Analysis ──────────────────────────────────────────────────────────────
    def run_analysis(self, portfolio, stocks, headlines, ticker_sentiment):
        """Score all stocks and print trade plan."""
        if not stocks: return []
        usd_cad      = self.fetch_usdcad_rate()
        sector_ranks = self.build_sector_rank_map(stocks)
        regime_bull, regime_label = market_regime()

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(fetch_weekly_trend, [s["symbol"] for s in stocks]))

        fast = sorted(
            [self.evaluate_stock(s, portfolio["cash"], usd_cad, sector_ranks,
                                 include_earnings=False, regime_bullish=regime_bull,
                                 all_stocks=stocks)
             for s in stocks],
            key=lambda x: x["score"], reverse=True)
        shortlist = {r["symbol"] for r in fast[:15]}

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(fetch_earnings_date, list(shortlist)))

        recs = sorted(
            [self.evaluate_stock(s, portfolio["cash"], usd_cad, sector_ranks,
                                 include_earnings=(s["symbol"] in shortlist),
                                 regime_bullish=regime_bull, all_stocks=stocks)
             for s in stocks],
            key=lambda x: x["score"], reverse=True)

        buys   = [r for r in recs if r["signal"]=="BUY"  ][:5]
        watches= [r for r in recs if r["signal"]=="WATCH"][:5]

        bar = "═"*68
        cprint(f"\n[bold magenta]{bar}[/]" if RICH else f"\n{bar}")
        cprint("[bold magenta]  📐 RULE-BASED TRADE PLAN[/]" if RICH else "  📐 RULE-BASED TRADE PLAN")
        cprint(f"[bold magenta]{bar}[/]" if RICH else bar)
        cprint(f"\n[bold]Market regime:[/] {regime_label}" if RICH else f"\nMarket regime: {regime_label}")

        cprint("\n[bold]Top headlines:[/]" if RICH else "\nTop headlines:")
        for h in headlines[:5]:
            cprint(f"  [dim]• {h[:100]}[/]" if RICH else f"  • {h[:100]}")

        if buys:
            cprint("\n[bold green]✅ BUY candidates[/]" if RICH else "\n✅ BUY candidates")
            for rec in buys[:3]:
                print_trade_card(rec, usd_cad)
                self.log_paper_trade(rec)
        else:
            cprint("\n[yellow]No BUY setups right now — best names in WATCH.[/]"
                   if RICH else "\nNo BUY setups right now.")

        if watches:
            cprint("\n[bold yellow]👀 WATCH list[/]" if RICH else "\n👀 WATCH list")
            for rec in watches[:3]: print_trade_card(rec, usd_cad)

        scan_prices = {s["symbol"]: s["price"] for s in stocks}
        missing = [sym for sym in portfolio.get("holdings", {}) if sym not in scan_prices]
        if missing:
            scan_prices.update(fetch_prices(missing))
        print_holdings_review(portfolio, scan_prices)

        cprint("\n[bold]Action plan:[/]" if RICH else "\nAction plan:")
        if buys:
            b=buys[0]
            cprint(f"  1) {b['symbol']} — strongest score ({b['score']:.2f})")
            if len(buys)>1: cprint(f"  2) {buys[1]['symbol']} as secondary")
            cprint("  3) Keep ≥10% cash buffer. Max 1-2 new trades at once.")
        else:
            cprint("  1) No clean trigger. Wait for support or stronger 1h RSI.")
            if watches: cprint(f"  2) Watch {watches[0]['symbol']}")
            cprint("  3) Preserve cash.")
        return recs

    # ── Claude helpers ────────────────────────────────────────────────────────
    def explain_symbol_setup(self, symbol, scanned_stocks, portfolio_cash):
        usd_cad      = self.fetch_usdcad_rate()
        sector_ranks = self.build_sector_rank_map(scanned_stocks)
        target = next((s for s in scanned_stocks if s["symbol"]==symbol), None)
        if not target:
            target = fetch_stock(symbol)
            if target.get("ok"):
                target["sentiment"]={"score":0,"label":"⚪ neutral","headlines":[]}
                scanned_stocks = scanned_stocks + [target]
                sector_ranks   = self.build_sector_rank_map(scanned_stocks)
            else:
                cprint(f"[red]❌ Could not fetch {symbol}[/]" if RICH else f"❌ {symbol}")
                return None
        regime_bull, _ = market_regime()
        rec = self.evaluate_stock(target, portfolio_cash, usd_cad, sector_ranks,
                                  regime_bullish=regime_bull, all_stocks=scanned_stocks)
        print_trade_card(rec, usd_cad)
        return rec

    def explain_with_claude(self, rec, portfolio, scanned_stocks, headlines):
        s = rec["stock"]
        ctx = build_context(portfolio, scanned_stocks, headlines, [rec])
        prompt = f"""Explain this trading signal to a beginner investor in Canada.
Do not change the signal. Do not invent data. Be concise.

Signal: {rec['signal']}  Score: {rec['score']:.2f}
Ticker: {s['symbol']}  Price: ${s['price']:.2f}
Stop: ${rec['stop']:.2f}  Target: ${rec['target']:.2f}  Entry tip: {rec['entry_tip']}
Reasons: {', '.join(rec['reasons']) or 'mixed'}
Warnings: {', '.join(rec['warnings']) or 'none'}
RSI-d: {s.get('rsi_d')}  RSI-1h: {s.get('rsi_1h')}  Vol spike: {s.get('vol_spike')}x
Support: {s.get('support')}  Resistance: {s.get('resistance')}
Sentiment: {s.get('sentiment',{}).get('label','unknown')}

{ctx}

Write 3 short parts: (1) Why this signal, (2) Biggest risks, (3) What to watch next."""
        print_claude_response(f"Claude explains {rec['symbol']}",
                              ask_claude_capture(prompt))

    def review_portfolio_with_claude(self, portfolio, scanned_stocks, headlines, recs):
        ctx = build_context(portfolio, scanned_stocks, headlines, recs)
        prompt = f"""Review this beginner investor's portfolio in Canada. Be practical and concise.
{ctx}
Give: 1) Portfolio snapshot  2) Risks  3) Closest attention  4) One action plan."""
        print_claude_response("Claude portfolio review", ask_claude_capture(prompt))

    def ask_general_claude(self, question, portfolio, scanned_stocks, headlines, recs):
        ctx = build_context(portfolio, scanned_stocks, headlines, recs)
        prompt = f"""You are a trading assistant for a beginner investor in Canada.
Educational only — not financial advice. Do not invent data.
{ctx}
User: {question}
Answer in plain language, concisely."""
        print_claude_response("Claude answer", ask_claude_capture(prompt))

    # ── Monitoring ────────────────────────────────────────────────────────────
    def monitor_stop_losses(self, portfolio, interval=300):
        cprint("\n[cyan]👁  Stop-loss monitor running (background)[/]"
               if RICH else "\n👁  Stop-loss monitor running...")
        while True:
            time.sleep(interval)
            p = self.load_portfolio()
            if not p["holdings"]: continue
            live = fetch_prices(list(p["holdings"].keys()))
            for sym, pos in p["holdings"].items():
                price=live.get(sym)
                if not price: continue
                sl =p.get("stop_losses",{}).get(sym)
                tgt=p.get("targets",{}).get(sym)
                pct=(price-pos["avg_cost"])/pos["avg_cost"]*100
                if sl and price<=sl:
                    msg=f"🔴 STOP HIT: {sym} @ ${price:.2f} (stop ${sl}) — SELL"
                    cprint(f"[bold red]{msg}[/]" if RICH else msg)
                    self.send_telegram(f"⚠️ {msg}")
                elif tgt and price>=tgt:
                    msg=f"🎯 TARGET HIT: {sym} @ ${price:.2f} — consider selling"
                    cprint(f"[bold green]{msg}[/]" if RICH else msg)
                    self.send_telegram(f"✅ {msg}")
                elif pct<=-8:
                    msg=f"⚠️ {sym} down {pct:.1f}% — review"
                    cprint(f"[bold yellow]{msg}[/]" if RICH else msg)

    def show_accuracy(self):
        acc = self.load_accuracy()
        sigs=[s for s in acc.get("signals",[]) if "pnl" in s]
        if not sigs:
            cprint("[yellow]No completed trades yet.[/]" if RICH else "No completed trades yet.")
            return
        wins=sum(1 for s in sigs if s["pnl"]>0)
        cprint(f"\n[bold]📊 Accuracy: {wins}W/{len(sigs)-wins}L ({wins/len(sigs)*100:.0f}% win rate)[/]"
               if RICH else f"\n📊 Accuracy: {wins}W/{len(sigs)-wins}L ({wins/len(sigs)*100:.0f}%)")

    # ── Unknown-command handler ───────────────────────────────────────────────
    def handle_unknown_input(self, user_input, scanned_stocks, portfolio):
        raw = user_input.strip()
        is_ticker = bool(re.match(r'^[A-Za-z]{1,6}([-\.][A-Za-z0-9]{1,4})?$', raw))
        if is_ticker:
            self.explain_symbol_setup(raw.upper(), scanned_stocks, portfolio["cash"])
        else:
            hint = (
                "Unknown command. Try:\n"
                "  idea SOFI          score a ticker\n"
                "  why SOFI           Claude explains the signal\n"
                "  ask <question>     free-form question to Claude\n"
                "  review portfolio   Claude reviews your holdings\n"
                "  price RIVN         live price\n"
                "  scan               fresh market scan\n"
                "  buy / sell / portfolio / news / accuracy / quit"
            )
            cprint(f"[yellow]{hint}[/]" if RICH else hint)

    # ── Chat loop ─────────────────────────────────────────────────────────────
    def chat_loop(self, portfolio, scanned_stocks, headlines, ticker_sentiment):
        live_prices  = {s["symbol"]:s["price"] for s in scanned_stocks}
        last_headlines = headlines
        recommendations = []
        _analysis_dirty = bool(scanned_stocks)

        cprint(f"\n[bold cyan]{'─'*58}[/]" if RICH else f"\n{'─'*58}")
        cprint("[bold cyan]  💬 INTERACTIVE MODE[/]" if RICH else "  💬 INTERACTIVE MODE")
        for cmd,desc in [
            ("buy TICKER SHARES PRICE","log a buy"),
            ("import TICKER SHARES PRICE","add existing holding"),
            ("sell TICKER SHARES PRICE","log a sell"),
            ("setstop TICKER PRICE","set stop loss"),
            ("settarget TICKER PRICE","set price target"),
            ("portfolio","show P&L"),
            ("price TICKER","live price"),
            ("idea TICKER","score one ticker"),
            ("why TICKER","Claude explains signal"),
            ("review portfolio","Claude reviews holdings"),
            ("ask QUESTION","free-form Claude question"),
            ("scan","fresh scan + news"),
            ("news","latest headlines"),
            ("accuracy","win rate stats"),
            ("validate","check paper trade outcomes vs backtest"),
            ("quit","exit"),
        ]:
            cprint(f"  [cyan]{cmd:<32}[/] [dim]{desc}[/]" if RICH else f"  {cmd:<32} {desc}")
        cprint(f"[bold cyan]{'─'*58}[/]\n" if RICH else f"{'─'*58}\n")

        while True:
            if _analysis_dirty and scanned_stocks:
                cprint("\n[bold cyan]⏳ Scoring stocks & fetching earnings dates (may take ~60s)...[/]"
                       if RICH else "\n⏳ Scoring stocks & fetching earnings dates (may take ~60s)...")
                recommendations = self.run_analysis(portfolio, scanned_stocks,
                                                    last_headlines, ticker_sentiment)
                _analysis_dirty = False

            try:
                user_input = input("\n  You: ").strip()
            except (KeyboardInterrupt, EOFError):
                cprint("\n[bold]Goodbye! 🚀[/]" if RICH else "\nGoodbye! 🚀")
                break
            if not user_input: continue
            cmd = user_input.lower().split()

            if cmd[0]=="quit":
                cprint("[bold]Goodbye! 🚀[/]" if RICH else "Goodbye! 🚀"); break

            elif cmd[0]=="portfolio":
                fresh=fetch_prices(list(portfolio["holdings"].keys())) if portfolio["holdings"] else {}
                live_prices.update(fresh)
                portfolio_summary(portfolio, live_prices)

            elif cmd[0]=="accuracy":
                self.show_accuracy()

            elif cmd[0]=="validate":
                self.validate_paper_trades()

            elif cmd[0]=="news":
                cprint(f"\n[bold]📰 Headlines:[/]" if RICH else "\n📰 Headlines:")
                for h in last_headlines[:10]:
                    cprint(f"  [dim]• {h[:100]}[/]" if RICH else f"  • {h[:100]}")

            elif cmd[0]=="price" and len(cmd)>=2:
                sym=cmd[1].upper()
                s=fetch_stock(sym)
                if s["ok"]:
                    live_prices[sym]=s["price"]
                    rsi_str =f"  RSI-d:{s['rsi_d']} RSI-1h:{s['rsi_1h']}" if s["rsi_d"] else ""
                    sup_str =f"  sup:${s['support']} res:${s['resistance']}" if s["support"] else ""
                    cprint(f"  [cyan]{sym}[/]: ${s['price']}  {s['change']:+.1f}%  vol:{s['vol_spike']}x{rsi_str}{sup_str}"
                           if RICH else f"  {sym}: ${s['price']}  {s['change']:+.1f}%  vol:{s['vol_spike']}x{rsi_str}{sup_str}")
                else:
                    cprint(f"[red]❌ {sym} failed[/]" if RICH else f"❌ {sym} failed")

            elif cmd[0]=="idea" and len(cmd)>=2:
                rec=self.explain_symbol_setup(cmd[1].upper(), scanned_stocks, portfolio["cash"])
                if rec and rec["stock"].get("ok"): live_prices[cmd[1].upper()]=rec["stock"]["price"]

            elif cmd[0]=="why" and len(cmd)>=2:
                rec=self.explain_symbol_setup(cmd[1].upper(), scanned_stocks, portfolio["cash"])
                if rec: self.explain_with_claude(rec, portfolio, scanned_stocks, last_headlines)

            elif cmd[0]=="review" and len(cmd)>=2 and cmd[1]=="portfolio":
                self.review_portfolio_with_claude(portfolio, scanned_stocks, last_headlines, recommendations)

            elif cmd[0]=="ask" and len(user_input)>4:
                q=user_input[4:].strip()
                if q: self.ask_general_claude(q, portfolio, scanned_stocks, last_headlines, recommendations)

            elif cmd[0]=="scan":
                extra=[t.upper() for t in cmd[1:]]
                scanned_stocks, last_headlines, ticker_sentiment = self.scan_all(extra)
                live_prices={s["symbol"]:s["price"] for s in scanned_stocks}
                _analysis_dirty=True

            elif cmd[0]=="import" and len(cmd)>=4:
                try:
                    sym,shares,price=parse_trade_cmd(cmd)
                except ValueError as e:
                    cprint(f"[red]❌ {e}[/]" if RICH else f"❌ {e}"); continue
                if sym in portfolio["holdings"]:
                    old=portfolio["holdings"][sym]
                    nt =old["shares"]+shares
                    avg=(old["avg_cost"]*old["shares"]+price*shares)/nt
                    portfolio["holdings"][sym]={"shares":round(nt,8),"avg_cost":round(avg,4)}
                else:
                    portfolio["holdings"][sym]={"shares":round(shares,8),"avg_cost":price}
                portfolio["history"].append({"action":"IMPORT","symbol":sym,"shares":shares,
                                             "price":price,"date":datetime.now().isoformat()})
                self.save_portfolio(portfolio)
                cprint(f"  [green]✅ Imported {format_shares(shares)}x {sym} @ ${price:.2f}[/]"
                       if RICH else f"  ✅ Imported {format_shares(shares)}x {sym} @ ${price:.2f}")
                fresh=fetch_prices(list(portfolio["holdings"].keys()))
                live_prices.update(fresh)
                _analysis_dirty=True

            elif cmd[0]=="setstop" and len(cmd)>=3:
                try: sym,_,price=parse_trade_cmd(["setstop",cmd[1],"1",cmd[2]])
                except ValueError as e:
                    cprint(f"[red]❌ {e}[/]" if RICH else f"❌ {e}"); continue
                portfolio.setdefault("stop_losses",{})[sym]=price
                self.save_portfolio(portfolio)
                cprint(f"  [green]✅ Stop loss set: {sym} @ ${price:.2f}[/]"
                       if RICH else f"  ✅ Stop: {sym} @ ${price:.2f}")
                _analysis_dirty=True

            elif cmd[0]=="settarget" and len(cmd)>=3:
                try: sym,_,price=parse_trade_cmd(["settarget",cmd[1],"1",cmd[2]])
                except ValueError as e:
                    cprint(f"[red]❌ {e}[/]" if RICH else f"❌ {e}"); continue
                portfolio.setdefault("targets",{})[sym]=price
                self.save_portfolio(portfolio)
                cprint(f"  [green]✅ Target set: {sym} @ ${price:.2f}[/]"
                       if RICH else f"  ✅ Target: {sym} @ ${price:.2f}")
                _analysis_dirty=True

            elif cmd[0]=="buy" and len(cmd)>=4:
                try: sym,shares,price=parse_trade_cmd(cmd)
                except ValueError as e:
                    cprint(f"[red]❌ {e}[/]" if RICH else f"❌ {e}"); continue
                cost=shares*price
                if cost>portfolio["cash"]+0.01:
                    cprint(f"[red]❌ Need ${cost:.2f}, have ${portfolio['cash']:.2f}[/]"
                           if RICH else f"❌ Need ${cost:.2f}, have ${portfolio['cash']:.2f}"); continue
                portfolio["cash"]-=cost
                if sym in portfolio["holdings"]:
                    old=portfolio["holdings"][sym]; nt=old["shares"]+shares
                    avg=(old["avg_cost"]*old["shares"]+price*shares)/nt
                    portfolio["holdings"][sym]={"shares":round(nt,8),"avg_cost":round(avg,4)}
                else:
                    portfolio["holdings"][sym]={"shares":round(shares,8),"avg_cost":price}
                portfolio["history"].append({"action":"BUY","symbol":sym,"shares":shares,
                                             "price":price,"date":datetime.now().isoformat()})
                ls=fetch_stock(sym)
                if ls.get("ok"):
                    ls["sentiment"]={"score":0,"label":"⚪ neutral","headlines":[]}
                    stop_g,tgt_g,_=compute_trade_levels(ls)
                else:
                    stop_g,tgt_g=round(price*0.94,2),round(price*1.10,2)
                portfolio.setdefault("stop_losses",{}).setdefault(sym,stop_g)
                portfolio.setdefault("targets",{}).setdefault(sym,tgt_g)
                self.save_portfolio(portfolio)
                live_prices[sym]=price
                acc=self.load_accuracy()
                acc["signals"].append({"symbol":sym,"buy_price":price,
                                       "date":datetime.now().isoformat(),"pnl":None})
                self.save_accuracy(acc)
                cprint(f"  [green]✅ BUY {format_shares(shares)}x {sym} @ ${price:.2f} = ${cost:.2f}[/]"
                       if RICH else f"  ✅ BUY {format_shares(shares)}x {sym} @ ${price:.2f}")
                cprint(f"  [dim]stop ${portfolio['stop_losses'][sym]}  target ${portfolio['targets'][sym]}[/]"
                       if RICH else f"  stop ${portfolio['stop_losses'][sym]}  target ${portfolio['targets'][sym]}")
                cprint(f"  [cyan]💰 Cash: ${portfolio['cash']:.2f}[/]"
                       if RICH else f"  💰 Cash: ${portfolio['cash']:.2f}")
                _analysis_dirty=True
                # Auto-place bracket order on IBKR paper account
                try:
                    import ibkr_data as _id
                    _stop_lvl   = portfolio["stop_losses"][sym]
                    _target_lvl = portfolio["targets"][sym]
                    _bracket = _id.place_bracket_order(sym, int(shares), _target_lvl, _stop_lvl, entry=price)
                    cprint(f"  [cyan]📋 Bracket order placed on IBKR paper:[/]\n"
                           f"     buy #{_bracket['buy_order_id']}  "
                           f"target #{_bracket['target_order_id']} @ ${_target_lvl}  "
                           f"stop #{_bracket['stop_order_id']} @ ${_stop_lvl}"
                           if RICH else
                           f"  📋 Bracket placed — buy #{_bracket['buy_order_id']}  "
                           f"target #{_bracket['target_order_id']} @ ${_target_lvl}  "
                           f"stop #{_bracket['stop_order_id']} @ ${_stop_lvl}")
                except Exception as _e:
                    cprint(f"  [yellow]⚠️  Bracket order failed: {_e}[/]"
                           if RICH else f"  ⚠️  Bracket order failed: {_e}")

            elif cmd[0]=="sell" and len(cmd)>=4:
                try: sym,shares,price=parse_trade_cmd(cmd)
                except ValueError as e:
                    cprint(f"[red]❌ {e}[/]" if RICH else f"❌ {e}"); continue
                held=float(portfolio["holdings"].get(sym,{}).get("shares",0))
                if shares>held+1e-9:
                    cprint(f"[red]❌ Only hold {held} shares of {sym}[/]"
                           if RICH else f"❌ Only hold {held}"); continue
                proceeds=shares*price
                pnl=(price-portfolio["holdings"][sym]["avg_cost"])*shares
                portfolio["cash"]+=proceeds
                portfolio["holdings"][sym]["shares"]=round(held-shares,8)
                if portfolio["holdings"][sym]["shares"]<=1e-8:
                    del portfolio["holdings"][sym]
                    portfolio.get("stop_losses",{}).pop(sym,None)
                    portfolio.get("targets",{}).pop(sym,None)
                portfolio["history"].append({"action":"SELL","symbol":sym,"shares":shares,
                                             "price":price,"pnl":round(pnl,2),
                                             "date":datetime.now().isoformat()})
                acc=self.load_accuracy()
                for sig in reversed(acc["signals"]):
                    if sig["symbol"]==sym and sig.get("pnl") is None:
                        sig["pnl"]=round(pnl,2); break
                self.save_accuracy(acc); self.save_portfolio(portfolio)
                icon="🟢" if pnl>=0 else "🔴"
                cprint(f"  [green]✅ SELL {format_shares(shares)}x {sym} @ ${price:.2f} = ${proceeds:.2f}[/]"
                       if RICH else f"  ✅ SELL {format_shares(shares)}x {sym} = ${proceeds:.2f}")
                cprint(f"  [{'green' if pnl>=0 else 'red'}]{icon} P&L: ${pnl:+.2f}[/]"
                       if RICH else f"  {icon} P&L: ${pnl:+.2f}")
                cprint(f"  [cyan]💰 Cash: ${portfolio['cash']:.2f}[/]"
                       if RICH else f"  💰 Cash: ${portfolio['cash']:.2f}")
                _analysis_dirty=True

            else:
                self.handle_unknown_input(user_input, scanned_stocks, portfolio)

    # ── Main entry point ──────────────────────────────────────────────────────
    def main(self):
        if "--setup-telegram" in sys.argv:
            self.setup_telegram(); return

        if RICH:
            console.print(Panel.fit(self._banner_rich, border_style="cyan"))
        else:
            for line in self._banner_plain:
                print(line)
            print("\n💡 Tip: pip install rich  for a much better display\n")

        portfolio = self.load_portfolio()
        if portfolio["holdings"]:
            live=fetch_prices(list(portfolio["holdings"].keys()))
            portfolio_summary(portfolio, live)

        extra=[t.upper() for t in sys.argv[1:] if not t.startswith("--")]

        stocks, headlines, ticker_sentiment = [], [], {}
        if extra or "--scan-startup" in sys.argv:
            stocks, headlines, ticker_sentiment = self.scan_all(extra)
        else:
            cprint("\n[yellow]Type 'scan' to start a fresh market scan.[/]"
                   if RICH else "\nType 'scan' to start a fresh market scan.")
            try: headlines, _, ticker_sentiment = self.fetch_news()
            except Exception: pass

        if portfolio["holdings"]:
            threading.Thread(target=self.monitor_stop_losses, args=(portfolio,),
                             daemon=True).start()

        self.chat_loop(portfolio, stocks, headlines, ticker_sentiment)
