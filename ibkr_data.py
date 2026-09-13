"""
ibkr_data.py — IBKR-first data layer with Yahoo HTTP fallback.

Public API
----------
get_daily_bars(symbol, duration)      -> list[{date,open,high,low,close,volume}]
get_hourly_closes(symbol, days)       -> list[float]
get_intraday_bars(symbol, interval, period) -> list[(ts,o,h,l,c,v)]
get_options_contract(ticker, direction, dte_min) -> dict | None

Connection is a lazy singleton; set env vars to override defaults:
  TWS_HOST, TWS_PORT, IBKR_CLIENT_ID
"""

import os, threading, warnings, logging, time as _time
from datetime import datetime, timedelta

# ib_insync spawns async coroutines on non-owning threads; all harmless, suppress.
for _msg in ("qualifyContractsAsync", "connectAsync"):
    warnings.filterwarnings("ignore", category=RuntimeWarning,
                            message=f"coroutine.*{_msg}.*was never awaited")

# ib_insync logs Error 200/162/354 via its own wrapper logger independently of
# errorEvent. Filter those out — our try/except + Yahoo fallback handles them.
class _IbNoiseFilter(logging.Filter):
    _SUPPRESS = {"Error 162,", "Error 200,", "Error 354,", "Error 10167,"}
    def filter(self, record):
        msg = record.getMessage()
        return not any(msg.startswith(s) for s in self._SUPPRESS)

for _logger_name in ("ib_insync.wrapper", "ib_insync.client", "ib_insync"):
    logging.getLogger(_logger_name).addFilter(_IbNoiseFilter())

TWS_HOST  = os.environ.get("TWS_HOST",  "172.18.176.1")
TWS_PORT  = int(os.environ.get("TWS_PORT",  "7496"))
CLIENT_ID = int(os.environ.get("IBKR_CLIENT_ID", "43"))  # different from MCP server's 42

# ^-prefixed index symbols → (IBKR name, exchange, whatToShow)
_INDEX_MAP = {
    "^VIX":  ("VIX",  "CBOE",    "TRADES"),
    "^SPX":  ("SPX",  "CBOE",    "TRADES"),
    "^GSPC": ("SPX",  "CBOE",    "TRADES"),
    "^NDX":  ("NDX",  "NASDAQ",  "TRADES"),
    "^DJI":  ("INDU", "NYSE",    "TRADES"),
    "^RUT":  ("RUT",  "RUSSELL", "TRADES"),
}


def _contract_info(symbol: str):
    """
    Return (contract, whatToShow) for use with reqHistoricalData.
    Handles indices (^VIX), forex (USDCAD=X), Canadian (.TO), and US stocks.
    """
    from ib_insync import Stock, Index, Forex
    sym = symbol.upper()

    if sym in _INDEX_MAP or sym.startswith("^"):
        name, exch, show = _INDEX_MAP.get(sym, (sym[1:], "CBOE", "TRADES"))
        return Index(name, exch), show

    if sym.endswith("=X"):
        return Forex(sym[:-2]), "MIDPOINT"

    if sym.endswith(".TO"):
        # SMART+CAD lets IBKR route to TSX/TSXV automatically.
        # Direct "TSX" exchange fails for cross-listed names like CRON, NXE.
        return Stock(sym[:-3], "SMART", "CAD"), "TRADES"

    return Stock(sym, "SMART", "USD"), "TRADES"


def _stock_contract(symbol: str):
    """Return a Stock-only contract (used by options paths which need an equity underlying)."""
    from ib_insync import Stock
    sym = symbol.upper()
    if sym.endswith(".TO"):
        return Stock(sym[:-3], "SMART", "CAD")
    return Stock(sym, "SMART", "USD")

_POOL_SIZE   = 2                        # TWS allows multiple clientIds simultaneously
_BASE_ID     = CLIENT_ID               # 43, 44
_pool_lock   = threading.Lock()
_pool: list  = []                      # list of IB() instances
_pool_idx    = 0                       # round-robin cursor

# Error codes that are expected and handled by our try/except + Yahoo fallback.
# Suppress them so they don't pollute the console.
_SUPPRESSED_ERRORS = {
    162,   # Historical data service error / scanner cancelled
    200,   # No security definition / invalid exchange
    354,   # Market data not subscribed
    10167, # Requested market data is not subscribed (variant)
}

def _silence_ib(ib):
    """Replace ib_insync's noisy error event with a filtered handler."""
    import logging
    _log = logging.getLogger("ibkr_data")
    def _handler(reqId, errorCode, errorString, contract):
        if errorCode not in _SUPPRESSED_ERRORS:
            _log.warning("IBKR %d (req %d): %s", errorCode, reqId, errorString)
    ib.errorEvent.clear()
    ib.errorEvent += _handler


def _get_ib():
    """
    Return a connected IB() instance from a small round-robin pool.
    Each slot gets its own clientId so TWS treats them as independent clients.
    If a slot is disconnected it is reconnected in-place before returning.
    """
    global _pool_idx
    with _pool_lock:
        # Initialise pool on first call
        if not _pool:
            from ib_insync import IB
            for i in range(_POOL_SIZE):
                ib = IB()
                ib.connect(TWS_HOST, TWS_PORT,
                            clientId=_BASE_ID + i, timeout=8, readonly=True)
                _silence_ib(ib)
                _pool.append(ib)

        # Round-robin pick
        idx = _pool_idx % _POOL_SIZE
        _pool_idx += 1
        ib = _pool[idx]

        # Reconnect stale slot
        if not ib.isConnected():
            from ib_insync import IB
            try:
                ib.disconnect()
            except Exception:
                pass
            ib = IB()
            ib.connect(TWS_HOST, TWS_PORT,
                        clientId=_BASE_ID + idx, timeout=8, readonly=True)
            _silence_ib(ib)
            _pool[idx] = ib

        return ib


# ── IBKR fetchers ─────────────────────────────────────────────────────────────

def _ib_daily_bars(symbol: str, duration: str) -> list[dict]:
    ib = _get_ib()
    contract, what = _contract_info(symbol.upper())
    ib.qualifyContracts(contract)
    bars = ib.reqHistoricalData(
        contract,
        endDateTime    = "",
        durationStr    = duration,
        barSizeSetting = "1 day",
        whatToShow     = what,
        useRTH         = True,
        formatDate     = 1,
    )
    return [
        {"date":   str(b.date),
         "open":   round(float(b.open),  4),
         "high":   round(float(b.high),  4),
         "low":    round(float(b.low),   4),
         "close":  round(float(b.close), 4),
         "volume": int(b.volume)}
        for b in bars
    ]


def _ib_hourly_closes(symbol: str, days: int) -> list[float]:
    ib = _get_ib()
    contract, what = _contract_info(symbol.upper())
    ib.qualifyContracts(contract)
    bars = ib.reqHistoricalData(
        contract,
        endDateTime    = "",
        durationStr    = f"{days} D",
        barSizeSetting = "1 hour",
        whatToShow     = what,
        useRTH         = True,
        formatDate     = 1,
    )
    return [round(float(b.close), 4) for b in bars]


def _ib_intraday_bars(symbol: str, bar_size: str, duration: str) -> list[tuple]:
    ib = _get_ib()
    contract, what = _contract_info(symbol.upper())
    ib.qualifyContracts(contract)
    bars = ib.reqHistoricalData(
        contract,
        endDateTime    = "",
        durationStr    = duration,
        barSizeSetting = bar_size,
        whatToShow     = what,
        useRTH         = True,
        formatDate     = 2,  # epoch seconds
    )
    # Indices and forex have volume=0 by nature — only enforce volume > 0 for equities
    needs_volume = what == "TRADES" and not symbol.upper().startswith("^")
    result = []
    for b in bars:
        if None in (b.open, b.high, b.low, b.close):
            continue
        if needs_volume and b.volume <= 0:
            continue
        ts = int(b.date) if isinstance(b.date, (int, float)) else int(b.date.timestamp())
        result.append((ts,
                        round(float(b.open),  4),
                        round(float(b.high),  4),
                        round(float(b.low),   4),
                        round(float(b.close), 4),
                        int(b.volume)))
    return result


def _ib_intraday_bars_chunked(symbol: str, bar_size: str, n_chunks: int = 10) -> list[tuple]:
    """
    IBKR caps 15-min bar requests at 60 calendar days per call.
    This fetches n_chunks × 60-day windows going back in time and stitches them.
    n_chunks=10 → ~600 calendar days ≈ 2 years of 15-min bars.

    Respects IBKR's pacing rule: waits 2s between chunks so we don't trip
    the "60 requests / 10 min" guard.
    """
    ib = _get_ib()
    contract, what = _contract_info(symbol.upper())
    ib.qualifyContracts(contract)
    needs_volume = what == "TRADES" and not symbol.upper().startswith("^")

    all_bars: dict[int, tuple] = {}
    end_dt = datetime.now()

    for chunk in range(n_chunks):
        end_str = end_dt.strftime("%Y%m%d %H:%M:%S")
        try:
            raw = ib.reqHistoricalData(
                contract,
                endDateTime    = end_str,
                durationStr    = "60 D",
                barSizeSetting = bar_size,
                whatToShow     = what,
                useRTH         = True,
                formatDate     = 2,
            )
        except Exception:
            break

        if not raw:
            break

        added = 0
        for b in raw:
            if None in (b.open, b.high, b.low, b.close):
                continue
            if needs_volume and b.volume <= 0:
                continue
            ts = int(b.date) if isinstance(b.date, (int, float)) else int(b.date.timestamp())
            if ts not in all_bars:
                all_bars[ts] = (ts,
                                round(float(b.open),  4),
                                round(float(b.high),  4),
                                round(float(b.low),   4),
                                round(float(b.close), 4),
                                int(b.volume))
                added += 1

        # Earliest bar in this chunk → next chunk ends one day before it
        earliest_ts = min(int(b.date) if isinstance(b.date, (int, float))
                          else int(b.date.timestamp()) for b in raw)
        end_dt = datetime.fromtimestamp(earliest_ts) - timedelta(days=1)

        if added == 0:
            break  # No new bars — we've reached the limit of available history

        if chunk < n_chunks - 1:
            _time.sleep(2)  # IBKR pacing: 2s between historical data requests

    return sorted(all_bars.values(), key=lambda b: b[0])


# ── Yahoo HTTP fallbacks ───────────────────────────────────────────────────────

_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"
_HEADERS     = {"User-Agent": "Mozilla/5.0"}

# IBKR duration → Yahoo range
_DUR_TO_RANGE = {
    "2 Y": "2y", "1 Y": "1y", "6 M": "6mo", "3 M": "3mo",
    "60 D": "60d", "30 D": "30d", "14 D": "14d", "7 D": "7d", "1 D": "1d",
}

def _yahoo_parse_daily(data, symbol) -> list[dict]:
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        return []
    ts  = result.get("timestamp", [])
    qb  = (result.get("indicators", {}).get("quote") or [{}])[0]
    ops, his, los, cls, vls = (qb.get(k, []) for k in ("open","high","low","close","volume"))
    rows = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c = ops[i], his[i], los[i], cls[i]
            v = vls[i]
            if None in (o, h, l, c):
                continue
            rows.append({
                "date":   datetime.fromtimestamp(t).strftime("%Y-%m-%d"),
                "open":   round(o, 4), "high": round(h, 4),
                "low":    round(l, 4), "close": round(c, 4),
                "volume": int(v) if v else 0,
            })
        except (IndexError, TypeError):
            continue
    return rows


def _yahoo_daily_bars(symbol: str, duration: str) -> list[dict]:
    import requests
    rng = _DUR_TO_RANGE.get(duration, "1y")
    url = f"{_YAHOO_CHART}/{symbol.upper()}?interval=1d&range={rng}"
    try:
        r = requests.get(url, timeout=9, headers=_HEADERS)
        return _yahoo_parse_daily(r.json(), symbol)
    except Exception:
        return []


def _yahoo_hourly_closes(symbol: str, days: int) -> list[float]:
    import requests
    rng = "14d" if days >= 14 else "7d"
    url = f"{_YAHOO_CHART}/{symbol.upper()}?interval=1h&range={rng}"
    try:
        r  = requests.get(url, timeout=8, headers=_HEADERS)
        result = (r.json().get("chart", {}).get("result") or [None])[0]
        if not result:
            return []
        closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close", [])
        return [round(float(c), 4) for c in closes if c is not None]
    except Exception:
        return []


def _yahoo_intraday_bars(symbol: str, interval: str, period: str) -> list[tuple]:
    import requests
    rng = _PERIOD_TO_RANGE.get(period, period)
    url = f"{_YAHOO_CHART}/{symbol.upper()}?interval={interval}&range={rng}"
    try:
        r = requests.get(url, timeout=9, headers=_HEADERS)
        result = (r.json().get("chart", {}).get("result") or [None])[0]
        if not result:
            return []
        qb  = (result.get("indicators", {}).get("quote") or [{}])[0]
        ts  = result.get("timestamp", [])
        ops, his, los, cls, vls = (qb.get(k, []) for k in ("open","high","low","close","volume"))
        return [
            (t, o, h, l, c, v)
            for t, o, h, l, c, v in zip(ts, ops, his, los, cls, vls)
            if None not in (t, o, h, l, c, v) and v > 0
        ]
    except Exception:
        return []


# ── Public API ────────────────────────────────────────────────────────────────

# IV Rank cache: {symbol: {"iv_rank": float, "iv_current": float, "ts": epoch}}
_iv_rank_cache: dict = {}
_IV_RANK_TTL = 3600  # 1 hour — IV rank changes slowly


def get_iv_rank(symbol: str) -> "dict | None":
    """
    Compute IV Rank from 1 year of daily OPTION_IMPLIED_VOLATILITY bars via IBKR.

    IV Rank = (current_IV - 52wk_low) / (52wk_high - 52wk_low) * 100
    Returns {"iv_rank": float, "iv_current": float, "iv_high": float, "iv_low": float}
    or None on failure (IBKR unavailable, symbol has no options, etc.).
    """
    import time as _t
    cached = _iv_rank_cache.get(symbol.upper())
    if cached and _t.time() - cached["ts"] < _IV_RANK_TTL:
        return cached

    try:
        ib       = _get_ib()
        contract = _stock_contract(symbol.upper())
        ib.qualifyContracts(contract)
        bars = ib.reqHistoricalData(
            contract,
            endDateTime    = "",
            durationStr    = "1 Y",
            barSizeSetting = "1 day",
            whatToShow     = "OPTION_IMPLIED_VOLATILITY",
            useRTH         = True,
            formatDate     = 1,
        )
        if not bars or len(bars) < 30:
            return None

        ivs = [float(b.close) for b in bars if b.close and float(b.close) > 0]
        if len(ivs) < 30:
            return None

        iv_cur  = ivs[-1]
        iv_high = max(ivs)
        iv_low  = min(ivs)
        iv_rank = round((iv_cur - iv_low) / (iv_high - iv_low) * 100, 1) \
                  if iv_high > iv_low else 50.0

        result = {
            "iv_rank":    iv_rank,
            "iv_current": round(iv_cur * 100, 1),   # as percentage (e.g. 32.5%)
            "iv_high":    round(iv_high * 100, 1),
            "iv_low":     round(iv_low * 100, 1),
            "ts":         _t.time(),
        }
        _iv_rank_cache[symbol.upper()] = result
        return result
    except Exception:
        return None


def get_daily_bars(symbol: str, duration: str = "1 Y") -> list[dict]:
    """Daily OHLCV. IBKR primary, Yahoo fallback. duration: '1 Y', '60 D', '2 Y'."""
    try:
        bars = _ib_daily_bars(symbol, duration)
        if bars:
            return bars
    except Exception:
        pass
    return _yahoo_daily_bars(symbol, duration)


def get_hourly_closes(symbol: str, days: int = 14) -> list[float]:
    """Hourly closes for RSI_1h. IBKR primary, Yahoo fallback."""
    try:
        closes = _ib_hourly_closes(symbol, days)
        if closes:
            return closes
    except Exception:
        pass
    return _yahoo_hourly_closes(symbol, days)


# Yahoo interval string → IBKR bar size string
_INTERVAL_TO_IBKR = {
    "1m":  "1 min", "2m": "2 mins", "5m": "5 mins",
    "15m": "15 mins", "30m": "30 mins", "1h": "1 hour",
}
# Yahoo period string → IBKR duration string
_PERIOD_TO_IBKR = {
    "1d": "1 D", "2d": "2 D", "5d": "5 D", "1wk": "5 D",
    "10d": "10 D", "30d": "30 D",
    "60d":  "60 D",
    "180d": "6 M",
    "730d": "2 Y",
}

# Yahoo period → Yahoo range.  Yahoo caps intraday at 60 days; map longer requests.
_PERIOD_TO_RANGE = {
    "1d": "1d", "2d": "2d", "5d": "5d", "1wk": "5d",
    "10d": "60d", "30d": "60d",
    "60d":  "60d",
    "180d": "60d",
    "730d": "60d",
}


def get_intraday_bars(symbol: str, interval: str = "5m",
                      period: str = "1d",
                      prefer_yahoo: bool = False) -> list[tuple]:
    """
    Intraday bars as (ts_epoch, open, high, low, close, volume) tuples.
    prefer_yahoo=True  → skip IBKR entirely (bulk scan path, avoids pacing limits)
    prefer_yahoo=False → IBKR primary, Yahoo fallback (critical path: regime, open trades)

    For long-period 15-min requests (730d / 180d) we use chunked IBKR fetching
    because IBKR caps a single 15-min request at 60 calendar days.
    Each chunk adds 60 days; 10 chunks ≈ 2 years.
    """
    if prefer_yahoo:
        return _yahoo_intraday_bars(symbol, interval, period)

    ib_bar = _INTERVAL_TO_IBKR.get(interval, "5 mins")
    ib_dur = _PERIOD_TO_IBKR.get(period, "1 D")

    # Chunked path: long 15-min / 5-min requests that exceed IBKR's 60-day cap
    _LONG_PERIODS = {"730d", "365d", "180d"}
    _CHUNK_BARS   = {"15m", "5m"}
    if period in _LONG_PERIODS and interval in _CHUNK_BARS:
        n_chunks = 10 if period == "730d" else 5
        try:
            bars = _ib_intraday_bars_chunked(symbol, ib_bar, n_chunks=n_chunks)
            if bars:
                return bars
        except Exception:
            pass
        return _yahoo_intraday_bars(symbol, interval, period)

    # Standard single-request path
    try:
        bars = _ib_intraday_bars(symbol, ib_bar, ib_dur)
        if bars:
            return bars
    except Exception:
        pass
    return _yahoo_intraday_bars(symbol, interval, period)


def get_extended_hours_bars(symbol: str, period: str = "5d",
                            interval: str = "5m") -> list[dict]:
    """
    Return intraday bars INCLUDING pre-market (4am-9:30am ET) and
    after-hours (4pm-8pm ET) for the requested period.

    Each bar is a dict: {t, o, h, l, c, v, rth}
      rth=True  → regular trading hours (09:30–16:00 ET)
      rth=False → extended hours (pre-market or after-hours)

    interval: "1m" or "5m" (default "5m")
    Used exclusively by the chart endpoint — NOT for the scoring engine
    (which needs RTH-only bars for accurate VWAP / session indicators).
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")

    _IBKR_BAR = {"1m": "1 min", "5m": "5 mins", "15m": "15 mins"}
    ibkr_bar  = _IBKR_BAR.get(interval, "5 mins")
    yahoo_ivl = interval  # Yahoo uses "1m", "5m" directly

    raw = []

    # ── IBKR: useRTH=False gives all sessions ─────────────────────────────────
    try:
        ib  = _get_ib()
        contract, what = _contract_info(symbol.upper())
        ib.qualifyContracts(contract)
        ib_dur = _PERIOD_TO_IBKR.get(period, "5 D")
        bars = ib.reqHistoricalData(
            contract, endDateTime="",
            durationStr=ib_dur, barSizeSetting=ibkr_bar,
            whatToShow=what, useRTH=False, formatDate=2,
        )
        for b in bars:
            if None in (b.open, b.high, b.low, b.close):
                continue
            ts  = int(b.date) if isinstance(b.date, (int, float)) else int(b.date.timestamp())
            bdt = datetime.fromtimestamp(ts, tz=_ET)
            rth = (bdt.hour > 9 or (bdt.hour == 9 and bdt.minute >= 30)) and bdt.hour < 16
            raw.append({"t": ts, "o": round(float(b.open), 4), "h": round(float(b.high), 4),
                        "l": round(float(b.low), 4),  "c": round(float(b.close), 4),
                        "v": int(b.volume), "rth": rth})
        if raw:
            return raw
    except Exception:
        pass

    # ── Yahoo fallback: includePrePost=true ───────────────────────────────────
    try:
        import requests
        rng = _PERIOD_TO_RANGE.get(period, period)
        url = (f"{_YAHOO_CHART}/{symbol.upper()}"
               f"?interval={yahoo_ivl}&range={rng}&includePrePost=true")
        r = requests.get(url, timeout=9, headers=_HEADERS)
        result = (r.json().get("chart", {}).get("result") or [None])[0]
        if result:
            qb  = (result.get("indicators", {}).get("quote") or [{}])[0]
            ts_list = result.get("timestamp", [])
            ops = qb.get("open", [])
            his = qb.get("high", [])
            los = qb.get("low",  [])
            cls = qb.get("close",[])
            vls = qb.get("volume",[])
            for t, o, h, l, c, v in zip(ts_list, ops, his, los, cls, vls):
                if None in (t, o, h, l, c):
                    continue
                bdt = datetime.fromtimestamp(t, tz=_ET)
                rth = (bdt.hour > 9 or (bdt.hour == 9 and bdt.minute >= 30)) and bdt.hour < 16
                raw.append({"t": t, "o": round(o, 4), "h": round(h, 4),
                            "l": round(l, 4), "c": round(c, 4),
                            "v": int(v) if v else 0, "rth": rth})
    except Exception:
        pass

    return raw


def get_index_tickers(index: str) -> list[str]:
    """
    Return constituent tickers for a major index.
    index: "sp500" | "nasdaq100" | "dj30" | "cream"

    S&P 500: fetched live from Wikipedia (stable, no API key needed).
    NASDAQ-100 and DJ30: hardcoded (small, rarely change).
    cream: top symbols by OOS Sharpe from data/cream_universe.json (generated by index_sweep.sh).
    """
    if index == "cream":
        import json as _json, os as _os
        cream_file = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data", "cream_universe.json")
        try:
            with open(cream_file) as _f:
                d = _json.load(_f)
            # Accept top_N key for any N (top_30, top_25, etc.)
            for key in sorted(d.keys(), reverse=True):
                if key.startswith("top_"):
                    syms = d[key]
                    if isinstance(syms, list) and syms:
                        return syms
            # Fallback: take ranked symbols with n>=5 and sharpe>=2
            ranked = d.get("ranked", [])
            return [r["symbol"] for r in ranked if r.get("n", 0) >= 5 and r.get("sharpe", 0) >= 2.0]
        except FileNotFoundError:
            return []
    if index == "dj30":
        return [
            "AAPL","AMGN","AXP","BA","CAT","CRM","CSCO","CVX","DIS","DOW",
            "GS","HD","HON","IBM","INTC","JNJ","JPM","KO","MCD","MMM",
            "MRK","MSFT","NKE","PG","TRV","UNH","V","VZ","WBA","WMT",
        ]
    if index == "nasdaq100":
        return [
            "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","AVGO","COST",
            "NFLX","AMD","ADBE","QCOM","INTC","CSCO","TXN","INTU","AMGN","SBUX",
            "AMAT","MU","ISRG","LRCX","KLAC","ADI","MRVL","REGN","PANW","CRWD",
            "SNPS","CDNS","ASML","MCHP","ABNB","FTNT","MNST","ORLY","PCAR","PAYX",
            "KDP","DXCM","CSGP","BIIB","IDXX","EXC","GEHC","ODFL","FAST","VRSK",
            "ON","GFS","ILMN","ZS","WDAY","TEAM","SPLK","ANSS","DLTR","SGEN",
            "FANG","CEG","XEL","CTSH","MTCH","VRSN","CPRT","GILD","WBD","EA",
            "SIRI","LULU","AEP","HON","TTD","EBAY","ALGN","TTWO","MDB","DDOG",
            "NET","SNOW","ROKU","DOCU","ZM","PTON","LYFT","UBER","DASH","RBLX",
            "HOOD","COIN","PLTR","IONQ","SMCI","ARM","MSTR","ASTS","RDDT","APP",
        ]
    if index == "sp500":
        try:
            import requests, re
            r = requests.get(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                timeout=10, headers=_HEADERS,
            )
            # Parse the first table's Symbol column
            tickers = re.findall(r'<td><a[^>]*>([A-Z]{1,5}(?:\.[A-Z])?)</a></td>', r.text)
            # Wikipedia uses . for BRK.B etc; yfinance uses -
            tickers = [t.replace(".", "-") for t in tickers]
            if len(tickers) >= 400:
                return tickers
        except Exception:
            pass
        # Hardcoded fallback — large-cap core (top ~200 by weight)
        return [
            "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AVGO","BRK-B","JPM",
            "LLY","UNH","V","XOM","MA","COST","HD","PG","JNJ","ORCL","BAC","ABBV",
            "KO","MRK","CVX","WMT","CRM","NFLX","AMD","ACN","ABT","QCOM","TXN",
            "TMO","PEP","PM","LIN","DHR","ISRG","CSCO","GE","IBM","NOW","INTC",
            "DIS","GS","BLK","SPGI","MU","SYK","REGN","ADP","VRTX","AMAT","ADI",
            "BKNG","KLAC","MS","C","AMGN","TJX","MMC","DE","PANW","LRCX","BSX",
            "CB","PLD","FI","ZTS","MO","COP","MDLZ","ICE","SO","SCHW","CMG","AON",
            "HUM","CI","WM","MCK","CTAS","ITW","SHW","PNC","USB","TDG","EOG","MSI",
            "WCN","NSC","APH","ECL","WELL","AJG","OTIS","CMI","FTNT","CRWD","NET",
            "SNOW","DDOG","ZS","WDAY","TEAM","UBER","ABNB","DASH","RBLX","COIN",
        ]
    return []


def get_premarket_gap(symbol: str) -> dict:
    """
    Pre-market catalyst data for a symbol.

    Returns:
      gap_pct     : (premarket_last - prev_close) / prev_close × 100
      pm_volume   : total pre-market share volume today
      avg_vol_20d : 20-day average daily volume
      vol_ratio   : pm_volume relative to expected pre-market volume
                    (expected ≈ avg_vol_20d × 0.04, i.e. ~4% of daily happens pre-market)
      pm_price    : last pre-market trade price
      prev_close  : previous RTH close

    Works pre-open AND during the session (uses useRTH=False to capture extended hours).
    Returns {} on any error — caller should treat missing data as no catalyst.
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")

    try:
        ib = _get_ib()
        contract, what = _contract_info(symbol.upper())
        ib.qualifyContracts(contract)

        # 21 daily RTH bars → yesterday's close + 20-day avg volume
        daily = ib.reqHistoricalData(
            contract,
            endDateTime    = "",
            durationStr    = "30 D",
            barSizeSetting = "1 day",
            whatToShow     = what,
            useRTH         = True,
            formatDate     = 1,
        )
        if not daily or len(daily) < 2:
            return {}
        # If market is already open, daily[-1] is today's partial bar — use daily[-2].
        # Before the open, daily[-1] is yesterday's complete bar (correct).
        from datetime import date as _date
        today_str = _date.today().strftime("%Y%m%d")
        last_bar_date = str(daily[-1].date)[:8]
        prev_bar = daily[-2] if last_bar_date == today_str else daily[-1]
        prev_close  = float(prev_bar.close)
        avg_vol_20d = sum(int(b.volume) for b in daily[-20:]) / min(20, len(daily))

        # Today's extended-hours 5-min bars (pre-market + RTH so far)
        ext_bars = ib.reqHistoricalData(
            contract,
            endDateTime    = "",
            durationStr    = "1 D",
            barSizeSetting = "5 mins",
            whatToShow     = what,
            useRTH         = False,
            formatDate     = 2,
        )
        if not ext_bars:
            return {}

        # Keep only pre-market bars (before 9:30 ET)
        pm_bars = []
        for b in ext_bars:
            ts_val = int(b.date) if isinstance(b.date, (int, float)) else int(b.date.timestamp())
            bar_dt = datetime.fromtimestamp(ts_val, tz=_ET)
            if bar_dt.hour < 9 or (bar_dt.hour == 9 and bar_dt.minute < 30):
                pm_bars.append(b)

        # Previous day high/low — from yesterday's RTH bar
        prev_day_high = round(float(daily[-1].high), 4)
        prev_day_low  = round(float(daily[-1].low),  4)

        if not pm_bars:
            return {"gap_pct": 0.0, "pm_volume": 0,
                    "avg_vol_20d": int(avg_vol_20d), "vol_ratio": 0.0,
                    "pm_price": prev_close, "prev_close": prev_close,
                    "prev_day_high": prev_day_high, "prev_day_low": prev_day_low,
                    "pm_high": prev_close, "pm_low": prev_close}

        pm_price  = float(pm_bars[-1].close)
        pm_volume = sum(int(b.volume) for b in pm_bars if b.volume and b.volume > 0)
        pm_high   = round(max(float(b.high) for b in pm_bars), 4)
        pm_low    = round(min(float(b.low)  for b in pm_bars), 4)
        gap_pct   = (pm_price - prev_close) / prev_close * 100 if prev_close else 0.0
        expected_pm = avg_vol_20d * 0.04
        vol_ratio   = pm_volume / expected_pm if expected_pm > 0 else 0.0

        return {
            "gap_pct":       round(gap_pct, 2),
            "pm_volume":     pm_volume,
            "avg_vol_20d":   int(avg_vol_20d),
            "vol_ratio":     round(vol_ratio, 2),
            "pm_price":      round(pm_price, 4),
            "prev_close":    round(prev_close, 4),
            "prev_day_high": prev_day_high,
            "prev_day_low":  prev_day_low,
            "pm_high":       pm_high,
            "pm_low":        pm_low,
        }
    except Exception:
        pass

    # ── Yahoo Finance fallback ────────────────────────────────────────────────
    try:
        import yfinance as yf
        tk   = yf.Ticker(symbol.upper())
        info = tk.fast_info

        prev_close  = float(info.previous_close or 0)
        pm_price    = float(info.last_price or prev_close)
        avg_vol_20d = float(info.three_month_average_volume or info.regular_market_volume or 1)

        if not prev_close:
            return {}

        gap_pct = (pm_price - prev_close) / prev_close * 100

        # Pre-market volume from intraday history with prepost=True
        hist = tk.history(period="1d", interval="5m", prepost=True)
        pm_volume = 0
        pm_high   = pm_price
        pm_low    = pm_price
        if not hist.empty:
            try:
                from zoneinfo import ZoneInfo
            except ImportError:
                from backports.zoneinfo import ZoneInfo
            _ET2 = ZoneInfo("America/New_York")
            for ts, row in hist.iterrows():
                dt = ts.to_pydatetime().astimezone(_ET2)
                if dt.hour < 9 or (dt.hour == 9 and dt.minute < 30):
                    v = row.get("Volume", 0)
                    try:
                        v = int(v) if v == v else 0  # guard against NaN
                    except (TypeError, ValueError):
                        v = 0
                    pm_volume += v
                    h = float(row.get("High", pm_price) or pm_price)
                    l = float(row.get("Low",  pm_price) or pm_price)
                    pm_high = max(pm_high, h)
                    pm_low  = min(pm_low,  l)

        expected_pm = avg_vol_20d * 0.04
        vol_ratio   = pm_volume / expected_pm if expected_pm > 0 else 0.0

        return {
            "gap_pct":       round(gap_pct, 2),
            "pm_volume":     pm_volume,
            "avg_vol_20d":   int(avg_vol_20d),
            "vol_ratio":     round(vol_ratio, 2),
            "pm_price":      round(pm_price, 4),
            "prev_close":    round(prev_close, 4),
            "pm_high":       round(pm_high, 4),
            "pm_low":        round(pm_low,  4),
        }
    except Exception:
        return {}


def get_daily_bias(symbol: str) -> dict:
    """
    Daily trend bias for a symbol using 20-day EMA on daily closes.

    Returns:
      bias  : "bull" (price > EMA20 by >1%), "bear" (price < EMA20 by >1%), "neutral"
      ema20 : 20-day EMA value
      price : latest daily close

    Used in evaluate_symbol() to penalise CALL signals on downtrending stocks.
    Data comes from Yahoo Finance daily bars (no IBKR quota used).
    """
    try:
        import yfinance as yf
        hist = yf.download(symbol, period="45d", interval="1d",
                           progress=False, auto_adjust=True)
        if hist.empty or len(hist) < 20:
            return {"bias": "neutral", "ema20": None, "price": None}

        closes = [float(c) for c in hist["Close"].values]
        # EMA-20: seed with SMA of first 20 bars, then roll forward
        k   = 2 / 21
        ema = sum(closes[:20]) / 20
        for c in closes[20:]:
            ema = c * k + ema * (1 - k)

        current = closes[-1]
        if current > ema * 1.01:
            bias = "bull"
        elif current < ema * 0.99:
            bias = "bear"
        else:
            bias = "neutral"

        return {"bias": bias, "ema20": round(ema, 2), "price": round(current, 2)}
    except Exception:
        return {"bias": "neutral", "ema20": None, "price": None}


def get_top_gainers(n: int = 25) -> list[str]:
    """
    Return up to n US equity symbols that are top % gainers today via IBKR scanner.
    Filters: price > $10, volume > 1M. Falls back to [] on any error.
    Skips after 15:30 ET — IBKR cancels scanner subscriptions near close.
    """
    from ib_insync import ScannerSubscription
    # IBKR cancels scanner subs after ~15:30; don't bother calling
    now_et = datetime.now(tz=__import__("zoneinfo", fromlist=["ZoneInfo"]).ZoneInfo("America/New_York"))
    if now_et.hour == 15 and now_et.minute >= 30:
        return []
    try:
        ib = _get_ib()
        sub = ScannerSubscription(
            instrument   = "STK",
            locationCode = "STK.US.MAJOR",
            scanCode     = "TOP_PERC_GAIN",
            abovePrice   = 10.0,
            aboveVolume  = 1000000,
        )
        results = ib.reqScannerData(sub)
        # Filter to pure USD symbols only — STK.US.MAJOR can return cross-listed
        # Canadian names (e.g. XIU) whose primary exchange resolves to TSX/CAD.
        return [
            r.contractDetails.contract.symbol
            for r in results[:n]
            if r.contractDetails.contract.currency == "USD"
        ]
    except Exception:
        return []


def get_l2_imbalance(symbol: str) -> "float | None":
    """
    Top-of-book bid/(bid+ask) size ratio from a market-data snapshot.
    >0.6 = bid-heavy (buy pressure), <0.4 = ask-heavy (sell pressure).
    Returns None if data unavailable or sizes are zero.
    """
    try:
        ib = _get_ib()
        contract, _ = _contract_info(symbol.upper())
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(1.5)
        bid_sz = float(ticker.bidSize or 0)
        ask_sz = float(ticker.askSize or 0)
        ib.cancelMktData(contract)
        if bid_sz + ask_sz <= 0:
            return None
        return round(bid_sz / (bid_sz + ask_sz), 3)
    except Exception:
        return None


def get_options_contract(ticker: str, direction: str = "CALL",
                         dte_min: int = 30) -> "dict | None":
    """
    Best liquid options contract. IBKR primary, yfinance fallback.
    Output keys: strike, mid, bid, ask, spread_pct, iv, volume,
                 openInterest, inTheMoney, expiry, dte.
    """
    from datetime import date as _date
    today = _date.today()

    def _dte(exp_str):
        try:
            return (_date.fromisoformat(exp_str) - today).days
        except Exception:
            return 9999

    def _pick_best(rows, spot=None):
        liquid = [r for r in rows if (r.get("spread_pct") or 999) < 15]
        pool   = liquid or rows
        otm    = [r for r in pool if not r.get("inTheMoney")]
        pool   = otm if otm else pool
        if spot:
            pool.sort(key=lambda r: (abs(r["strike"] - spot), -(r.get("openInterest") or 0)))
        else:
            pool.sort(key=lambda r: -(r.get("openInterest") or 0))
        return pool[0] if pool else None

    # ── IBKR path ──
    try:
        from ib_insync import Option
        ib     = _get_ib()
        right  = direction[0].upper()
        stock  = _stock_contract(ticker)
        ib.qualifyContracts(stock)
        chains = ib.reqSecDefOptParams(ticker, "", "STK", stock.conId)
        chain  = next((c for c in chains if c.exchange == "SMART"), None)
        if chain:
            valid_expiries = [
                e for e in sorted(chain.expirations)
                if _dte(f"{e[:4]}-{e[4:6]}-{e[6:]}") >= dte_min
            ]
            if valid_expiries:
                expiry     = valid_expiries[0]
                expiry_iso = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
                actual_dte = _dte(expiry_iso)

                tkr  = ib.reqMktData(stock, "", False, False)
                ib.sleep(1)
                spot = tkr.last or tkr.close or 0

                strikes = sorted(chain.strikes)
                if spot:
                    strikes = sorted(strikes, key=lambda s: abs(s - spot))[:16]
                    strikes = sorted(strikes)

                contracts = [Option(ticker, expiry, s, right, "SMART") for s in strikes]
                ib.qualifyContracts(*contracts)
                tickers = ib.reqTickers(*contracts)
                ib.sleep(2)

                rows = []
                for t in tickers:
                    c   = t.contract
                    bid = t.bid or 0
                    ask = t.ask or 0
                    mid = round((bid + ask) / 2, 2) if bid + ask > 0 else (t.last or 0)
                    sp  = round((ask - bid) / mid * 100, 1) if mid > 0 else 999
                    iv  = round(t.impliedVolatility * 100, 1) if t.impliedVolatility else 0
                    itm = (c.strike < spot if right == "C" else c.strike > spot) if spot else False
                    rows.append({
                        "strike": c.strike, "mid": mid,
                        "bid": round(bid, 2), "ask": round(ask, 2),
                        "spread_pct": sp, "iv": iv,
                        "volume": int(t.volume or 0),
                        "openInterest": int(
                            t.callOpenInterest if right == "C" else t.putOpenInterest or 0),
                        "inTheMoney": itm,
                        "expiry": expiry_iso, "dte": actual_dte,
                    })
                best = _pick_best(rows, spot)
                if best:
                    return best
    except Exception:
        pass

    # ── yfinance fallback ──
    try:
        import yfinance as yf
        t        = yf.Ticker(ticker)
        expiries = t.options
        if not expiries:
            return None
        valid  = [e for e in expiries if _dte(e) >= dte_min]
        if not valid:
            return None
        expiry     = min(valid, key=_dte)
        actual_dte = _dte(expiry)
        chain      = t.option_chain(expiry)
        df         = chain.calls if direction == "CALL" else chain.puts
        price      = getattr(t.fast_info, "last_price", None)
        rows = []
        for _, row in df.iterrows():
            bid  = float(row.get("bid") or 0)
            ask  = float(row.get("ask") or 0)
            last = float(row.get("lastPrice") or 0)
            mid  = (bid + ask) / 2 if bid + ask > 0 else last
            sp   = round((ask - bid) / mid * 100, 1) if mid > 0 else 999
            rows.append({
                "strike":       float(row.get("strike", 0)),
                "mid":          round(mid, 2),
                "bid":          round(bid, 2),
                "ask":          round(ask, 2),
                "spread_pct":   sp,
                "iv":           round(float(row.get("impliedVolatility") or 0) * 100, 1),
                "volume":       int(row.get("volume") or 0),
                "openInterest": int(row.get("openInterest") or 0),
                "inTheMoney":   bool(row.get("inTheMoney", False)),
                "expiry":       expiry,
                "dte":          actual_dte,
            })
        return _pick_best(rows, price)
    except Exception:
        return None


def get_options_chain(ticker: str, target_dte: int = 1) -> "dict | None":
    """
    Full options chain (calls + puts, all near-ATM strikes) for the expiry
    closest to target_dte. Returns the same structure as _options_polygon so
    GapFade can use it identically.
    """
    from datetime import date as _date
    today = _date.today()

    def _dte(exp_str):
        try:    return (_date.fromisoformat(exp_str) - today).days
        except: return 9999

    def _iso(e):   # IBKR format YYYYMMDD → ISO
        return f"{e[:4]}-{e[4:6]}-{e[6:]}"

    try:
        from ib_insync import Option
        ib    = _get_ib()
        stock = _stock_contract(ticker)
        ib.qualifyContracts(stock)
        chains = ib.reqSecDefOptParams(ticker, "", "STK", stock.conId)
        chain  = next((c for c in chains if c.exchange == "SMART"), None)
        if not chain:
            return None

        # All expiries ≥1 DTE, pick closest to target_dte
        valid = [e for e in sorted(chain.expirations) if _dte(_iso(e)) >= 1]
        if not valid:
            return None
        expiry     = min(valid, key=lambda e: abs(_dte(_iso(e)) - target_dte))
        expiry_iso = _iso(expiry)
        actual_dte = _dte(expiry_iso)

        # Get spot price
        tkr  = ib.reqMktData(stock, "", False, False)
        ib.sleep(1)
        spot = tkr.last or tkr.close or 0

        # Up to 24 strikes around spot
        strikes = sorted(chain.strikes)
        if spot:
            strikes = sorted(strikes, key=lambda s: abs(s - spot))[:24]
            strikes = sorted(strikes)

        call_contracts = [Option(ticker, expiry, s, "C", "SMART") for s in strikes]
        put_contracts  = [Option(ticker, expiry, s, "P", "SMART") for s in strikes]
        all_contracts  = call_contracts + put_contracts
        ib.qualifyContracts(*all_contracts)
        tickers = ib.reqTickers(*all_contracts)
        ib.sleep(2)

        calls, puts = [], []
        for tk in tickers:
            c   = tk.contract
            bid = tk.bid or 0
            ask = tk.ask or 0
            mid = round((bid + ask) / 2, 2) if bid + ask > 0 else (tk.last or 0)
            sp  = round((ask - bid) / mid * 100, 1) if mid > 0 else None
            iv  = round(tk.impliedVolatility * 100, 1) if tk.impliedVolatility else 0
            itm = (c.strike < spot if c.right == "C" else c.strike > spot) if spot else False
            oi  = int(tk.callOpenInterest if c.right == "C" else tk.putOpenInterest or 0)
            row = {
                "strike":       c.strike,
                "lastPrice":    round(tk.last or 0, 2),
                "bid":          round(bid, 2),
                "ask":          round(ask, 2),
                "mid":          round(mid, 2),
                "spread_pct":   sp,
                "iv":           iv,
                "volume":       int(tk.volume or 0),
                "openInterest": oi,
                "inTheMoney":   itm,
            }
            (calls if c.right == "C" else puts).append(row)

        if not calls and not puts:
            return None

        return {
            "ticker":       ticker,
            "expiry":       expiry_iso,
            "dte":          actual_dte,
            "calls":        sorted(calls, key=lambda x: x["strike"]),
            "puts":         sorted(puts,  key=lambda x: x["strike"]),
            "all_expiries": [_iso(e) for e in valid[:8]],
            "source":       "ibkr",
        }
    except Exception:
        return None


# ── Order execution (paper trading) ──────────────────────────────────────────
PAPER_HOST = os.environ.get("PAPER_HOST", "172.18.176.1")
PAPER_PORT = int(os.environ.get("PAPER_PORT", "4002"))
PAPER_CLIENT_ID = 47  # dedicated, never overlaps with data pool (43/44) or MCP (42)


def _paper_ib():
    """Return a fresh connected IB() on the paper account."""
    import asyncio
    from ib_insync import IB
    if PAPER_PORT == TWS_PORT:
        raise RuntimeError("Paper port == live port. Refusing to connect.")
    # ib_insync needs an asyncio event loop. Background threads (e.g. _run_intraday_scan)
    # don't have one by default, so create one if missing.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    ib = IB()
    ib.connect(PAPER_HOST, PAPER_PORT, clientId=PAPER_CLIENT_ID, timeout=10)
    return ib


def place_options_order(symbol: str, direction: str, qty: int = 1,
                        dte_min: int = 1, spot: float = 0.0,
                        max_cost: float = 250.0) -> "dict | None":
    """
    Find an affordable OTM options contract and place market BUY on paper account.
    Walks OTM from the first strike below/above spot until the Yahoo mid-price
    fits within max_cost (default $250 per contract). Skips Canadian symbols.
    """
    from datetime import date as _date
    sym = symbol.upper()
    if sym.endswith((".TO", ".V", ".CN")):
        return None

    right = "C" if direction.upper() == "CALL" else "P"
    max_premium = max_cost / 100.0  # per-share threshold

    from ib_insync import Option, MarketOrder, Stock
    ib = _paper_ib()
    try:
        stock = Stock(sym, "SMART", "USD")
        ib.qualifyContracts(stock)

        # Always fetch live price at order time — signal entry price may be stale
        try:
            import requests as _req
            _r = _req.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                params={"interval": "1d", "range": "1d"},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=5
            ).json()
            live_spot = _r["chart"]["result"][0]["meta"].get("regularMarketPrice", 0)
            if live_spot:
                spot = live_spot
        except Exception:
            pass
        if not spot:
            return None

        # Get options chain definitions (strikes/expiries only — no prices)
        chains = ib.reqSecDefOptParams(sym, "", "STK", stock.conId)
        chain  = next((c for c in chains if c.exchange == "SMART"), None)
        if not chain:
            return None

        today = _date.today()
        valid_expiries = sorted([
            e for e in chain.expirations
            if (_date.fromisoformat(f"{e[:4]}-{e[4:6]}-{e[6:]}") - today).days >= dte_min
        ])
        if not valid_expiries:
            return None

        expiry_ibkr = valid_expiries[0]
        expiry_iso  = f"{expiry_ibkr[:4]}-{expiry_ibkr[4:6]}-{expiry_ibkr[6:]}"
        dte         = (_date.fromisoformat(expiry_iso) - today).days
        expiry_yf   = expiry_iso  # Yahoo uses YYYY-MM-DD

        # Fetch Yahoo options chain for mid-price checks (no IB market data sub needed)
        yahoo_prices: "dict[float, float]" = {}  # strike → mid price
        try:
            import yfinance as _yf
            _ticker = _yf.Ticker(sym)
            _chain  = _ticker.option_chain(expiry_yf)
            _df     = _chain.puts if right == "P" else _chain.calls
            for _, row in _df.iterrows():
                bid, ask = row.get("bid", 0), row.get("ask", 0)
                mid = (bid + ask) / 2 if bid and ask else row.get("lastPrice", 0)
                yahoo_prices[float(row["strike"])] = mid
        except Exception:
            pass  # no prices — will skip affordability check and use first OTM

        # Build OTM strike list ordered from least OTM → most OTM
        if right == "P":
            candidates = sorted([s for s in chain.strikes if s < spot], reverse=True)
        else:
            candidates = sorted([s for s in chain.strikes if s > spot])

        if not candidates:
            return None

        # Walk OTM until we find a strike within budget (or use first OTM if no prices)
        strike = candidates[0]
        est_price = None
        for s in candidates:
            mid = yahoo_prices.get(s)
            if mid is None:
                strike = s
                break
            if mid <= max_premium:
                strike = s
                est_price = round(mid * 100, 2)
                break
        else:
            # Nothing fits budget — skip rather than overspend
            print(f"  ⚠️  {sym}: no contract under ${max_cost:.0f} found — skipping order")
            return None

        # Qualify and place
        opt_contract = Option(sym, expiry_ibkr, strike, right, "SMART")
        qualified = ib.qualifyContracts(opt_contract)
        if not qualified:
            return None

        order_id = ib.client.getReqId()
        order = MarketOrder("BUY", qty)
        order.orderId = order_id
        order.tif     = "DAY"
        ib.placeOrder(opt_contract, order)
        ib.sleep(1)

        return {
            "con_id":    opt_contract.conId,
            "symbol":    sym,
            "direction": direction.upper(),
            "right":     right,
            "strike":    strike,
            "expiry":    expiry_iso,
            "dte":       dte,
            "qty":       qty,
            "est_price": est_price,
            "order_id":  order_id,
            "status":    "submitted",
        }
    finally:
        ib.disconnect()


def close_options_position(con_id: int, symbol: str, right: str,
                           strike: float, expiry: str, qty: int) -> bool:
    """
    Close an options position on the paper account via market SELL.
    Called when the underlying hits target or stop.
    Returns True if sell order placed, False if no position found.
    """
    from ib_insync import Option, MarketOrder
    expiry_ibkr = expiry.replace("-", "")
    opt_contract = Option(symbol, expiry_ibkr, strike, right, "SMART")
    if con_id:
        opt_contract.conId = con_id

    ib = _paper_ib()
    try:
        positions = ib.positions()
        pos = next((p for p in positions if p.contract.conId == con_id), None)
        if not pos or abs(pos.position) == 0:
            return False

        actual_qty = int(abs(pos.position))
        # Use the position's own contract (has conId populated); set SMART routing.
        close_contract = pos.contract
        close_contract.exchange = "SMART"
        ib.qualifyContracts(close_contract)

        order = MarketOrder("SELL", actual_qty)
        order.tif = "DAY"
        ib.placeOrder(close_contract, order)
        ib.sleep(1)
        return True
    finally:
        ib.disconnect()


def place_bracket_order(symbol: str, shares: int, target: float, stop: float,
                        direction: str = "CALL", entry: float = 0.0) -> dict:
    """
    Place a bracket order on the IBKR paper account (port 4002).

    CALL: market BUY  → limit SELL at target + stop SELL at stop
    PUT:  market SELL → limit BUY  at target + stop BUY  at stop (short)

    Never touches the live data port.
    """
    if PAPER_PORT == TWS_PORT:
        raise RuntimeError(
            f"place_bracket_order refused: PAPER_PORT ({PAPER_PORT}) == TWS_PORT ({TWS_PORT}). "
            "Orders must go to the paper account only."
        )
    from ib_insync import IB, Stock, MarketOrder, LimitOrder, StopOrder

    sym       = symbol.upper()
    currency  = "CAD" if sym.endswith(".TO") else "USD"
    ib_sym    = sym[:-3] if sym.endswith(".TO") else sym
    is_long   = direction.upper() == "CALL"
    entry_act = "BUY"  if is_long else "SELL"
    exit_act  = "SELL" if is_long else "BUY"

    contract = Stock(ib_sym, "SMART", currency)

    ib = IB()
    try:
        ib.connect(PAPER_HOST, PAPER_PORT, clientId=PAPER_CLIENT_ID, timeout=10)
        ib.qualifyContracts(contract)

        parent_id = ib.client.getReqId()
        tp_id     = ib.client.getReqId()
        sl_id     = ib.client.getReqId()
        oca_group = f"bracket_{sym}_{parent_id}"

        parent = MarketOrder(entry_act, shares)
        parent.orderId  = parent_id
        parent.tif      = "DAY"
        parent.transmit = False

        take_profit = LimitOrder(exit_act, shares, round(target, 2))
        take_profit.orderId  = tp_id
        take_profit.parentId = parent_id
        take_profit.tif      = "GTC"
        take_profit.ocaGroup = oca_group
        take_profit.ocaType  = 1
        take_profit.transmit = False

        stop_loss = StopOrder(exit_act, shares, round(stop, 2))
        stop_loss.orderId  = sl_id
        stop_loss.parentId = parent_id
        stop_loss.tif      = "GTC"
        stop_loss.ocaGroup = oca_group
        stop_loss.ocaType  = 1
        stop_loss.transmit = True

        ib.placeOrder(contract, parent)
        ib.placeOrder(contract, take_profit)
        ib.placeOrder(contract, stop_loss)
        ib.sleep(1)

        result = {
            "status":          "submitted",
            "symbol":          sym,
            "direction":       direction.upper(),
            "shares":          shares,
            "entry":           entry,
            "target":          target,
            "stop":            stop,
            "buy_order_id":    parent_id,
            "target_order_id": tp_id,
            "stop_order_id":   sl_id,
            "oca_group":       oca_group,
        }
        _log_bracket_placed(result)
        return result
    finally:
        ib.disconnect()


def close_stock_position(symbol: str, shares: int) -> dict:
    """
    Flatten a stock position on the IBKR paper account:
    cancel the symbol's open (bracket) orders, then market SELL the shares.
    Used by the swing 21-day timeout — the one exit IBKR's GTC OCA can't do itself.
    """
    if PAPER_PORT == TWS_PORT:
        raise RuntimeError("close_stock_position refused: paper port == live port.")
    from ib_insync import IB, Stock, MarketOrder

    sym      = symbol.upper()
    currency = "CAD" if sym.endswith(".TO") else "USD"
    ib_sym   = sym[:-3] if sym.endswith(".TO") else sym
    contract = Stock(ib_sym, "SMART", currency)

    ib = IB()
    try:
        ib.connect(PAPER_HOST, PAPER_PORT, clientId=PAPER_CLIENT_ID, timeout=10)
        ib.qualifyContracts(contract)

        # Cancel this symbol's open orders first (the OCA bracket children),
        # otherwise the market sell + resting stop could double-sell.
        cancelled = 0
        for trade in ib.openTrades():
            if trade.contract.symbol == ib_sym and trade.orderStatus.status not in (
                    "Filled", "Cancelled", "Inactive"):
                ib.cancelOrder(trade.order)
                cancelled += 1
        if cancelled:
            ib.sleep(1)

        order = MarketOrder("SELL", shares)
        ib.placeOrder(contract, order)
        ib.sleep(1)
        return {"status": "submitted", "symbol": sym, "shares": shares,
                "cancelled_orders": cancelled}
    finally:
        ib.disconnect()


def _bracket_log_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bracket_log.json")


def _log_bracket_placed(r: dict):
    import json
    path = _bracket_log_path()
    try:
        data = json.load(open(path)) if os.path.exists(path) else {"trades": []}
    except Exception:
        data = {"trades": []}

    entry  = r.get("entry") or 0.0
    target = r["target"]
    stop   = r["stop"]
    is_long = r["direction"] == "CALL"

    # Full ATR move as % from entry to target
    atr_target_pct = round((target - entry) / entry * 100, 3) if entry else None
    atr_stop_pct   = round((stop   - entry) / entry * 100, 3) if entry else None

    data["trades"].append({
        "placed_at":    datetime.now().isoformat(timespec="seconds"),
        "symbol":       r["symbol"],
        "direction":    r["direction"],
        "shares":       r["shares"],
        "entry":        entry,
        "target":       target,
        "stop":         stop,
        "atr_target_pct": atr_target_pct,
        "atr_stop_pct":   atr_stop_pct,
        "buy_order_id":    r["buy_order_id"],
        "target_order_id": r["target_order_id"],
        "stop_order_id":   r["stop_order_id"],
        "oca_group":    r["oca_group"],
        "status":       "open",
        "exit_price":   None,
        "exit_at":      None,
        "pnl_pct":      None,
        "outcome":      None,
        "captured_pct_of_full_move": None,
    })
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def update_bracket_log():
    """
    Connect to paper account, check executions, and close out any open
    bracket_log entries. Call this periodically (e.g. from a daily script).
    """
    import json
    path = _bracket_log_path()
    if not os.path.exists(path):
        return

    data = json.load(open(path))
    open_trades = [t for t in data["trades"] if t["status"] == "open"]
    if not open_trades:
        return

    from ib_insync import IB
    ib = IB()
    try:
        ib.connect(PAPER_HOST, PAPER_PORT, clientId=PAPER_CLIENT_ID, timeout=10)
        ib.reqExecutions()
        ib.sleep(2)
        execs = ib.executions()

        # Build map: orderId → fill price
        fill_map = {}
        for e in execs:
            fill_map[e.execution.orderId] = {
                "price": e.execution.price,
                "time":  e.execution.time,
            }

        changed = False
        for t in open_trades:
            tp_fill = fill_map.get(t["target_order_id"])
            sl_fill = fill_map.get(t["stop_order_id"])
            fill = tp_fill or sl_fill
            if not fill:
                continue

            exit_price = fill["price"]
            outcome    = "target_hit" if tp_fill else "stop_hit"
            entry      = t.get("entry") or 0.0
            is_long    = t["direction"] == "CALL"

            if entry:
                pnl_pct = round((exit_price - entry) / entry * 100 * (1 if is_long else -1), 3)
                full_move = abs(t.get("atr_target_pct") or 0)
                captured  = round(abs(pnl_pct) / full_move * 100, 1) if full_move else None
            else:
                pnl_pct = None
                captured = None

            t.update({
                "status":    "closed",
                "exit_price": exit_price,
                "exit_at":   str(fill["time"]),
                "pnl_pct":   pnl_pct,
                "outcome":   outcome,
                "captured_pct_of_full_move": captured,
            })
            changed = True
            print(f"  [{t['symbol']} {t['direction']}] {outcome}  exit ${exit_price}  "
                  f"P&L {pnl_pct:+.2f}%  captured {captured}% of full ATR move")

        if changed:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
    finally:
        ib.disconnect()
