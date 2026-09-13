"""
day_trading.py — Intraday Signal Scanner
=========================================
Combines:
  Pine Script layer  — VWAP, ATR, ADX, session filters, opening drive,
                       liquidity sweeps, exhaustion, early reversals,
                       scoring engine with S/A/B/C/D grades
  Your existing algo — CMF, OBV, MFI, BB compression, smart money
                       confluence, market regime gate, earnings blackout,
                       R:R enforcement (≥1.5), paper trade logging

Primary TF : 5-min bars
HTF        : 15-min bars (confirmation)
Data       : Yahoo Finance intraday (free, no API key)
Refresh    : every 5 min during market hours (9:30–16:00 ET)

Run:
    python3 day_trading.py                     # continuous loop
    python3 day_trading.py --once              # single scan snapshot
    python3 day_trading.py --interval 3        # refresh every 3 min
    python3 day_trading.py --symbols TSLA NVDA # custom symbol list
"""

import sys, os, time, json, random, re, threading, math, logging
from datetime import datetime, timezone, timedelta, date
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_logger = logging.getLogger("day_trading")

# ── Load data/config.env so NTFY_TOPIC etc. are available in standalone mode ──
def _load_config_env():
    _cf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "config.env")
    if not os.path.exists(_cf):
        return
    with open(_cf) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
_load_config_env()

# Use a different clientId range than api.py (43-44) to avoid "client id in use" error
os.environ.setdefault("IBKR_CLIENT_ID", "47")

# ── Reuse shared math + utilities from existing algo ──────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core_signals import (
    _calc_rsi, _calc_ema, _calc_sma,
    _calc_cmf, _calc_obv_trend, _calc_mfi, _calc_bb_compression,
    safe_get, cache_get, cache_set,
    fetch_earnings_date, market_regime,
)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    RICH = True
except ImportError:
    RICH = False

console = Console() if RICH else None

# ═══════════════════════════════════════════════════════════════════════════════
# UNIVERSE
# ═══════════════════════════════════════════════════════════════════════════════

# SPY/QQQ used for regime only; rest are signal candidates
REGIME_SYMS = ["SPY", "QQQ"]


_RAW_SYMBOLS = [
    # Index proxies (intraday liquidity reference)
    "SPY", "QQQ", "IWM",
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO",
    "ORCL", "CRM", "ADBE", "NFLX",
    # Semiconductors
    "AMD", "QCOM", "MU", "AMAT", "KLAC", "TXN", "SMCI",
    # Cloud / cybersecurity
    "SNOW", "DDOG", "WDAY", "ZS", "NET", "CRWD", "PANW", "FTNT",
    # Financials (removed GS — backtest Sharpe -6.7, PF 0.37)
    "JPM", "BAC", "MS", "V", "MA",
    # Energy
    "XOM", "CVX", "OXY", "SLB",
    # Consumer / travel / health (removed ABNB — Sharpe -10.0, PF 0.22)
    "COST", "PEP", "SBUX", "BKNG", "ISRG", "UBER",
]
# De-duplicate while preserving insertion order
DEFAULT_SYMBOLS = list(dict.fromkeys(_RAW_SYMBOLS))

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

SCAN_INTERVAL_MIN = 5       # refresh every N minutes
SIGNAL_THRESHOLD  = 90      # min score (0-100) to fire a signal — Grade S only
                            # Backtest: Grade A (80-89) OOS PF=0.66, Grade S OOS PF=1.21
WATCHING_THRESHOLD = 75     # score 75-89: push "BUILDING UP" alert, don't log as trade
COOLDOWN_MIN      = 40      # minutes between signals for the same symbol
ATR_STOP_MULT     = 2.0     # stop  = entry ± atr × mult
ATR_TARGET_MULT   = 3.0     # target = entry ± atr × mult  → 1.5R baseline
ADX_TREND_MIN     = 20      # ADX floor for trend confirmation
ADX_SIDEWAYS      = 15      # below this = sideways penalty

PAPER_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "day_trades.json")
PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "portfolio.json")

RISK_PCT_PER_TRADE = 0.01    # 1% of account per signal
VIX_HIGH_THRESHOLD = 30      # widen stops above this VIX level
VIX_HIGH_MULT      = 1.25    # multiply ATR multiples by this when VIX is high

# ── Trade management & portfolio risk ────────────────────────────────────────
BREAKEVEN_AT_R     = 1.0     # once trade gains 1R, raise stop to entry (live data:
                             # losers always ran to full -1.37% stop, winners stalled)
DAILY_MAX_STOPS    = 3       # 3 stop-outs in one day = circuit breaker, no new entries
MAX_OPEN_TRADES    = 4       # portfolio heat cap — max concurrent open day trades
MAX_OPEN_PER_SECTOR = 2      # 2026-05-26: NET+SNOW+DDOG opened same day = one bet ×3

# ── Options suggestion (Black-Scholes) ───────────────────────────────────────
ACCOUNT_SIZE      = 100_000.0  # no hard cap — position sized from portfolio.json cash
MAX_OPTION_SPEND  = float("inf")  # no spend cap — show all options regardless of cost

# Shared state updated once per scan cycle by intraday_regime()
_regime_cache = {
    "spy_chg":    0.0,
    "vix":        None,
    "first30_bull": None,   # True/False/None — SPY first-30-min direction
    "first30_ret":  0.0,    # SPY first-30-min return %
    "sector_rets":  {},     # {etf: pct_chg_from_open} updated each cycle
}
_regime_cache_lock = threading.Lock()

# ── Sector ETF membership map ─────────────────────────────────────────────────
# Maps each ticker to its primary sector ETF for relative-strength gating.
_SECTOR_ETF_MAP = {
    # Tech
    **{s: "XLK" for s in ["NVDA","AAPL","AMD","MSFT","INTC","QCOM","MRVL","MU",
                            "SMCI","AMAT","LRCX","ORCL","CRM","ADBE","NOW","SHOP",
                            "PLTR","SOUN","IONQ","BBAI","AI","MARA","RIOT","CLSK"]},
    # Comm services
    **{s: "XLC" for s in ["META","GOOGL","NFLX","SNAP","ROKU"]},
    # Consumer disc
    **{s: "XLY" for s in ["TSLA","AMZN","UBER","ABNB","DASH","LYFT","RBLX",
                            "CHWY","DKNG","PENN","RIVN","LCID"]},
    # Financials
    **{s: "XLF" for s in ["JPM","BAC","GS","MS","V","MA","COIN","SOFI","HOOD",
                            "UPST","AFRM"]},
    # Energy
    **{s: "XLE" for s in ["XOM","CVX","OXY","SLB"]},
    # Healthcare
    **{s: "XLV" for s in ["LLY","MRNA","BIIB","VRTX"]},
}
_SECTOR_ETFS = {"XLK", "XLC", "XLY", "XLF", "XLE", "XLV"}

# ── FOMC / CPI / PPI macro dates (position halving) ──────────────────────────
# FOMC: 8 meetings; CPI: monthly; PPI: day after CPI
_MACRO_DATES = {
    # FOMC 2026
    "2026-01-29", "2026-03-19", "2026-04-30", "2026-06-18",
    "2026-07-30", "2026-09-17", "2026-10-29", "2026-12-10",
    # CPI 2026
    "2026-01-14", "2026-02-11", "2026-03-11", "2026-04-10",
    "2026-05-13", "2026-06-10", "2026-07-09", "2026-08-12",
    "2026-09-09", "2026-10-14", "2026-11-12", "2026-12-11",
    # PPI 2026 (day after CPI)
    "2026-01-15", "2026-02-12", "2026-03-12", "2026-04-11",
    "2026-05-14", "2026-06-11", "2026-07-10", "2026-08-13",
    "2026-09-10", "2026-10-15", "2026-11-13", "2026-12-12",
    # FOMC 2027 (approximate — update when Fed publishes official calendar)
    "2027-01-27", "2027-03-17", "2027-05-05", "2027-06-16",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
    # CPI 2027 (approximate monthly release dates)
    "2027-01-13", "2027-02-10", "2027-03-10", "2027-04-14",
    "2027-05-12", "2027-06-09", "2027-07-14", "2027-08-11",
    "2027-09-08", "2027-10-13", "2027-11-10", "2027-12-08",
    # PPI 2027 (day after CPI)
    "2027-01-14", "2027-02-11", "2027-03-11", "2027-04-15",
    "2027-05-13", "2027-06-10", "2027-07-15", "2027-08-12",
    "2027-09-09", "2027-10-14", "2027-11-11", "2027-12-09",
}

PRE_SCREEN_TOP   = 45   # max symbols passed to full evaluation after pre-screen
PRE_SCREEN_MIN_RV = 0.6  # symbols with rel-vol below this are skipped

# ── Catalyst gate ─────────────────────────────────────────────────────────────
# Pre-market gap + volume scores are cached once per day per symbol.
# Catalyst bonus is added to bull AND bear scores before threshold check.
# With threshold=80: catalyst stock needs 68 raw (Grade B); no-catalyst needs 80 (Grade A).
CATALYST_BONUS    = 12   # pts added when gap + volume confirm a catalyst
CATALYST_MIN_GAP  = 2.0  # % — minimum abs gap to count as catalyst
CATALYST_MIN_VOL  = 1.2  # ratio — pre-market vol > 1.2× expected baseline
_catalyst_cache: dict = {}   # {symbol: {"gap_pct": float, "vol_ratio": float, "score": int, ...key levels...}}
_catalyst_cache_date: str = ""
_catalyst_lock = threading.Lock()
_premarket_top_symbols: list = []  # top catalyst movers injected into scan universe after 9:35 AM

DAILY_BIAS_PENALTY = 12   # points deducted when stock is below its 20-day EMA
DAILY_BIAS_BONUS   =  5   # points added when stock is clearly above 20-day EMA
_bias_cache: dict  = {}   # {symbol: {"bias": "bull"/"bear"/"neutral", ...}}
_bias_cache_date: str = ""
_bias_lock = threading.Lock()

# ── Key level scoring ─────────────────────────────────────────────────────────
# Previous day high/low (PDH/PDL) and pre-market high/low (PMH/PML) are the
# most-watched intraday levels by institutional traders. A signal that fires
# exactly when price breaks or tests one of these levels is far more reliable
# than the same signal in the middle of nowhere.
KEY_LEVEL_ZONE     = 0.008   # within 0.8% of a level = "at the level"
KEY_LEVEL_BONUS    = 15      # max pts from key level proximity


def _key_level_bonus(price: float, catalyst: dict) -> tuple:
    """
    Return (bull_bonus, bear_bonus, bull_tags, bear_tags) based on price vs key levels.

    PDH (Previous Day High) — if price breaks ABOVE → very bullish (institutional breakout)
                            — if price tests from below → mild bullish pressure
    PDL (Previous Day Low)  — if price breaks BELOW → very bearish (institutional breakdown)
                            — if price bounces off → mild bullish support
    PMH/PML — pre-market high/low, similar logic, smaller bonus

    Breaking a level = strongest signal (price acceptance above/below)
    Testing from wrong side = weaker (possible rejection coming)

    Each side capped at KEY_LEVEL_BONUS so levels can't single-handedly push a weak signal.
    """
    pdh = catalyst.get("prev_day_high")
    pdl = catalyst.get("prev_day_low")
    pmh = catalyst.get("pm_high")
    pml = catalyst.get("pm_low")

    bull_b, bear_b = 0, 0
    bull_t, bear_t = [], []

    def _above(lvl):   return lvl and price > lvl
    def _near_above(lvl): return lvl and lvl * (1 - KEY_LEVEL_ZONE) <= price <= lvl
    def _below(lvl):   return lvl and price < lvl
    def _near_below(lvl): return lvl and lvl <= price <= lvl * (1 + KEY_LEVEL_ZONE)

    # PDH — break above = CALL confluence; bounce off support after break = also bullish
    if _above(pdh):
        bull_b += 15; bull_t.append(f"broke PDH ${pdh:.2f} ↑")
    elif _near_above(pdh):
        bull_b += 6;  bull_t.append(f"testing PDH ${pdh:.2f}")

    # PDL — break below = PUT confluence; price bouncing off PDL = CALL support
    if _below(pdl):
        bear_b += 15; bear_t.append(f"broke PDL ${pdl:.2f} ↓")
    elif _near_below(pdl):
        bull_b += 8;  bull_t.append(f"PDL support ${pdl:.2f} holding")

    # PMH — if different enough from PDH
    if pmh and pdh and abs(pmh - pdh) / max(pdh, 1) > 0.002:
        if _above(pmh):
            bull_b += 8; bull_t.append(f"broke PMH ${pmh:.2f} ↑")
        elif _near_above(pmh):
            bull_b += 4; bull_t.append(f"testing PMH ${pmh:.2f}")

    # PML — if different enough from PDL
    if pml and pdl and abs(pml - pdl) / max(pdl, 1) > 0.002:
        if _below(pml):
            bear_b += 8; bear_t.append(f"broke PML ${pml:.2f} ↓")
        elif _near_below(pml):
            bull_b += 4; bull_t.append(f"PML support ${pml:.2f} holding")

    return min(bull_b, KEY_LEVEL_BONUS), min(bear_b, KEY_LEVEL_BONUS), bull_t, bear_t


# ── Chloe Protocol add-ons ────────────────────────────────────────────────────

def _weekly_bias(primary: list) -> str:
    """
    Approximates Chloe's weekly bias check from 10-day 5m bars.
    Compares first-day average close to last-day average close.
    Returns 'bull', 'bear', or 'neutral'.
    """
    if not primary or len(primary) < 156:   # need at least 2 trading days
        return "neutral"
    bars_per_day = 78                        # 5m bars in a 6.5h RTH session
    first_day = primary[:bars_per_day]
    last_day  = primary[-bars_per_day:]
    first_avg = sum(b[4] for b in first_day) / len(first_day)
    last_avg  = sum(b[4] for b in last_day)  / len(last_day)
    chg = (last_avg - first_avg) / first_avg * 100
    if chg >  1.5: return "bull"
    if chg < -1.5: return "bear"
    return "neutral"


def _level_confluence(catalyst: dict) -> list:
    """
    Detects when multiple key levels cluster (within 0.15%) or land on a round number.
    Returns a list of human-readable tags: ["PDH≈PMH", "PDH≈$300", …]
    """
    lvls = {
        "PDH": catalyst.get("prev_day_high"),
        "PDL": catalyst.get("prev_day_low"),
        "PMH": catalyst.get("pm_high"),
        "PML": catalyst.get("pm_low"),
    }
    lvls = {k: v for k, v in lvls.items() if v}
    tags = []

    # Cluster check: two levels within 0.15% of each other
    keys = list(lvls.keys())
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            v1, v2 = lvls[keys[i]], lvls[keys[j]]
            if abs(v1 - v2) / max(v1, v2) < 0.0015:
                tags.append(f"{keys[i]}≈{keys[j]}")

    # Round-number check: within 0.3% of $x.00 / $x.50 / $x.25 / $x.75
    for name, val in lvls.items():
        for mult in [100, 50, 25, 10, 5]:
            nearest = round(val / mult) * mult
            if abs(val - nearest) / val < 0.003:
                tags.append(f"{name}≈${nearest:.0f}")
                break

    return tags


def _level_pullback(primary: list, catalyst: dict, direction: str):
    """
    Chloe's core entry condition: price already broke the key level in the last
    1–2 hours, then pulled back TO that level.  Entering here (not on the break
    candle) gives better R:R and avoids chasing.

    Returns (is_pullback: bool, tag: str | None)
    """
    if not primary or len(primary) < 6:
        return False, None

    pdh = catalyst.get("prev_day_high")
    pdl = catalyst.get("prev_day_low")
    pmh = catalyst.get("pm_high")
    pml = catalyst.get("pm_low")

    recent  = primary[-15:]      # last 15 bars ≈ 75 min
    current = recent[-1][4]      # latest close
    ZONE    = 0.0020             # 0.20% tolerance = "at the level"

    if direction == "CALL":
        for level, name in [(pdh, "PDH"), (pmh, "PMH")]:
            if not level:
                continue
            # At least one of the earlier bars closed clearly above the level
            had_break = any(b[4] > level * 1.0015 for b in recent[:-3])
            # Current price has come back near the level (pullback)
            near_level = abs(current - level) / level < ZONE
            # Still above or right at the level (not a breakdown)
            holds      = current >= level * (1 - ZONE)
            if had_break and near_level and holds:
                return True, f"pullback to {name} ${level:.2f}"

    elif direction == "PUT":
        for level, name in [(pdl, "PDL"), (pml, "PML")]:
            if not level:
                continue
            had_break  = any(b[4] < level * 0.9985 for b in recent[:-3])
            near_level = abs(current - level) / level < ZONE
            holds      = current <= level * (1 + ZONE)
            if had_break and near_level and holds:
                return True, f"pullback to {name} ${level:.2f}"

    return False, None


# ═══════════════════════════════════════════════════════════════════════════════
# INTRADAY DATA FETCHING
# ═══════════════════════════════════════════════════════════════════════════════

def _et_now():
    return datetime.now(tz=_ET)


def _parse_yahoo_bars(raw):
    """Extract (timestamp, open, high, low, close, volume) tuples from Yahoo chart JSON."""
    result = (raw.get("chart", {}).get("result") or [None])[0]
    if not result:
        return []
    q   = (result.get("indicators", {}).get("quote") or [{}])[0]
    ts  = result.get("timestamp", [])
    ops = q.get("open",   [])
    his = q.get("high",   [])
    los = q.get("low",    [])
    cls = q.get("close",  [])
    vls = q.get("volume", [])
    bars = [
        (t, o, h, l, c, v)
        for t, o, h, l, c, v in zip(ts, ops, his, los, cls, vls)
        if None not in (t, o, h, l, c, v) and v > 0
    ]
    return bars


def fetch_intraday(symbol, interval="5m", period="1d", yahoo_only=False):
    """Return list of (ts, open, high, low, close, volume). Cached per minute bucket."""
    bucket    = int(time.time() / 60)
    cache_key = f"id::{symbol}::{interval}::{period}::{bucket}::{'y' if yahoo_only else 'i'}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached
    import ibkr_data as _id
    bars = _id.get_intraday_bars(symbol, interval, period, prefer_yahoo=yahoo_only)
    if bars:
        cache_set(cache_key, bars)
    return bars


def fetch_symbol_data(symbol, use_ibkr=False):
    """Fetch 5 days of 5-min bars + 15-min HTF.
    use_ibkr=True for SPY regime (real-time critical); Yahoo for bulk scan."""
    yahoo = not use_ibkr
    primary = fetch_intraday(symbol, "5m",  "5d", yahoo_only=yahoo)
    htf     = fetch_intraday(symbol, "15m", "5d", yahoo_only=yahoo)
    htf = htf[-13:] if htf else []
    return primary, htf


def build_scan_universe(symbols=None, top_n=None, min_rv=None):
    """
    Pre-screen symbols by relative volume; return the most active ones.
    Seed list = DEFAULT_SYMBOLS + live IBKR top-gainers (deduped).
    """
    syms   = list(symbols) if symbols is not None else list(DEFAULT_SYMBOLS)
    top_n  = top_n    if top_n    is not None else PRE_SCREEN_TOP
    min_rv = min_rv   if min_rv   is not None else PRE_SCREEN_MIN_RV

    # Augment with live IBKR top gainers so we catch hot stocks outside our list
    try:
        import ibkr_data as _id
        live = _id.get_top_gainers(20)
        syms = list(dict.fromkeys(syms + live))  # dedup, preserve order
    except Exception:
        pass

    def _quick_rv(sym):
        try:
            bars = fetch_intraday(sym, "5m", "1d", yahoo_only=True)
            if len(bars) < 5:
                return sym, 0.0
            vols = [b[5] for b in bars]
            avg  = sum(vols[:-1]) / max(1, len(vols) - 1)
            rv   = vols[-1] / avg if avg > 0 else 0.0
            return sym, round(rv, 2)
        except Exception:
            return sym, 0.0

    with ThreadPoolExecutor(max_workers=16) as ex:
        rv_pairs = list(ex.map(_quick_rv, syms))

    active = [(sym, rv) for sym, rv in rv_pairs if rv >= min_rv]
    active.sort(key=lambda x: -x[1])
    return [sym for sym, _ in active[:top_n]]


def _fetch_vix():
    """Return current VIX level, or None on failure."""
    try:
        bars = fetch_intraday("^VIX", "5m", "1d")
        return round(bars[-1][4], 2) if bars else None
    except Exception:
        return None


def _account_cash():
    """Read available cash from portfolio.json; fallback to $1 000."""
    try:
        with open(PORTFOLIO_FILE) as f:
            return float(json.load(f).get("cash", 1000))
    except Exception:
        return 1000.0


def _position_size(entry, stop):
    """1% risk rule: shares = (cash × RISK_PCT) / |entry − stop|."""
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return 0
    return max(1, int(_account_cash() * RISK_PCT_PER_TRADE / risk_per_share))


# ═══════════════════════════════════════════════════════════════════════════════
# INTRADAY-SPECIFIC INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════

def _rma(series, period):
    """Wilder's RMA — same as ta.rma in Pine Script."""
    if len(series) < period:
        return None
    val = sum(series[:period]) / period
    for x in series[period:]:
        val = (val * (period - 1) + x) / period
    return val


def _calc_vwap(highs, lows, closes, vols):
    """Session VWAP from all provided bars (pass today's bars only)."""
    cum_tpv = sum((h + l + c) / 3 * v for h, l, c, v in zip(highs, lows, closes, vols))
    cum_vol = sum(vols)
    return round(cum_tpv / cum_vol, 4) if cum_vol > 0 else closes[-1]


def _calc_vwap_sigma(highs, lows, closes, vols):
    """Return (vwap, sigma) using volume-weighted std dev of typical price."""
    import math
    tps = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    cum_vol = sum(vols)
    if cum_vol <= 0:
        return closes[-1], 0.0
    vwap = sum(tp * v for tp, v in zip(tps, vols)) / cum_vol
    var  = sum(v * (tp - vwap) ** 2 for tp, v in zip(tps, vols)) / cum_vol
    return round(vwap, 4), round(math.sqrt(var), 4)


def _calc_anchored_vwap(bars, anchor_price: float):
    """
    VWAP anchored to the bar after price first crossed anchor_price from below.
    Returns anchored VWAP or None if there is no anchor point in the window.
    Used to track institutional cost basis since a key structural level was broken.
    """
    if not bars or anchor_price <= 0:
        return None
    # Find the first bar where close exceeded anchor_price — start accumulation there
    start = None
    for i, b in enumerate(bars):
        if b[4] >= anchor_price:   # b[4] = close
            start = i
            break
    if start is None:
        return None
    cum_tpv = cum_vol = 0.0
    for b in bars[start:]:
        h, l, c, v = b[2], b[3], b[4], b[5]
        tp = (h + l + c) / 3
        cum_tpv += tp * v
        cum_vol += v
    return round(cum_tpv / cum_vol, 4) if cum_vol > 0 else None


def _calc_atr_bars(highs, lows, closes, period=14):
    """ATR using Wilder's RMA (matches Pine Script ta.atr)."""
    if len(highs) < period + 1:
        return None
    trs = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]))
        for i in range(1, len(highs))
    ]
    val = _rma(trs, period)
    return round(val, 5) if val else None


def _calc_adx_bars(highs, lows, closes, period=14):
    """Manual ADX/+DI/-DI using Wilder's RMA (matches Pine Script manual ADX)."""
    if len(highs) < period * 2 + 2:
        return None, None, None

    plus_dms, minus_dms, trs = [], [], []
    for i in range(1, len(highs)):
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dms.append(up   if (up > down   and up   > 0) else 0.0)
        minus_dms.append(down if (down > up  and down > 0) else 0.0)
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1])
        ))

    atr_s     = _rma(trs,       period)
    plus_rma  = _rma(plus_dms,  period)
    minus_rma = _rma(minus_dms, period)
    if not atr_s or atr_s == 0:
        return None, None, None

    plus_di  = 100 * (plus_rma  or 0) / atr_s
    minus_di = 100 * (minus_rma or 0) / atr_s

    # Build DX series then smooth to get ADX
    n        = len(trs)
    atr_run  = sum(trs[:period])       / period
    pdi_run  = sum(plus_dms[:period])  / period
    mdi_run  = sum(minus_dms[:period]) / period
    dx_list  = []
    for i in range(period, n):
        atr_run = (atr_run * (period - 1) + trs[i])       / period
        pdi_run = (pdi_run * (period - 1) + plus_dms[i])  / period
        mdi_run = (mdi_run * (period - 1) + minus_dms[i]) / period
        if atr_run == 0:
            dx_list.append(0.0)
            continue
        _pdi = 100 * pdi_run / atr_run
        _mdi = 100 * mdi_run / atr_run
        dx_list.append(
            100 * abs(_pdi - _mdi) / (_pdi + _mdi) if (_pdi + _mdi) > 0 else 0.0
        )

    adx = _rma(dx_list, period)
    return (
        round(adx, 2)      if adx      else None,
        round(plus_di, 2)  if plus_di  else None,
        round(minus_di, 2) if minus_di else None,
    )


def _calc_rvol_tod(primary_bars):
    """
    Relative volume normalized by time-of-day slot.
    Compares the current bar's volume to the average volume at the same 5-min
    time slot across all prior sessions in the 5-day window.  This avoids the
    inflated baseline produced by a plain 20-bar SMA when the first bar of each
    session always carries abnormally high volume.
    Falls back to a 20-bar rolling average when fewer than 2 same-slot samples
    exist (e.g. rare pre-market slots or the very first session in history).
    """
    if len(primary_bars) < 2:
        return 1.0
    cur      = primary_bars[-1]
    cur_ts   = datetime.fromtimestamp(cur[0], tz=_ET)
    cur_slot = (cur_ts.hour, cur_ts.minute)
    cur_vol  = cur[5]
    prior_vols = [
        b[5] for b in primary_bars[:-1]
        if b[5] > 0 and
           (datetime.fromtimestamp(b[0], tz=_ET).hour,
            datetime.fromtimestamp(b[0], tz=_ET).minute) == cur_slot
    ]
    if len(prior_vols) >= 2:
        avg = sum(prior_vols) / len(prior_vols)
    else:
        vls = [b[5] for b in primary_bars]
        n   = min(20, len(vls) - 1)
        avg = sum(vls[-n - 1:-1]) / n if n > 0 else cur_vol
    return round(cur_vol / avg, 2) if avg > 0 else 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# CHART SERIES (per-bar arrays for frontend charting)
# ═══════════════════════════════════════════════════════════════════════════════

def compute_chart_series(primary_bars):
    """Return per-bar VWAP, EMA9, EMA21 arrays suitable for Lightweight Charts."""
    if len(primary_bars) < 2:
        return {}

    ts  = [b[0] for b in primary_bars]
    his = [b[2] for b in primary_bars]
    los = [b[3] for b in primary_bars]
    cls = [b[4] for b in primary_bars]
    vls = [b[5] for b in primary_bars]

    # Cumulative session VWAP (resets each bar, builds from session open)
    cum_tpv, cum_vol = 0.0, 0.0
    vwap_vals = []
    for h, l, c, v in zip(his, los, cls, vls):
        cum_tpv += (h + l + c) / 3 * v
        cum_vol += v
        vwap_vals.append(round(cum_tpv / cum_vol, 4) if cum_vol > 0 else c)

    # EMA series builder — one value per bar, None until period is met
    def _ema_series(values, period):
        if len(values) < period:
            return [None] * len(values)
        k   = 2 / (period + 1)
        out = [None] * (period - 1)
        ema = sum(values[:period]) / period
        out.append(round(ema, 4))
        for v in values[period:]:
            ema = v * k + ema * (1 - k)
            out.append(round(ema, 4))
        return out

    # RSI series — Wilder's smoothed, one value per bar
    def _rsi_series(values, period=14):
        n = len(values)
        if n < period + 1:
            return [None] * n
        out  = [None] * period
        gains = losses = 0.0
        for i in range(1, period + 1):
            d = values[i] - values[i - 1]
            if d > 0: gains += d
            else:     losses += abs(d)
        ag, al = gains / period, losses / period
        out.append(round(100 if al == 0 else 100 - 100 / (1 + ag / al), 2))
        for i in range(period + 1, n):
            d  = values[i] - values[i - 1]
            ag = (ag * (period - 1) + (d if d > 0 else 0)) / period
            al = (al * (period - 1) + (abs(d) if d < 0 else 0)) / period
            out.append(round(100 if al == 0 else 100 - 100 / (1 + ag / al), 2))
        return out

    ema9_vals  = _ema_series(cls, 9)
    ema21_vals = _ema_series(cls, 21)
    rsi_vals   = _rsi_series(cls, 14)

    return {
        "vwap": [{"t": t, "v": v} for t, v in zip(ts, vwap_vals)],
        "ema9":  [{"t": t, "v": v} for t, v in zip(ts, ema9_vals)  if v is not None],
        "ema21": [{"t": t, "v": v} for t, v in zip(ts, ema21_vals) if v is not None],
        "rsi":   [{"t": t, "v": v} for t, v in zip(ts, rsi_vals)   if v is not None],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# FULL INDICATOR SNAPSHOT
# ═══════════════════════════════════════════════════════════════════════════════

def compute_indicators(primary_bars, htf_bars):
    """
    Derive all indicators from 5-min (primary) and 15-min (HTF) bar lists.

    primary_bars covers up to 5 trading days so trend indicators (EMA, RSI,
    ATR, ADX) have enough history from the first bar of the session.
    Session-specific indicators (VWAP, ORB, open_first) are computed from
    today's bars only so they reset correctly each morning.

    Returns a flat dict, or None if there is insufficient data.
    """
    if len(primary_bars) < 20:
        return None

    # ── Split: today's bars vs full cross-day window ──────────────────────────
    last_et    = datetime.fromtimestamp(primary_bars[-1][0], tz=_ET)
    today_date = last_et.date()
    today_bars = [b for b in primary_bars
                  if datetime.fromtimestamp(b[0], tz=_ET).date() == today_date]
    if not today_bars:
        return None

    # Cross-day arrays — used for all trend / momentum indicators
    ops = [b[1] for b in primary_bars]
    his = [b[2] for b in primary_bars]
    los = [b[3] for b in primary_bars]
    cls = [b[4] for b in primary_bars]
    vls = [b[5] for b in primary_bars]

    price    = cls[-1]
    bar_open = ops[-1]

    # Today-only arrays — used for session-reset indicators
    t_his = [b[2] for b in today_bars]
    t_los = [b[3] for b in today_bars]
    t_cls = [b[4] for b in today_bars]
    t_vls = [b[5] for b in today_bars]
    t_ops = [b[1] for b in today_bars]

    # ── VWAP + σ bands (today only — resets each session) ────────────────────
    vwap, vwap_sigma = _calc_vwap_sigma(t_his, t_los, t_cls, t_vls)

    # ── ATR (cross-day — needs history for accurate volatility) ──────────────
    atr = _calc_atr_bars(his, los, cls, 14) or (price * 0.005)

    # ── ADX (cross-day) ───────────────────────────────────────────────────────
    adx, plus_di, minus_di = _calc_adx_bars(his, los, cls, 14)

    # ── EMAs (cross-day) ──────────────────────────────────────────────────────
    ema9  = _calc_ema(cls, 9)
    ema21 = _calc_ema(cls, 21)

    # ── RSI (cross-day) ───────────────────────────────────────────────────────
    rsi = _calc_rsi(cls, 14)

    # ── Volume ratio (time-of-day normalized — same 5-min slot across prior sessions) ──
    rel_vol = _calc_rvol_tod(primary_bars)

    # ── Smart money (cross-day) ───────────────────────────────────────────────
    cmf           = _calc_cmf(his, los, cls, vls, 20)
    obv           = _calc_obv_trend(cls, vls)
    mfi           = _calc_mfi(his, los, cls, vls, 14)
    bb_squeezed, bb_width = _calc_bb_compression(cls, 20)

    # ── Price structure (cross-day) ───────────────────────────────────────────
    lb         = min(15, len(his) - 1)
    highest15  = max(his[-lb - 1:-1]) if lb > 0 else his[-1]
    lowest15   = min(los[-lb - 1:-1]) if lb > 0 else los[-1]
    prev_close = cls[-2] if len(cls) >= 2 else price
    prev_high  = his[-2] if len(his) >= 2 else his[-1]
    prev_low   = los[-2] if len(los) >= 2 else los[-1]
    body_size   = abs(price - bar_open)
    candle_range = (his[-1] - los[-1]) if his and los else 0
    body_ratio  = (body_size / candle_range) if candle_range > 0 else 0.0
    candle_bull = price > bar_open
    candle_bear = price < bar_open

    # ── 15-min HTF EMA trend ──────────────────────────────────────────────────
    htf_bull = htf_bear = False
    if len(htf_bars) >= 21:
        htf_cls = [b[4] for b in htf_bars]
        he9     = _calc_ema(htf_cls, 9)
        he21    = _calc_ema(htf_cls, 21)
        if he9 and he21:
            htf_bull = he9 > he21
            htf_bear = he9 < he21

    # ── Session info (ET) ─────────────────────────────────────────────────────
    h, m          = last_et.hour, last_et.minute
    in_session    = (h > 9 or (h == 9 and m >= 35)) and h < 16
    avoid_session = (h >= 11 and h <= 13)
    opening_drive = (h == 9 and m <= 45)

    # ── Opening Range (today's first 3 bars = first 15 min of session) ────────
    or_established = len(t_his) > 3
    or_high = max(t_his[0], t_his[1], t_his[2]) if or_established else t_his[0]
    or_low  = min(t_los[0], t_los[1], t_los[2]) if or_established else t_los[0]
    or_break_up   = or_established and price > or_high and (len(t_cls) < 2 or t_cls[-2] <= or_high)
    or_break_down = or_established and price < or_low  and (len(t_cls) < 2 or t_cls[-2] >= or_low)

    return {
        # Price / bar
        "price":        round(price, 4),
        "bar_open":     bar_open,
        "prev_close":   prev_close,
        "prev_high":    prev_high,
        "prev_low":     prev_low,
        "body_size":    body_size,
        "body_ratio":   round(body_ratio, 3),
        "candle_bull":  candle_bull,
        "candle_bear":  candle_bear,
        # Trend
        "vwap":         vwap,
        "vwap_sigma":   vwap_sigma,
        "ema9":         ema9,
        "ema21":        ema21,
        "htf_bull":     htf_bull,
        "htf_bear":     htf_bear,
        # Volatility / momentum
        "atr":          atr,
        "adx":          adx,
        "plus_di":      plus_di,
        "minus_di":     minus_di,
        "rsi":          rsi,
        # Volume
        "rel_vol":      rel_vol,
        "vol_current":  vls[-1],
        "vol_prev":     vls[-2] if len(vls) >= 2 else vls[-1],
        # Smart money
        "cmf":          cmf,
        "obv":          obv,
        "mfi":          mfi,
        "bb_squeezed":  bb_squeezed,
        "bb_width":     bb_width,
        # Breakout levels
        "highest15":    highest15,
        "lowest15":     lowest15,
        # Opening Range Breakout (today-only)
        "or_high":        or_high,
        "or_low":         or_low,
        "or_established": or_established,
        "or_break_up":    or_break_up,
        "or_break_down":  or_break_down,
        # Day-open price for RS vs SPY (today's first bar)
        "open_first":   t_ops[0],
        # RS vs SPY placeholder — filled in evaluate_symbol()
        "rs_vs_spy":    0.0,
        # Session flags
        "in_session":    in_session,
        "avoid_session": avoid_session,
        "opening_drive": opening_drive,
        # Raw cross-day series (used by _prev_rsi and score_intraday)
        "closes":   cls,
        "highs":    his,
        "lows":     los,
        "volumes":  vls,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# HYBRID SCORING ENGINE
# Combines Pine Sniper's additive score + your algo's smart-money layer.
# Raw scores may exceed 100; caller clamps to 0-100.
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_1h_swing(h1_bars):
    """
    Detect last confirmed swing high/low from 1H bars and whether the
    structure shows higher highs / lower lows.

    A swing is confirmed when bar[i] is an extreme relative to both its
    neighbours — so the last bar is never a confirmed swing (still forming).
    Returns (last_swing_high, last_swing_low, higher_highs, lower_lows).
    All values are None/False if there is insufficient data.
    """
    if not h1_bars or len(h1_bars) < 5:
        return None, None, False, False

    his = [b[2] for b in h1_bars]
    los = [b[3] for b in h1_bars]

    swing_highs, swing_lows = [], []
    # Only iterate up to second-to-last bar so each swing is confirmed
    for i in range(1, len(his) - 1):
        if his[i] > his[i - 1] and his[i] > his[i + 1]:
            swing_highs.append(his[i])
        if los[i] < los[i - 1] and los[i] < los[i + 1]:
            swing_lows.append(los[i])

    last_sh = swing_highs[-1] if swing_highs else None
    last_sl = swing_lows[-1]  if swing_lows  else None
    higher_highs = len(swing_highs) >= 2 and swing_highs[-1] > swing_highs[-2]
    lower_lows   = len(swing_lows)  >= 2 and swing_lows[-1]  < swing_lows[-2]

    return last_sh, last_sl, higher_highs, lower_lows


def _prev_rsi(ind):
    """RSI on closes[:-1] — used to detect RSI direction change in reversals."""
    closes = ind["closes"]
    if len(closes) < 16:
        return 50.0
    return _calc_rsi(closes[:-1], 14) or 50.0


def score_intraday(ind, *, noise_upper=None, noise_lower=None,
                   l2_imbalance=None, sector_aligned=None,
                   macro_day=False):
    """
    Returns (bull_score, bear_score, bull_reasons, bear_reasons,
             bull_warnings, bear_warnings).

    Optional kwargs injected by evaluate_symbol():
      noise_upper/lower  — Zarattini noise-cone bounds (None = skip gate)
      l2_imbalance       — bid/(bid+ask) from L2 top-of-book (None = skip)
      sector_aligned     — True/False/None — sector ETF moving same direction
      macro_day          — True if today is FOMC/CPI/PPI (halve ORB bonus)
    """
    p        = ind["price"]
    atr      = ind["atr"] or (p * 0.005)
    adx      = ind["adx"]
    rsi      = ind["rsi"]
    ema9     = ind["ema9"]
    ema21    = ind["ema21"]
    vwap     = ind["vwap"]
    rv       = ind["rel_vol"]
    prev_c   = ind["prev_close"]
    prev_h   = ind["prev_high"]
    prev_l   = ind["prev_low"]

    bull, bear   = 0.0, 0.0
    bull_r, bear_r = [], []
    bull_w, bear_w = [], []

    # ── TREND: EMA9>EMA21 + above/below VWAP ─────────────────────────────────
    bull_trend = bool(ema9 and ema21 and ema9 > ema21 and p > vwap)
    bear_trend = bool(ema9 and ema21 and ema9 < ema21 and p < vwap)

    if bull_trend:
        bull += 20; bull_r.append("trend: EMA9>21 + above VWAP")
    elif ema9 and ema21 and ema9 > ema21:
        bull += 8;  bull_r.append("EMA9>EMA21")

    if bear_trend:
        bear += 20; bear_r.append("trend: EMA9<21 + below VWAP")
    elif ema9 and ema21 and ema9 < ema21:
        bear += 8;  bear_r.append("EMA9<EMA21")

    # VWAP side bonus
    if p > vwap:
        bull += 8;  bull_r.append(f"above VWAP ({vwap:.2f})")
    else:
        bear += 8;  bear_r.append(f"below VWAP ({vwap:.2f})")

    # ── VWAP σ BAND EXPANSION ─────────────────────────────────────────────────
    # Price above VWAP+1σ = institutional momentum zone (algos chasing above benchmark)
    # Price above VWAP+2σ = potential exhaustion unless volume is accelerating
    vwap_sigma = ind.get("vwap_sigma", 0.0)
    if vwap_sigma and vwap_sigma > 0:
        vb1 = vwap + vwap_sigma
        vb2 = vwap + 2 * vwap_sigma
        if p > vb2:
            # Extended: possible exhaustion — penalise unless RVOL confirms expansion
            if rv < 1.5:
                bull -= 10; bull_w.append(f"extended above VWAP+2σ (${vb2:.2f}) — exhaustion risk")
            else:
                bull += 5;  bull_r.append(f"above VWAP+2σ (${vb2:.2f}) w/ volume surge")
        elif p > vb1:
            bull += 8;  bull_r.append(f"above VWAP+1σ (${vb1:.2f}) — momentum zone")
        vb1_dn = vwap - vwap_sigma
        vb2_dn = vwap - 2 * vwap_sigma
        if p < vb2_dn:
            if rv < 1.5:
                bear -= 10; bear_w.append(f"extended below VWAP-2σ (${vb2_dn:.2f}) — exhaustion risk")
            else:
                bear += 5;  bear_r.append(f"below VWAP-2σ (${vb2_dn:.2f}) w/ volume surge")
        elif p < vb1_dn:
            bear += 8;  bear_r.append(f"below VWAP-1σ (${vb1_dn:.2f}) — momentum zone")

    # ── HTF CONFIRMATION (15-min EMA trend) ───────────────────────────────────
    if ind["htf_bull"]:
        bull += 10; bull_r.append("15-min HTF uptrend")
    if ind["htf_bear"]:
        bear += 10; bear_r.append("15-min HTF downtrend")

    # ── 1H SWING STRUCTURE ────────────────────────────────────────────────────
    # Breaking a 1H swing high/low = structure break — strongest signal.
    # At the level (within 0.5%) = key inflection zone.
    # Higher highs / lower lows = trend is making progress.
    h1_sh = ind.get("h1_swing_high")
    h1_sl = ind.get("h1_swing_low")
    if h1_sh:
        if p > h1_sh:
            bull += 12; bull_r.append(f"broke 1H swing high ${h1_sh:.2f}")
        elif p >= h1_sh * (1 - 0.005):
            bull += 6;  bull_r.append(f"at 1H swing high ${h1_sh:.2f}")
    if h1_sl:
        if p < h1_sl:
            bear += 12; bear_r.append(f"broke 1H swing low ${h1_sl:.2f}")
        elif p <= h1_sl * (1 + 0.005):
            bear += 6;  bear_r.append(f"at 1H swing low ${h1_sl:.2f}")
    if ind.get("h1_higher_highs"):
        bull += 8;  bull_r.append("1H higher highs — bull structure")
    if ind.get("h1_lower_lows"):
        bear += 8;  bear_r.append("1H lower lows — bear structure")

    # ── RSI MOMENTUM ──────────────────────────────────────────────────────────
    if rsi:
        if rsi > 58:
            bull += 15; bull_r.append(f"RSI momentum ({rsi:.0f})")
        elif rsi < 42:
            bear += 15; bear_r.append(f"RSI momentum ({rsi:.0f})")

        # Exhaustion penalties (Pine: bullExhausted / bearExhausted)
        if rsi > 78 and ind["vol_current"] < ind["vol_prev"]:  # overbought + volume declining
            bull -= 20; bull_w.append(f"RSI exhausted ({rsi:.0f})")
        elif rsi > 78:
            bull -= 10; bull_w.append(f"RSI overheated ({rsi:.0f})")
        elif rsi < 22:
            bear -= 20; bear_w.append(f"RSI exhausted ({rsi:.0f})")

    # ── ADX TREND STRENGTH ────────────────────────────────────────────────────
    if adx is not None:
        if adx > 30:
            bull += 15; bear += 15   # strong trend benefits whichever side
        elif adx > ADX_TREND_MIN:
            bull += 8;  bear += 8
        if adx < ADX_SIDEWAYS:
            bull -= 20; bear -= 20
            bull_w.append(f"sideways (ADX {adx:.0f})")
            bear_w.append(f"sideways (ADX {adx:.0f})")

    # ── BREAKOUTS (Pine: 15-bar high/low) ─────────────────────────────────────
    if p > ind["highest15"]:
        bull += 15; bull_r.append("15-bar high breakout")
    if p < ind["lowest15"]:
        bear += 15; bear_r.append("15-bar low breakdown")

    # ── CONTINUATION ──────────────────────────────────────────────────────────
    if ema9 and p > prev_c and p > ema9 and ind["vol_current"] > ind["vol_prev"]:
        bull += 10; bull_r.append("momentum continuation")
    if ema9 and p < prev_c and p < ema9 and ind["vol_current"] > ind["vol_prev"]:
        bear += 10; bear_r.append("momentum continuation")

    # ── REVERSAL: EMA9 cross with RSI flip ────────────────────────────────────
    prior_rsi = _prev_rsi(ind)
    if ema9 and prev_c < ema9 <= p and rsi and rsi > prior_rsi and ind["candle_bull"]:
        bull += 20; bull_r.append("EMA9 cross-up reversal")
    if ema9 and prev_c > ema9 >= p and ind["candle_bear"]:
        bear += 20; bear_r.append("EMA9 cross-down reversal")

    # ── EARLY REVERSAL: liquidity sweep + engulfing (Pine: earlyBull/earlyBear)
    sweep_bull = (ind["lows"][-1]  < prev_l and ind["candle_bull"] and rv > 1.5)
    sweep_bear = (ind["highs"][-1] > prev_h and ind["candle_bear"] and rv > 1.5)
    if sweep_bull:
        bull += 15; bull_r.append("sweep low + bull engulf (reversal)")
    if sweep_bear:
        bear += 15; bear_r.append("sweep high + bear engulf (reversal)")

    # ── EXPLOSIVE CANDLE (Pine: bodySize > ATR * 0.4) ─────────────────────────
    if ind["body_size"] > atr * 0.4:
        if ind["candle_bull"]:
            bull += 15; bull_r.append("explosive bull candle")
        if ind["candle_bear"]:
            bear += 15; bear_r.append("explosive bear candle")

    # ── BODY/RANGE RATIO (MTF Momentum filter) ────────────────────────────────
    # Strong candle (body ≥60% of range) at a key level = genuine momentum.
    # Doji/wick-heavy candle (<30%) at a key level = likely fake-out/trap.
    br = ind.get("body_ratio", 0)
    if br >= 0.60:
        if ind["candle_bull"]:
            bull += 8;  bull_r.append(f"momentum candle {br:.0%} body")
        if ind["candle_bear"]:
            bear += 8;  bear_r.append(f"momentum candle {br:.0%} body")
    elif br < 0.30 and br > 0:
        bull -= 8; bear -= 8
        bull_w.append(f"doji/wick candle {br:.0%} body — possible trap")
        bear_w.append(f"doji/wick candle {br:.0%} body — possible trap")

    # ── VOLUME ────────────────────────────────────────────────────────────────
    if rv > 2.0:
        bull += 18; bear += 18; bull_r.append(f"extreme volume {rv:.1f}x")
    elif rv > 1.3:
        bull += 12; bear += 12

    # ── SMART MONEY CONFLUENCE (your algo: vol+CMF+OBV+MFI) ──────────────────
    cmf = ind["cmf"]; obv = ind["obv"]; mfi = ind["mfi"]
    sm  = 0
    if rv >= 1.5:                              sm += 1
    if cmf is not None and cmf >= 0.10:        sm += 1; bull += 5
    elif cmf is not None and cmf <= -0.10:     bear += 5
    if obv == 1:                               sm += 1
    elif obv == -1:                            bear += 5
    if mfi is not None and 25 <= mfi <= 55:    sm += 1

    if sm >= 4:
        bull += 18; bull_r.append("full smart money: vol+CMF+OBV+MFI")
    elif sm >= 3:
        bull += 9;  bull_r.append(f"smart money confluence {sm}/4")

    # ── BB SQUEEZE: coiled spring (your algo) ─────────────────────────────────
    if ind["bb_squeezed"]:
        if obv == 1:
            bull += 12; bull_r.append("BB squeeze + OBV rising (coiled spring)")
        else:
            bull += 5; bear += 5

    # ── OPENING DRIVE BONUS (Pine: hour==9 and minute<=45) ────────────────────
    if ind["opening_drive"]:
        bull += 5; bear += 5

    # ── OPENING RANGE BREAKOUT ────────────────────────────────────────────────
    # Full +25 only when volume confirms AND price is outside Zarattini noise cone.
    # Zarattini, Barbon & Aziz (SSRN 4729284, 2024): ORB edge disappears without
    # abnormally high opening volume — random-stock ORB shows near-zero edge.
    # Noise cone: if price is within ±avg-daily-range of open, ORB bonus is halved
    # since it may be random drift rather than a genuine breakout.
    if ind.get("or_break_up"):
        above_noise = noise_upper is None or p >= noise_upper
        base_pts    = 25 if (rv >= 1.3 and above_noise) else 8
        macro_mult  = 0.5 if macro_day else 1.0
        pts         = int(base_pts * macro_mult)
        tag         = f"ORB breakout above ${ind['or_high']:.2f} (vol {rv:.1f}x)"
        if macro_day:
            tag += " [macro day — halved]"
        elif not above_noise:
            tag += " [noise cone]"
        elif rv < 1.3:
            tag = f"ORB breakout above ${ind['or_high']:.2f} (low vol)"
        bull += pts; bull_r.append(tag)
    if ind.get("or_break_down"):
        below_noise = noise_lower is None or p <= noise_lower
        base_pts    = 25 if (rv >= 1.3 and below_noise) else 8
        macro_mult  = 0.5 if macro_day else 1.0
        pts         = int(base_pts * macro_mult)
        tag         = f"ORB breakdown below ${ind['or_low']:.2f} (vol {rv:.1f}x)"
        if macro_day:
            tag += " [macro day — halved]"
        elif not below_noise:
            tag += " [noise cone]"
        elif rv < 1.3:
            tag = f"ORB breakdown below ${ind['or_low']:.2f} (low vol)"
        bear += pts; bear_r.append(tag)

    # ── ORDER FLOW IMBALANCE PROXY ────────────────────────────────────────────
    # (Close - Open) / (High - Low) per bar: +1 = full buy pressure, -1 = full sell.
    # Validated OHLCV proxy for tape-level OFI (arXiv:2408.03594, 2024).
    _bar_range = ind["highs"][-1] - ind["lows"][-1]
    if _bar_range > 0:
        ofi = (p - ind["bar_open"]) / _bar_range
        if ofi > 0.65:
            bull += 8;  bull_r.append(f"OFI buy pressure ({ofi:.2f})")
        elif ofi < -0.65:
            bear += 8;  bear_r.append(f"OFI sell pressure ({ofi:.2f})")

    # ── L2 TOP-OF-BOOK IMBALANCE ─────────────────────────────────────────────
    # bid_sz / (bid_sz + ask_sz) from IBKR market data snapshot.
    # >0.6 = bid-heavy (buy pressure), <0.4 = ask-heavy (sell pressure).
    if l2_imbalance is not None:
        if l2_imbalance > 0.60:
            bull += 10; bull_r.append(f"L2 bid-heavy ({l2_imbalance:.2f})")
        elif l2_imbalance > 0.55:
            bull += 5;  bull_r.append(f"L2 mild bid ({l2_imbalance:.2f})")
        elif l2_imbalance < 0.40:
            bear += 10; bear_r.append(f"L2 ask-heavy ({l2_imbalance:.2f})")
        elif l2_imbalance < 0.45:
            bear += 5;  bear_r.append(f"L2 mild ask ({l2_imbalance:.2f})")

    # ── FIRST-30-MIN SPY ALIGNMENT (Gao et al. JFE 2018) ─────────────────────
    # If SPY's first-30-min return is positive, long bias for rest of day.
    with _regime_cache_lock:
        f30_bull = _regime_cache.get("first30_bull")
        f30_ret  = _regime_cache.get("first30_ret", 0.0)
    if f30_bull is True and abs(f30_ret) >= 0.10:
        bull += 8;  bull_r.append(f"first-30m SPY up {f30_ret:+.2f}%")
        bear -= 5
    elif f30_bull is False and abs(f30_ret) >= 0.10:
        bear += 8;  bear_r.append(f"first-30m SPY down {f30_ret:+.2f}%")
        bull -= 5

    # ── SECTOR ETF ALIGNMENT ─────────────────────────────────────────────────
    # Only score if sector_aligned was computed by evaluate_symbol().
    if sector_aligned is True:
        bull += 8;  bull_r.append("sector ETF aligned")
    elif sector_aligned is False:
        bull -= 8;  bull_w.append("sector ETF diverging")
        bear += 8;  bear_r.append("sector ETF diverging")

    # ── MACRO DAY PENALTY ─────────────────────────────────────────────────────
    if macro_day:
        bull -= 10; bear -= 10
        bull_w.append("FOMC/CPI/PPI day — reduced conviction")
        bear_w.append("FOMC/CPI/PPI day — reduced conviction")

    # ── RELATIVE STRENGTH vs SPY ──────────────────────────────────────────────
    rs = ind.get("rs_vs_spy", 0.0)
    if   rs >  2.0: bull += 12; bull_r.append(f"RS vs SPY +{rs:.1f}%")
    elif rs >  1.0: bull +=  6; bull_r.append(f"RS vs SPY +{rs:.1f}%")
    elif rs < -2.0: bear += 12; bear_r.append(f"RS vs SPY {rs:.1f}%")
    elif rs < -1.0: bear +=  6; bear_r.append(f"RS vs SPY {rs:.1f}%")

    # ── TREND DAY (Pine: trendDayBull/Bear: VWAP + EMA alignment + ADX>30) ────
    if adx and adx > 30 and bull_trend:
        bull += 20; bull_r.append(f"trend day setup (ADX {adx:.0f})")
    if adx and adx > 30 and bear_trend:
        bear += 20; bear_r.append(f"trend day setup (ADX {adx:.0f})")

    # ══ PENALTIES ══

    # Midday chop window (11am–1pm ET)
    if ind["avoid_session"]:
        bull -= 15; bear -= 15
        bull_w.append("midday chop (11-1pm ET)")
        bear_w.append("midday chop (11-1pm ET)")

    # Too extended from EMA9 (Pine: tooExtendedBull/Bear)
    if ema9:
        if p > ema9 + atr * 1.5:
            bull -= 20; bull_w.append("too extended above EMA9")
        elif p < ema9 - atr * 1.5:
            bear -= 20; bear_w.append("too extended below EMA9")

    # Sweep against direction (Pine: sweepHigh/sweepLow)
    trap_bull = (ind["highs"][-1] > prev_h and p < prev_h)  # bull trap
    trap_bear = (ind["lows"][-1]  < prev_l and p > prev_l)  # bear trap
    if trap_bull:
        bull -= 10; bull_w.append("sweep high (possible bull trap)")
    if trap_bear:
        bear -= 10; bear_w.append("sweep low (possible bear trap)")

    return bull, bear, bull_r, bear_r, bull_w, bear_w


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL GRADING
# ═══════════════════════════════════════════════════════════════════════════════

def _grade(score):
    if score >= 90: return "S"
    if score >= 80: return "A"
    if score >= 68: return "B"
    if score >= 55: return "C"
    return "D"


# ═══════════════════════════════════════════════════════════════════════════════
# CATALYST CACHE
# ═══════════════════════════════════════════════════════════════════════════════

def _get_catalyst(symbol: str) -> dict:
    """
    Return cached pre-market catalyst data for symbol.
    Cache is populated once per day by _prime_catalyst_cache().
    Thread-safe; never blocks — returns {} if not yet cached.
    """
    global _catalyst_cache_date
    today = _et_now().strftime("%Y-%m-%d")
    with _catalyst_lock:
        if _catalyst_cache_date != today:
            _catalyst_cache.clear()
            _catalyst_cache_date = today
        return dict(_catalyst_cache.get(symbol.upper(), {}))


def _prime_catalyst_cache(symbols: list[str]) -> None:
    """
    Fetch pre-market gap + volume for all symbols concurrently, store in cache.
    Call once per session after 9:35 AM (enough data for first 5-min bar).
    Non-blocking: individual symbol failures are silently skipped.
    """
    import ibkr_data as _id

    def _fetch(sym):
        try:
            data = _id.get_premarket_gap(sym)
            if data:
                score = 0
                if abs(data.get("gap_pct", 0)) >= CATALYST_MIN_GAP:
                    g = abs(data["gap_pct"])
                    score += 15 if g >= 10 else (10 if g >= 5 else 5)
                if data.get("vol_ratio", 0) >= CATALYST_MIN_VOL:
                    r = data["vol_ratio"]
                    score += 15 if r >= 5 else (10 if r >= 3 else 5)
                data["catalyst_score"] = score
                with _catalyst_lock:
                    _catalyst_cache[sym.upper()] = data
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_fetch, symbols))

    # Rank all catalyst symbols by strength (gap × vol) and assign rank_bonus.
    # Top 3 get +8 pts, next 4 get +5 pts, rest get +2 pts — on top of base catalyst bonus.
    # Also populate _premarket_top_symbols so the scan universe expands to include them.
    global _premarket_top_symbols
    with _catalyst_lock:
        ranked = sorted(
            _catalyst_cache.items(),
            key=lambda kv: abs(kv[1].get("gap_pct", 0)) * kv[1].get("vol_ratio", 0),
            reverse=True,
        )
        for i, (sym, data) in enumerate(ranked):
            data["rank_bonus"] = 8 if i < 3 else (5 if i < 7 else 2)
        _premarket_top_symbols = [sym for sym, _ in ranked[:10]]


def _get_daily_bias(symbol: str) -> dict:
    """Return cached daily bias for symbol. Falls back to neutral on miss."""
    global _bias_cache_date
    today = _et_now().strftime("%Y-%m-%d")
    with _bias_lock:
        if _bias_cache_date != today:
            _bias_cache.clear()
            _bias_cache_date = today
        return dict(_bias_cache.get(symbol.upper(), {"bias": "neutral"}))


def _prime_bias_cache(symbols: list[str]) -> None:
    """
    Fetch 20-day EMA daily bias for all symbols concurrently.
    Called once per session alongside _prime_catalyst_cache().
    """
    import ibkr_data as _id

    def _fetch(sym):
        try:
            data = _id.get_daily_bias(sym)
            with _bias_lock:
                _bias_cache[sym.upper()] = data
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(_fetch, symbols))


# ═══════════════════════════════════════════════════════════════════════════════
# FULL SYMBOL EVALUATION
# ═══════════════════════════════════════════════════════════════════════════════

def evaluate_symbol(symbol, cooldown_tracker, regime_bull):
    """
    Fetch → indicators → score → gate checks.
    Returns a signal dict or None if the symbol doesn't qualify.
    """
    # Wall-clock guard: bar timestamps can lag; always check real ET time so
    # force scans and delayed processing never log after-hours signals.
    now_et = _et_now()
    if not ((now_et.hour > 9 or (now_et.hour == 9 and now_et.minute >= 35)) and now_et.hour < 16):
        return None

    # No new entries after 15:30 — too little time left for the trade to play out
    if now_et.hour == 15 and now_et.minute >= 30:
        return None

    # SPY/QQQ define the regime via the same indicators the scorer uses,
    # so they score 90+ on every bull scan by construction — not a signal.
    # Trade SPY/QQQ directly using ORB or key level rules, not this scanner.
    if symbol.upper() in {"SPY", "QQQ"}:
        return None

    primary, htf = fetch_symbol_data(symbol)
    if not primary:
        return None

    ind = compute_indicators(primary, htf)
    if not ind or not ind["in_session"]:
        return None

    # ── 1H swing structure (IBKR 1-hour bars, last 5 trading days) ───────────
    try:
        h1 = fetch_intraday(symbol, "1h", "5d")
        h1_sh, h1_sl, h1_hh, h1_ll = _compute_1h_swing(h1)
    except Exception:
        h1_sh = h1_sl = None
        h1_hh = h1_ll = False
    ind["h1_swing_high"]  = h1_sh
    ind["h1_swing_low"]   = h1_sl
    ind["h1_higher_highs"] = h1_hh
    ind["h1_lower_lows"]   = h1_ll

    # ── Minimum relative volume gate ──────────────────────────────────────────
    # RVol < 0.5 means the current bar has less than half the average volume.
    # Low-volume "signals" are just drift — no real participation behind them.
    if ind["rel_vol"] < 0.5:
        return None

    # ── Minimum ATR % gate ────────────────────────────────────────────────────
    # ATR < 0.5% of price means the stop is paper-thin → position sizing
    # produces hundreds of shares → small adverse move = large dollar loss.
    # e.g. LCID $5.88, ATR $0.03 → 175 shares → -$84 on a $0.48 move.
    if ind["atr"] / ind["price"] < 0.005:
        return None

    # ── Relative strength vs SPY (inject before scoring) ─────────────────────
    with _regime_cache_lock:
        _rc_snap = dict(_regime_cache)
    spy_chg   = _rc_snap.get("spy_chg", 0.0)
    day_open  = ind.get("open_first") or ind["price"]
    stock_chg = (ind["price"] - day_open) / day_open * 100 if day_open else 0.0
    ind["rs_vs_spy"] = round(stock_chg - spy_chg, 2)

    # ── Earnings blackout ─────────────────────────────────────────────────────
    # edays=None means unknown — don't block.
    # Block 1-3 days before earnings (IV crush / unpredictable gap risk).
    # edays=0 (today) is handled later once we know if there was a BMO gap.
    _, edays = fetch_earnings_date(symbol)
    _post_earnings_gap = False
    if edays is not None and 1 <= edays <= 3:
        return None

    # ── Macro event day flag ──────────────────────────────────────────────────
    today_str = _et_now().strftime("%Y-%m-%d")
    macro_day = today_str in _MACRO_DATES

    # ── Zarattini noise cone ──────────────────────────────────────────────────
    # Compute 14-day average intraday range % → upper/lower bounds around open.
    # ORB bonus is only full-strength if price breaks outside this cone.
    noise_upper = noise_lower = None
    daily = []
    try:
        import ibkr_data as _id
        daily = _id.get_daily_bars(symbol) or []
        if len(daily) >= 5:
            # Bug fix: daily bars are dicts, not tuples — b[2] raised KeyError
            # silently and the noise cone never computed.
            ranges = [(b["high"] - b["low"]) / b["open"]
                      for b in daily[-14:] if b.get("open", 0) > 0]
            if ranges:
                avg_range = sum(ranges) / len(ranges)
                day_open  = ind.get("open_first") or ind["price"]
                noise_upper = round(day_open * (1 + avg_range), 4)
                noise_lower = round(day_open * (1 - avg_range), 4)
    except Exception:
        pass

    # ── L2 top-of-book imbalance ──────────────────────────────────────────────
    # Only fetch when basic signal formation is promising — avoids burning all
    # IBKR connections on dead symbols (each fetch costs ~1-2s + a connection).
    l2_imbalance = None
    _ema9  = ind.get("ema9")
    _ema21 = ind.get("ema21")
    _px    = ind.get("price", 0)
    _vwap  = ind.get("vwap")
    _promising = bool(
        ind.get("or_break_up") or ind.get("or_break_down") or
        (ind.get("adx") or 0) > 25 or
        (_ema9 and _ema21 and _px and _vwap and (
            (_ema9 > _ema21 and _px > _vwap) or
            (_ema9 < _ema21 and _px < _vwap)
        ))
    )
    if _promising:
        try:
            import ibkr_data as _id
            l2_imbalance = _id.get_l2_imbalance(symbol)
        except Exception as _e:
            _logger.warning("L2 fetch failed for %s: %s", symbol, _e)

    # ── Sector ETF alignment ──────────────────────────────────────────────────
    sector_aligned = None
    try:
        etf = _SECTOR_ETF_MAP.get(symbol.upper())
        if etf:
            etf_ret = _rc_snap.get("sector_rets", {}).get(etf)
            stock_chg_local = (ind["price"] - (ind.get("open_first") or ind["price"])) / (ind.get("open_first") or ind["price"]) * 100
            if etf_ret is not None:
                # aligned = stock is keeping up with or outpacing sector (relative strength)
                # direction match alone is not enough — laggards get no bonus
                sector_aligned = (
                    (stock_chg_local > 0 and etf_ret > 0 and stock_chg_local >= etf_ret * 0.5) or
                    (stock_chg_local < 0 and etf_ret < 0 and stock_chg_local <= etf_ret * 0.5)
                )
    except Exception:
        pass

    # ── Score ─────────────────────────────────────────────────────────────────
    bull_raw, bear_raw, bull_r, bear_r, bull_w, bear_w = score_intraday(
        ind,
        noise_upper=noise_upper,
        noise_lower=noise_lower,
        l2_imbalance=l2_imbalance,
        sector_aligned=sector_aligned,
        macro_day=macro_day,
    )

    # Bear market regime penalty (your algo)
    if not regime_bull:
        bull_raw -= 15
        bull_w.append("bear market regime")

    # ── Catalyst bonus ────────────────────────────────────────────────────────
    catalyst = _get_catalyst(symbol)
    cat_score = catalyst.get("catalyst_score", 0)
    if cat_score > 0:
        bonus = min(cat_score, CATALYST_BONUS) + catalyst.get("rank_bonus", 0)
        bull_raw += bonus
        bear_raw += bonus
        gap_str = f"{catalyst.get('gap_pct', 0):+.1f}% gap, {catalyst.get('vol_ratio', 0):.1f}× PM vol"
        rank_str = f" · PM rank #{_premarket_top_symbols.index(symbol.upper()) + 1}" \
                   if symbol.upper() in _premarket_top_symbols else ""
        bull_w.append(f"catalyst {gap_str}{rank_str}")
        bear_w.append(f"catalyst {gap_str}{rank_str}")

    # ── Post-earnings gap detection ───────────────────────────────────────────
    # edays=0  → earnings today; only trade if a big gap proves it was BMO.
    # edays=-1 → earnings yesterday; allow if gap hasn't been fully retested.
    _gap_pct = catalyst.get("gap_pct", 0) or 0.0
    if edays is not None:
        if edays == 0:
            if abs(_gap_pct) < CATALYST_MIN_GAP:
                return None   # No gap = AMC earnings not yet reported — skip
            _post_earnings_gap = True
        elif -2 <= edays < 0 and abs(_gap_pct) >= CATALYST_MIN_GAP:
            _post_earnings_gap = True

    # ── PEAD: post-earnings announcement drift (days +1 to +5) ───────────────
    # Ball & Brown 1968, Bernard & Thomas 1989: after a strong earnings-day
    # reaction, price drifts in the same direction for days. Find the reaction
    # day in the daily bars (largest open-gap within the earnings window) and
    # check whether the move is holding.
    if edays is not None and -5 <= edays <= -1 and len(daily) >= abs(edays) + 2:
        try:
            window = daily[-(abs(edays) + 2):]
            react_gap, react_open = 0.0, None
            for _j in range(1, len(window)):
                _pc = window[_j - 1].get("close", 0)
                _op = window[_j].get("open", 0)
                if _pc > 0 and _op > 0:
                    _g = (_op - _pc) / _pc * 100
                    if abs(_g) > abs(react_gap):
                        react_gap, react_open = _g, _op
            if react_open and abs(react_gap) >= 3.0:
                if react_gap > 0 and ind["price"] > react_open:
                    bull_raw += 10
                    bull_r.append(f"PEAD drift (+{react_gap:.1f}% earnings gap holding, day {abs(edays)})")
                elif react_gap < 0:
                    # Downward drift after a miss — fighting it with CALLs is -EV
                    bull_raw -= 12
                    bull_w.append(f"PEAD downward drift ({react_gap:.1f}% earnings gap, day {abs(edays)})")
        except Exception:
            pass

    # ── Ensure PDH/PDL/PMH/PML always populated ──────────────────────────────
    # _prime_catalyst_cache() may not have run yet (first scan of session,
    # IBKR unavailable, or new symbol added mid-day). Fall back to computing
    # directly from the 5-day bar history already in memory.
    if not catalyst.get("prev_day_high"):
        try:
            _today_d = datetime.fromtimestamp(primary[-1][0], tz=_ET).date()
            _day_map, _pm_bars = {}, []
            for _b in primary:
                _bdt = datetime.fromtimestamp(_b[0], tz=_ET)
                _day_map.setdefault(_bdt.date(), []).append(_b)
                if (_bdt.date() == _today_d and
                        (_bdt.hour < 9 or (_bdt.hour == 9 and _bdt.minute < 30))):
                    _pm_bars.append(_b)
            _prev = sorted((d for d in _day_map if d < _today_d), reverse=True)
            if _prev:
                _pb = _day_map[_prev[0]]
                catalyst["prev_day_high"] = round(max(b[2] for b in _pb), 4)
                catalyst["prev_day_low"]  = round(min(b[3] for b in _pb), 4)
            if _pm_bars:
                catalyst["pm_high"] = round(max(b[2] for b in _pm_bars), 4)
                catalyst["pm_low"]  = round(min(b[3] for b in _pm_bars), 4)
        except Exception:
            pass

    # ── Gap-fill risk detection ───────────────────────────────────────────────
    # If gapped up >1.5% but price is now back near prior close, the gap is
    # filling — institutions who chased the gap are offside and selling.
    if _gap_pct > 1.5:
        _prev_cl = ind.get("prev_close") or 0
        if _prev_cl > 0 and ind["price"] < _prev_cl * 1.005:
            bull_raw -= 15
            bull_w.append(f"gap fill in progress ({_gap_pct:+.1f}% gap reverting)")

    # ── Key level bonus ───────────────────────────────────────────────────────
    # PDH/PDL/PMH/PML are the most-watched levels by institutional traders.
    # Breaking a level = highest-conviction entry. Testing from wrong side = weaker.
    kl_bull, kl_bear, kl_bull_tags, kl_bear_tags = _key_level_bonus(ind["price"], catalyst)
    kl_tags = kl_bull_tags + kl_bear_tags  # combined for signal dict
    kl_bonus = kl_bull  # kept for signal dict (bull bonus)
    if kl_bull > 0:
        bull_raw += kl_bull
        bull_r.extend(kl_bull_tags)
    if kl_bear > 0:
        bear_raw += kl_bear
        bear_r.extend(kl_bear_tags)

    # ── Anchored VWAP (AVWAP) from prior-day high ─────────────────────────────
    # When price breaks and holds above the prior-day high, institutional buyers
    # entering there set a new "fair value" floor. AVWAP tracks their aggregate
    # cost basis — price staying above it = they're still in profit = no sell pressure.
    pdh = catalyst.get("prev_day_high")
    avwap = None
    if pdh:
        avwap = _calc_anchored_vwap(primary, pdh)
        if avwap:
            _cur_p = ind["price"]
            if _cur_p > avwap:
                bull_raw += 8;  bull_r.append(f"above AVWAP-from-PDH (${avwap:.2f})")
            elif _cur_p > avwap * (1 - 0.003):
                bull_raw += 3;  bull_r.append(f"testing AVWAP-from-PDH (${avwap:.2f})")
            else:
                bull_raw -= 5;  bull_w.append(f"below AVWAP-from-PDH (${avwap:.2f}) — weak hands")

    # ── Daily bias (20-day EMA on daily chart) ────────────────────────────────
    # Aligns with Chloe's strategy: only trade CALL on stocks in daily uptrend.
    # Penalty for downtrend = forces a stronger 5-min signal to still pass.
    daily_bias = _get_daily_bias(symbol)
    db = daily_bias.get("bias", "neutral")
    if db == "bear":
        bull_raw -= DAILY_BIAS_PENALTY
        bull_w.append(f"daily downtrend (below EMA20 ${daily_bias.get('ema20', '?')})")
    elif db == "bull":
        bull_raw += DAILY_BIAS_BONUS
        bull_r.append(f"daily uptrend (above EMA20 ${daily_bias.get('ema20', '?')})")

    # ── IV Rank gate — options pricing check (IBKR OPTION_IMPLIED_VOLATILITY) ──
    # High IV rank = options are expensive relative to history = IV crush risk.
    # Best CALL entries have IV rank < 50 (cheap options, vol expansion adds to P&L).
    _ivr = None
    try:
        import ibkr_data as _id
        _ivr = _id.get_iv_rank(symbol)
        if _ivr:
            ivr = _ivr["iv_rank"]
            iv_cur = _ivr["iv_current"]
            if ivr >= 70:
                bull_raw -= 20
                bull_w.append(f"IV rank {ivr:.0f} — options expensive, IV crush risk ({iv_cur:.0f}%)")
            elif ivr >= 50:
                bull_raw -= 10
                bull_w.append(f"IV rank {ivr:.0f} — elevated premium ({iv_cur:.0f}%)")
            elif ivr <= 25:
                bull_raw += 6
                bull_r.append(f"IV rank {ivr:.0f} — cheap options, vol expansion upside ({iv_cur:.0f}%)")
    except Exception:
        pass

    bull_prob = max(0.0, min(100.0, bull_raw))
    bear_prob = max(0.0, min(100.0, bear_raw))

    # ── CALL-only mode ────────────────────────────────────────────────────────
    # Live data: PUT trades PF=0.03 on 17 trades — disabled permanently.
    # Bear regime: no new entries (cash preservation) rather than PUT signals.
    bear_prob = 0.0
    if not regime_bull:
        bull_prob = 0.0

    # ── Direction gate ────────────────────────────────────────────────────────
    if bull_prob >= SIGNAL_THRESHOLD:
        direction = "CALL"
        score     = bull_prob
        reasons   = bull_r
        warnings  = bull_w
    else:
        # ── Watching tier: score 75-89, not strong enough to trade yet ────────
        if bull_prob >= WATCHING_THRESHOLD:
            return {
                "watching":  True,
                "symbol":    symbol,
                "direction": "CALL",
                "score":     round(bull_prob, 1),
                "entry":     ind["price"],
                "reasons":   bull_r,
                "ts":        time.time(),
            }
        return None

    # ── Cooldown gate ─────────────────────────────────────────────────────────
    now_ts = time.time()
    last   = cooldown_tracker.get(symbol, 0)
    if now_ts - last < COOLDOWN_MIN * 60:
        return None

    # ── Stop / target / R:R  (widen when VIX is high) ────────────────────────
    price = ind["price"]
    atr   = ind["atr"]
    vix   = _rc_snap.get("vix")

    vix_factor  = VIX_HIGH_MULT if (vix and vix > VIX_HIGH_THRESHOLD) else 1.0
    stop_mult   = ATR_STOP_MULT   * vix_factor
    target_mult = ATR_TARGET_MULT * vix_factor

    if direction == "CALL":
        stop   = round(price - atr * stop_mult,   2)
        target = round(price + atr * target_mult, 2)
    else:
        stop   = round(price + atr * stop_mult,   2)
        target = round(price - atr * target_mult, 2)

    risk   = abs(price - stop)
    reward = abs(target - price)
    rr     = round(reward / max(risk, 0.01), 2)

    if rr < 1.5:
        return None

    if vix and vix > VIX_HIGH_THRESHOLD:
        warnings.append(f"HIGH VIX {vix:.0f} — stops widened ×{VIX_HIGH_MULT}")

    # ── Pullback entry check (EMA9 / VWAP) ───────────────────────────────────
    ema9_val  = ind.get("ema9")  or 0
    vwap_val  = ind.get("vwap") or 0
    near_ema9 = ema9_val  and abs(price - ema9_val)  / ema9_val  <= 0.003
    near_vwap = vwap_val  and abs(price - vwap_val)  / vwap_val  <= 0.003
    pullback_entry = bool(near_ema9 or near_vwap)
    if not pullback_entry:
        warnings.append(f"extended — ideal entry near EMA9 ${ema9_val:.2f} or VWAP ${vwap_val:.2f}")

    # ── Chloe Protocol: weekly bias, confluence, level pullback ───────────────
    weekly_bias   = _weekly_bias(primary)
    confluence    = _level_confluence(catalyst)
    lvl_pb, lvl_pb_tag = _level_pullback(primary, catalyst, direction)

    # Score bonuses — additive, non-breaking
    base_score = round(score, 1)
    bonuses = []

    if weekly_bias == ("bull" if direction == "CALL" else "bear"):
        score += 5
        bonuses.append({"label": "Trend alignment", "pts": +5})
        reasons.append(f"10d trend {weekly_bias} — bias aligned")
    elif weekly_bias == ("bear" if direction == "CALL" else "bull"):
        score -= 3
        bonuses.append({"label": "Contra-trend penalty", "pts": -3})
        warnings.append(f"contra-trend — 10d bias is {weekly_bias}")

    if confluence:
        pts = 8 * min(len(confluence), 2)
        score += pts
        bonuses.append({"label": f"Level confluence ({len(confluence[:2])}×)", "pts": pts})
        reasons.append("level confluence: " + ", ".join(confluence[:2]))

    if lvl_pb:
        score += 12
        bonuses.append({"label": "Chloe pullback entry", "pts": +12})
        pullback_entry = True
        reasons.append(f"✓ Chloe entry: {lvl_pb_tag}")
    elif not lvl_pb and not pullback_entry:
        warnings.append("no level pullback yet — wait for price to return to key level")

    # Post-earnings gap pullback bonus — direction must match the gap
    if _post_earnings_gap and lvl_pb:
        _gap_dir_match = (direction == "CALL" and _gap_pct > 0) or (direction == "PUT" and _gap_pct < 0)
        if _gap_dir_match:
            score += 10
            bonuses.append({"label": "post-earnings gap pullback", "pts": +10})
            reasons.append(f"✓ earnings gap {_gap_pct:+.1f}% — pulling back to key level")

    # ── Resistance/support proximity gate ────────────────────────────────────
    # Block CALLs within 0.5% of overhead resistance (PDH, PMH)
    # Block PUTs within 0.5% of underlying support (PDL, PML)
    # This prevents entering momentum trades that have no room to run.
    _pdh = catalyst.get("prev_day_high")
    _pdl = catalyst.get("prev_day_low")
    _pmh = catalyst.get("pm_high")
    _pml = catalyst.get("pm_low")
    _PROXIMITY = 0.010   # 1.0% — need room to run before hitting resistance

    if direction == "CALL":
        for _lvl, _name in [(_pdh, "PDH"), (_pmh, "PMH")]:
            if _lvl and price > 0 and (_lvl - price) / price < _PROXIMITY and price < _lvl:
                score -= 15
                bonuses.append({"label": f"Near {_name} resistance", "pts": -15})
                warnings.append(f"⚠ within 0.5% of {_name} ${_lvl:.2f} — little room to run")
    else:  # PUT
        for _lvl, _name in [(_pdl, "PDL"), (_pml, "PML")]:
            if _lvl and price > 0 and (price - _lvl) / price < _PROXIMITY and price > _lvl:
                score -= 15
                bonuses.append({"label": f"Near {_name} support", "pts": -15})
                warnings.append(f"⚠ within 0.5% of {_name} ${_lvl:.2f} — little room to run")

    # ── Position sizing (1% risk rule; halved on macro days) ─────────────────
    shares     = _position_size(price, stop)
    if macro_day:
        shares = max(1, shares // 2)
        warnings.append("macro day — position halved")
    risk_dollar = round(abs(price - stop) * shares, 2)

    # ── Feature snapshot — ML-ready training data ─────────────────────────────
    # Freeze every indicator at entry time. With outcomes attached at close,
    # each trade becomes a labeled training example. Target: n ≥ 100 trades,
    # then run feature-importance to find which inputs actually predict P&L.
    def _r(x, nd=4):
        try:    return round(float(x), nd)
        except: return None
    _vw, _sg = ind.get("vwap") or 0, ind.get("vwap_sigma") or 0
    features = {
        "rsi":            _r(ind.get("rsi"), 1),
        "adx":            _r(ind.get("adx"), 1),
        "plus_di":        _r(ind.get("plus_di"), 1),
        "minus_di":       _r(ind.get("minus_di"), 1),
        "atr_pct":        _r(atr / price * 100 if price else None),
        "rel_vol":        _r(ind.get("rel_vol"), 2),
        "cmf":            _r(ind.get("cmf")),
        "mfi":            _r(ind.get("mfi"), 1),
        "obv_trend":      ind.get("obv"),
        "bb_width":       _r(ind.get("bb_width")),
        "bb_squeezed":    bool(ind.get("bb_squeezed")),
        "vwap_dist_pct":  _r((price - _vw) / _vw * 100 if _vw else None),
        "vwap_z":         _r((price - _vw) / _sg if _sg else None, 2),
        "ema_spread_pct": _r((ind.get("ema9", 0) - ind.get("ema21", 0)) /
                             (ind.get("ema21") or 1) * 100),
        "or_break_up":    bool(ind.get("or_break_up")),
        "htf_bull":       bool(ind.get("htf_bull")),
        "h1_higher_highs": bool(ind.get("h1_higher_highs")),
        "above_avwap_pdh": (price > avwap) if avwap else None,
        "rs_vs_spy":      _r(ind.get("rs_vs_spy"), 2),
        "spy_chg":        _r(_rc_snap.get("spy_chg"), 2),
        "vix":            _r(_rc_snap.get("vix"), 1),
        "first30_ret":    _r(_rc_snap.get("first30_ret"), 3),
        "sector_aligned": sector_aligned,
        "l2_imbalance":   _r(l2_imbalance, 3),
        "gap_pct":        _r(_gap_pct, 2),
        "catalyst_score": catalyst.get("catalyst_score", 0),
        "daily_bias":     db,
        "edays":          edays,
        "iv_rank":        _r(_ivr.get("iv_rank") if _ivr else None, 1),
        "macro_day":      macro_day,
        "post_earn_gap":  _post_earnings_gap,
        "hour":           _et_now().hour,
        "minute":         _et_now().minute,
    }

    return {
        "features":      features,
        "symbol":        symbol,
        "direction":     direction,
        "grade":         _grade(score),
        "score":         round(min(score, 100.0), 1),
        # Un-clamped score: every live trade so far clamped at 90-100, making the
        # threshold blind. Raw values (can exceed 140) restore discrimination for
        # future calibration — analyze score_raw vs pnl once n ≥ 50.
        "score_raw":     round(bull_raw, 1),
        "base_score":    base_score,
        "bonuses":       bonuses,
        "entry":         price,
        "stop":          stop,
        "target":        target,
        "rr":            rr,
        "atr":           round(atr, 4),
        "adx":           ind["adx"],
        "rsi":           ind["rsi"],
        "rel_vol":       ind["rel_vol"],
        "vwap":          ind["vwap"],
        "rs_vs_spy":     ind["rs_vs_spy"],
        "l2_imbalance":  l2_imbalance,
        "macro_day":     macro_day,
        "catalyst_score": cat_score,
        "gap_pct":        catalyst.get("gap_pct"),
        "pm_vol_ratio":   catalyst.get("vol_ratio"),
        "key_level":      kl_tags[0] if kl_tags else None,
        "key_bonus":      kl_bonus,
        "pdh":            catalyst.get("prev_day_high"),
        "pdl":            catalyst.get("prev_day_low"),
        "pm_high":        catalyst.get("pm_high"),
        "pm_low":         catalyst.get("pm_low"),
        "level_break":    (
            "PDH" if any("broke PDH" in t for t in kl_bull_tags) else
            "PMH" if any("broke PMH" in t for t in kl_bull_tags) else
            "PDL" if any("broke PDL" in t for t in kl_bear_tags) else
            "PML" if any("broke PML" in t for t in kl_bear_tags) else
            "PDH_test" if any("testing PDH" in t for t in kl_bull_tags) else
            "PDL_hold" if any("PDL support" in t for t in kl_bull_tags) else
            None
        ),
        "shares":        shares,
        "risk_dollar":   risk_dollar,
        "reasons":        reasons[:6],
        "warnings":       warnings[:4],
        "pullback_entry": pullback_entry,
        "weekly_bias":    weekly_bias,
        "confluence":     confluence,
        "level_pullback": lvl_pb,
        "level_pullback_tag": lvl_pb_tag,
        "chloe_setup": lvl_pb and weekly_bias == ("bull" if direction == "CALL" else "bear"),
        "post_earnings_gap": _post_earnings_gap,
        "swing_target": (
            # CALL: highest key level above day target (PMH preferred, then PDH)
            next((lvl for lvl in sorted(
                [v for v in [catalyst.get("pm_high"), catalyst.get("prev_day_high")] if v and v > target],
                reverse=True
            ) if lvl), None)
            if direction == "CALL" and lvl_pb and weekly_bias == "bull"
            else
            # PUT: lowest key level below day target (PML preferred, then PDL)
            next((lvl for lvl in sorted(
                [v for v in [catalyst.get("pm_low"), catalyst.get("prev_day_low")] if v and v < target]
            ) if lvl), None)
            if direction == "PUT" and lvl_pb and weekly_bias == "bear"
            else None
        ),
        "h1_swing_high":  ind.get("h1_swing_high"),
        "h1_swing_low":   ind.get("h1_swing_low"),
        "time":           _et_now().strftime("%H:%M"),
        "ts":             now_ts,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# INTRADAY MARKET REGIME
# ═══════════════════════════════════════════════════════════════════════════════

def intraday_regime():
    """
    Bull if SPY's 5-min price is above session VWAP AND EMA9>EMA21.
    Also caches VIX, SPY % change, first-30-min direction, and sector ETF
    returns into _regime_cache for use by score_intraday / evaluate_symbol.
    Falls back to daily regime if data is thin.
    """
    # VIX (fire-and-forget; failure is acceptable)
    vix = _fetch_vix()
    with _regime_cache_lock:
        _regime_cache["vix"] = vix

    bars, _ = fetch_symbol_data("SPY", use_ibkr=True)
    if len(bars) < 21:
        bull, label = market_regime()
        with _regime_cache_lock:
            _regime_cache["spy_chg"]      = 0.0
            _regime_cache["first30_bull"] = None
            _regime_cache["first30_ret"]  = 0.0
        return bull, f"daily fallback: {label}"

    # Separate today's bars from cross-day history
    last_et    = datetime.fromtimestamp(bars[-1][0], tz=_ET)
    today_date = last_et.date()
    today_spy  = [b for b in bars if datetime.fromtimestamp(b[0], tz=_ET).date() == today_date]

    ops = [b[1] for b in bars]
    his = [b[2] for b in bars]
    los = [b[3] for b in bars]
    cls = [b[4] for b in bars]
    vls = [b[5] for b in bars]

    # VWAP must use only today's bars — it resets each session.
    # Bug: was passing full 5-day history, giving a meaningless multi-day cumulative.
    t_his = [b[2] for b in today_spy] or his
    t_los = [b[3] for b in today_spy] or los
    t_cls = [b[4] for b in today_spy] or cls
    t_vls = [b[5] for b in today_spy] or vls
    vwap  = _calc_vwap(t_his, t_los, t_cls, t_vls)

    ema9  = _calc_ema(cls, 9)  or 0
    ema21 = _calc_ema(cls, 21) or 0
    price = cls[-1]

    # SPY % change from today's open (not the oldest bar in the 5-day window).
    # Bug: was using ops[0] = open of 5-day-ago bar as the baseline.
    spy_today_open = today_spy[0][1] if today_spy else (ops[-1] if ops else 0)
    spy_chg = (price - spy_today_open) / spy_today_open * 100 if spy_today_open else 0.0

    # ── First-30-min SPY alignment (Gao et al. JFE 2018) ─────────────────────
    # First 6 × 5-min bars = first 30 min. If SPY is up in that window,
    # momentum tends to persist into the last 30 min; we bonus aligned longs.
    try:
        if len(today_spy) >= 6:
            open_px  = today_spy[0][1]
            close_30 = today_spy[5][4]  # close of bar 6 (bar index 5)
            ret_30   = (close_30 - open_px) / open_px * 100 if open_px else 0.0
            f30_bull = ret_30 > 0
            f30_ret  = round(ret_30, 3)
        else:
            f30_bull = None
            f30_ret  = 0.0
    except Exception:
        f30_bull = None
        f30_ret  = 0.0

    # ── Sector ETF % change from open ────────────────────────────────────────
    def _etf_chg(etf):
        try:
            etf_bars = fetch_intraday(etf, "5m", "1d")
            if not etf_bars:
                return None
            etf_today = [b for b in etf_bars
                         if datetime.fromtimestamp(b[0], tz=_ET).date() == today_date]
            if len(etf_today) < 2:
                return None
            o = etf_today[0][1]
            c = etf_today[-1][4]
            return round((c - o) / o * 100, 3) if o else None
        except Exception:
            return None

    sector_rets = {}
    for etf in _SECTOR_ETFS:
        r = _etf_chg(etf)
        if r is not None:
            sector_rets[etf] = r

    bull  = (price > vwap) and (ema9 > ema21)
    label = ("🟢 intraday bull — SPY above VWAP + EMA9>21"
             if bull else
             "🔴 intraday bear — SPY below VWAP or EMA9<21")

    if vix and vix > VIX_HIGH_THRESHOLD:
        label += f"  ⚡ HIGH VIX {vix:.0f}"

    if f30_ret:
        label += f"  first-30m SPY: {f30_ret:+.2f}%"

    # Commit all updates atomically so evaluate_symbol threads see a consistent snapshot
    with _regime_cache_lock:
        _regime_cache["spy_chg"]      = round(spy_chg, 2)
        _regime_cache["first30_bull"] = f30_bull
        _regime_cache["first30_ret"]  = f30_ret
        _regime_cache["sector_rets"]  = sector_rets

    return bull, label


# ═══════════════════════════════════════════════════════════════════════════════
# PAPER TRADE LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

def _load_day_trades():
    if os.path.exists(PAPER_FILE):
        with open(PAPER_FILE) as f:
            return json.load(f)
    return {"trades": []}


_day_trades_lock = threading.Lock()

def _save_day_trades(pt):
    # Write to a temp file then atomically rename so concurrent readers never
    # see a partial write.
    tmp = PAPER_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pt, f, indent=2)
    os.replace(tmp, PAPER_FILE)

def _load_day_trades_locked():
    with _day_trades_lock:
        return _load_day_trades()


_GRADE_RANK = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0}


def stops_today_count():
    """Count today's real stop-outs (breakeven scratches excluded) for the
    circuit breaker. Shared by the standalone scan loop and api.py's scanner."""
    try:
        with _day_trades_lock:
            pt = _load_day_trades()
        today = _et_now().strftime("%Y-%m-%d")
        return sum(
            1 for t in pt["trades"]
            if t.get("date") == today
            and (t.get("close_reason") == "stop_hit" or
                 (t.get("status") == "lost" and not t.get("close_reason")))
        )
    except Exception:
        return 0


def check_open_trades():
    """
    Run once per scan cycle. Closes any open trade whose stop or target
    was crossed in the latest 5-min bar, or everything at 3:55pm EOD.

    Logic per bar (uses high/low, not just close — price can cross a level
    intrabar and close back through it):
      CALL: target hit if bar_high >= target
            stop  hit if bar_low  <= stop
      PUT:  target hit if bar_low  <= target
            stop  hit if bar_high >= stop
      Both crossed same bar → stop wins (conservative).
      3:55pm ET → EOD close at bar_close regardless.

    Returns list of newly closed trade dicts (for notification).
    """
    now_et  = _et_now()
    today   = now_et.strftime("%Y-%m-%d")
    eod     = (now_et.hour == 15 and now_et.minute >= 55)
    closed  = []

    with _day_trades_lock:
        pt      = _load_day_trades()
        changed = False

        for t in pt["trades"]:
            if t.get("status") != "open" or t.get("date") != today:
                continue

            sym    = t["symbol"]
            entry  = t["entry"]
            stop   = t["stop"]
            target = t["target"]
            direct = t["direction"]
            shares = t.get("shares", 0)

            try:
                # IBKR only — real-time data, no Yahoo corrupt bars for trade monitoring
                import ibkr_data as _id
                bars = _id.get_intraday_bars(sym, "5m", "1d", prefer_yahoo=False)
                if not bars:
                    bars = fetch_intraday(sym, "5m", "1d", yahoo_only=True)
                if not bars:
                    continue
                clean_bars = [b for b in bars if b[1] and b[2] and b[3] and b[4]
                              and abs(b[1] - (b[2]+b[3])/2) / ((b[2]+b[3])/2) < 0.5]
                if not clean_bars:
                    continue

                # Only look at bars since signal entry time (catches stops hit while scanner was paused)
                entry_time_str = t.get("time", "00:00")
                try:
                    _hh, _mm = map(int, entry_time_str.split(":"))
                    entry_ts = int(now_et.replace(hour=_hh, minute=_mm, second=0, microsecond=0).timestamp())
                    bars_since_entry = [b for b in clean_bars if b[0] >= entry_ts]
                except Exception:
                    bars_since_entry = clean_bars
                if not bars_since_entry:
                    bars_since_entry = clean_bars

                last      = clean_bars[-1]
                bar_close = last[4]
            except Exception:
                continue

            # ── MFE/MAE excursion tracking ────────────────────────────────────
            # Max favorable / adverse move since entry, persisted every cycle.
            # After 2 weeks this answers "where should the target actually be"
            # with real data instead of the ATR×3 guess (1/40 trades ever hit it).
            if bars_since_entry and entry > 0:
                _hi = max(b[2] for b in bars_since_entry)
                _lo = min(b[3] for b in bars_since_entry)
                if direct == "CALL":
                    _mfe = round((_hi - entry) / entry * 100, 3)
                    _mae = round((_lo - entry) / entry * 100, 3)
                else:
                    _mfe = round((entry - _lo) / entry * 100, 3)
                    _mae = round((entry - _hi) / entry * 100, 3)
                if _mfe != t.get("mfe_pct") or _mae != t.get("mae_pct"):
                    t["mfe_pct"], t["mae_pct"] = _mfe, _mae
                    changed = True

            exit_price = None
            status     = None
            reason     = None

            if eod:
                exit_price = bar_close
                status     = "closed"
                reason     = "eod"
            elif direct == "CALL":
                # Walk bars chronologically with a stop that ratchets to breakeven
                # once price has run +1R in our favor. Arming takes effect on the
                # NEXT bar (intra-bar order is unknowable — stay conservative).
                be_trigger = entry + (entry - stop) * BREAKEVEN_AT_R
                eff_stop   = max(stop, entry) if t.get("be_armed") else stop
                for b in bars_since_entry:
                    b_low, b_high = b[3], b[2]
                    if b_low <= eff_stop and b_high >= target:
                        exit_price = eff_stop; status = "lost"  # conservative: stop wins tie
                        reason = "breakeven_stop" if eff_stop >= entry else "stop_hit"
                        break
                    elif b_high >= target:
                        exit_price = target; status = "won"; reason = "target_hit"; break
                    elif b_low <= eff_stop:
                        exit_price = eff_stop
                        status = "lost"
                        reason = "breakeven_stop" if eff_stop >= entry else "stop_hit"
                        break
                    # Arm breakeven for subsequent bars
                    if not t.get("be_armed") and b_high >= be_trigger:
                        t["be_armed"]     = True
                        t["stop_initial"] = stop
                        t["stop"]         = round(entry, 4)   # persisted: UI + validate see it
                        eff_stop          = entry
                        changed           = True
            else:  # PUT (legacy — no new PUT entries in CALL-only mode)
                for b in bars_since_entry:
                    b_low, b_high = b[3], b[2]
                    if b_high >= stop and b_low <= target:
                        exit_price = stop; status = "lost"; reason = "stop_hit"; break
                    elif b_low <= target:
                        exit_price = target; status = "won"; reason = "target_hit"; break
                    elif b_high >= stop:
                        exit_price = stop; status = "lost"; reason = "stop_hit"; break

            if exit_price is not None:
                pnl_pct    = ((exit_price - entry) / entry * 100
                              if direct == "CALL"
                              else (entry - exit_price) / entry * 100)
                pnl_dollar = round(pnl_pct / 100 * entry * shares, 2)
                t.update({
                    "status":       status,
                    "exit_price":   round(exit_price, 4),
                    "exit_time":    now_et.strftime("%H:%M"),
                    "close_reason": reason,
                    "pnl_pct":      round(pnl_pct, 3),
                    "pnl_dollar":   pnl_dollar,
                })
                changed = True
                closed.append(t)

                # Close options position on IBKR paper when underlying exits
                _opt_ibkr = t.get("options_ibkr")
                if _opt_ibkr and _opt_ibkr.get("con_id"):
                    try:
                        import ibkr_data as _id
                        _closed = _id.close_options_position(
                            con_id  = _opt_ibkr["con_id"],
                            symbol  = _opt_ibkr["symbol"],
                            right   = _opt_ibkr["right"],
                            strike  = _opt_ibkr["strike"],
                            expiry  = _opt_ibkr["expiry"],
                            qty     = _opt_ibkr.get("qty", 1),
                        )
                        if _closed:
                            print(f"  📋 Options position closed: {sym} "
                                  f"{_opt_ibkr['direction']} ${_opt_ibkr['strike']} "
                                  f"— underlying {status} @ ${exit_price}")
                    except Exception as _e:
                        print(f"  ⚠️  Options close failed for {sym}: {_e}")

        if changed:
            _save_day_trades(pt)

    return closed

def log_signal(sig):
    """
    Write signal to day_trades.json.
    If the same symbol+direction already exists today, upsert only when the
    new score is strictly higher (keeps price/stop/target current).
    Only-open trades are ever upserted; closed trades are left untouched.
    The entire load-modify-save is held under _day_trades_lock so concurrent
    scanner threads and API reads never see a partial file.
    """
    with _day_trades_lock:
        return _log_signal_unsafe(sig)

def _log_signal_unsafe(sig):
    pt     = _load_day_trades()
    today  = _et_now().strftime("%Y-%m-%d")
    now_ts = time.time()

    # Cross-process per-symbol cooldown — block any direction flip within window.
    # Both day_trading.py and api.py write to the same JSON so we check the file
    # rather than the in-memory tracker, which is process-local.
    for existing in pt["trades"]:
        if existing.get("symbol") == sig["symbol"] and existing.get("date") == today:
            saved_ts = existing.get("ts", 0)
            if saved_ts and (now_ts - saved_ts) < COOLDOWN_MIN * 60:
                return False  # same symbol fired recently regardless of direction

    # ── Portfolio heat + sector concentration gates ──────────────────────────
    # 2026-05-26: NET, SNOW, DDOG opened simultaneously — three cloud-software
    # names is one correlated bet taken three times. Cap total open trades and
    # open trades per sector ETF bucket.
    _open_today = [t for t in pt["trades"]
                   if t.get("date") == today and t.get("status") == "open"
                   and t.get("symbol") != sig["symbol"]]
    if len(_open_today) >= MAX_OPEN_TRADES:
        _logger.info("heat cap: %d open trades — rejecting %s",
                     len(_open_today), sig["symbol"])
        return False
    _sig_etf = _SECTOR_ETF_MAP.get(sig["symbol"].upper())
    if _sig_etf:
        _same_sector = sum(1 for t in _open_today
                           if _SECTOR_ETF_MAP.get(t.get("symbol", "").upper()) == _sig_etf)
        if _same_sector >= MAX_OPEN_PER_SECTOR:
            _logger.info("sector cap: %d open in %s — rejecting %s",
                         _same_sector, _sig_etf, sig["symbol"])
            return False

    for existing in pt["trades"]:
        if (existing["symbol"]    == sig["symbol"] and
                existing["direction"] == sig["direction"] and
                existing["date"]      == today):
            if existing.get("status") != "open":
                return False  # closed trade — never overwrite
            if sig["score"] <= existing["score"]:
                return False  # not an improvement — skip
            # upgrade in place
            existing.update({
                "time":        sig["time"],
                "grade":       sig["grade"],
                "score":       sig["score"],
                "entry":       sig["entry"],
                "stop":        sig["stop"],
                "target":      sig["target"],
                "rr":          sig["rr"],
                "shares":      sig.get("shares", 0),
                "risk_dollar": sig.get("risk_dollar", 0),
                "rs_vs_spy":   sig.get("rs_vs_spy", 0.0),
                "rel_vol":     sig.get("rel_vol", None),
                "reasons":     sig.get("reasons", [])[:5],
                "warnings":    sig.get("warnings", [])[:3],
            })
            _save_day_trades(pt)
            return True

    opt = option_suggestion(sig)
    pt["trades"].append({
        "date":        today,
        "time":        sig["time"],
        "symbol":      sig["symbol"],
        "direction":   sig["direction"],
        "grade":       sig["grade"],
        "score":       sig["score"],
        "entry":       sig["entry"],
        "stop":        sig["stop"],
        "target":      sig["target"],
        "rr":          sig["rr"],
        "shares":      sig.get("shares", 0),
        "risk_dollar": sig.get("risk_dollar", 0),
        "rs_vs_spy":   sig.get("rs_vs_spy", 0.0),
        "rel_vol":     sig.get("rel_vol", None),
        "score_raw":   sig.get("score_raw"),
        "features":    sig.get("features", {}),
        # Full lists — truncation was silently dropping tags from the
        # BY SIGNAL TAG analysis in intraday_validate.py
        "reasons":     sig.get("reasons", []),
        "warnings":    sig.get("warnings", []),
        "ts":          sig.get("ts", now_ts),
        "status":      "open",
        "exit_price":  None,
        "exit_time":   None,
        "pnl_pct":     None,
        "pnl_dollar":  None,
        "option":      opt,
    })
    _save_day_trades(pt)
    _update_best_signal(sig)

    # Auto-place options order on IBKR paper account
    try:
        import ibkr_data as _id
        _opt = _id.place_options_order(
            sig["symbol"],
            direction=sig.get("direction", "CALL"),
            qty=1,
            dte_min=1,
            spot=sig.get("entry", 0.0),
        )
        if _opt:
            # Store contract details in trade record for exit hook.
            # Already inside _day_trades_lock (held by log_signal) — update pt directly.
            for _t in reversed(pt["trades"]):
                if (_t["symbol"] == sig["symbol"] and
                        _t["direction"] == sig["direction"] and
                        _t["status"] == "open"):
                    _t["options_ibkr"] = _opt
                    break
            _save_day_trades(pt)
            print(f"  📋 Options order on IBKR paper: BUY 1 {_opt['symbol']} "
                  f"{_opt['direction']} ${_opt['strike']} exp {_opt['expiry']}  "
                  f"est ${_opt['est_price']}")
        else:
            print(f"  ℹ️  No liquid options contract found for {sig['symbol']} — skipped")
    except Exception as _e:
        print(f"  ⚠️  Options order failed: {_e}")

    return True


# ═══════════════════════════════════════════════════════════════════════════════
# OPTIONS SUGGESTION  (Black-Scholes, no external deps)
# ═══════════════════════════════════════════════════════════════════════════════

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _bs_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)

def _bs_put(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

def _atm_strike(price: float) -> float:
    if price < 20:    increment = 0.5
    elif price < 50:  increment = 1.0
    elif price < 200: increment = 2.5
    else:             increment = 5.0
    return round(round(price / increment) * increment, 2)

def _next_otm_strike(price: float, direction: str) -> float:
    """First strike OTM from current price — for swing option suggestions."""
    if price < 20:    inc = 0.5
    elif price < 50:  inc = 1.0
    elif price < 200: inc = 2.5
    else:             inc = 5.0
    atm = _atm_strike(price)
    return round(atm + inc if direction == "CALL" else atm - inc, 2)

def _next_expiry(min_dte: int = 7) -> date:
    today      = date.today()
    days_ahead = (4 - today.weekday()) % 7   # next Friday
    if days_ahead < min_dte:
        days_ahead += 7
    candidate  = today + timedelta(days=days_ahead)
    while (candidate - today).days < min_dte:
        candidate += timedelta(days=7)
    return candidate

_iv_cache: dict = {}          # {symbol: (date_str, iv_float)}
_iv_cache_lock = threading.Lock()

def _get_atm_iv(symbol: str, price: float) -> "float | None":
    """ATM implied vol from yfinance options chain. Cached per calendar day."""
    today = date.today().isoformat()
    with _iv_cache_lock:
        if symbol in _iv_cache and _iv_cache[symbol][0] == today:
            return _iv_cache[symbol][1]
    try:
        import yfinance as _yf
        tk = _yf.Ticker(symbol)
        expiries = tk.options
        if not expiries:
            return None
        target_str = _next_expiry(min_dte=1).strftime("%Y-%m-%d")
        exp = next((e for e in expiries if e >= target_str), expiries[0])
        chain = tk.option_chain(exp)
        calls = chain.calls
        if calls is None or calls.empty:
            return None
        atm_idx = (calls["strike"] - price).abs().idxmin()
        iv = float(calls.loc[atm_idx, "impliedVolatility"])
        if iv <= 0:
            return None
        with _iv_cache_lock:
            _iv_cache[symbol] = (today, iv)
        return iv
    except Exception:
        return None


def option_suggestion(sig: dict) -> dict:
    """
    Return the specific option to buy for a signal.
    Uses live ATM IV from yfinance; falls back to ATR-derived estimate.
    """
    price  = sig["entry"]
    symbol = sig.get("symbol", "")
    # Try live IV first — ATR-derived vol is inaccurate for large-caps
    sigma = _get_atm_iv(symbol, price) if symbol else None
    if sigma is None or sigma <= 0:
        atr   = abs(sig["entry"] - sig["stop"]) / ATR_STOP_MULT
        sigma = (atr / price) * math.sqrt(252) if price > 0 else 0.35
    sigma  = max(0.15, min(sigma, 4.0))

    strike = _atm_strike(price)
    expiry = _next_expiry(min_dte=2)
    dte    = (expiry - date.today()).days
    T      = dte / 365.0

    direction = sig.get("direction", "CALL")
    pricer    = _bs_call if direction == "CALL" else _bs_put
    est_price = pricer(price, strike, T, 0.05, sigma)
    cost      = round(est_price * 100, 2)

    return {
        "type":       direction,
        "strike":     strike,
        "expiry":     expiry.strftime("%b %d"),
        "dte":        dte,
        "est_price":  round(est_price, 2),
        "cost":       cost,
        "contracts":  1,
        "sigma_pct":  round(sigma * 100, 1),
        "affordable": cost <= MAX_OPTION_SPEND,
    }


def option_suggestion_swing(sig: dict) -> dict:
    """
    Swing-trade option: 7-14 DTE, 1 strike OTM.
    Cheaper than ATM, more leverage, still moves meaningfully over 3-10 days.
    """
    price     = sig["entry"]
    symbol    = sig.get("symbol", "")
    direction = sig.get("direction", "CALL")
    sigma     = _get_atm_iv(symbol, price) if symbol else None
    if sigma is None or sigma <= 0:
        atr   = abs(sig["entry"] - sig["stop"]) / ATR_STOP_MULT
        sigma = (atr / price) * math.sqrt(252) if price > 0 else 0.35
    sigma  = max(0.15, min(sigma, 4.0))

    strike = _next_otm_strike(price, direction)
    expiry = _next_expiry(min_dte=7)
    dte    = (expiry - date.today()).days
    T      = dte / 365.0

    pricer    = _bs_call if direction == "CALL" else _bs_put
    est_price = pricer(price, strike, T, 0.05, sigma)
    cost      = round(est_price * 100, 2)

    return {
        "type":      direction,
        "trade_type": "swing",
        "strike":    strike,
        "expiry":    expiry.strftime("%b %d"),
        "dte":       dte,
        "est_price": round(est_price, 2),
        "cost":      cost,
        "contracts": 1,
        "sigma_pct": round(sigma * 100, 1),
    }


def _daily_swing_check(symbol: str, direction: str) -> dict:
    """
    Check if the stock's daily chart supports a swing trade entry.
    Uses IBKR daily bars (60 days). Returns pass/fail + score + reasons.
    """
    try:
        import ibkr_data as _id
        bars = _id.get_daily_bars(symbol, "60 D")
    except Exception:
        return {"pass": False, "score": 0, "reasons": [], "notes": ["daily data unavailable"]}

    if len(bars) < 25:
        return {"pass": False, "score": 0, "reasons": [], "notes": ["insufficient daily history"]}

    closes = [b["close"] for b in bars]
    highs  = [b["high"]  for b in bars]
    lows   = [b["low"]   for b in bars]
    opens  = [b["open"]  for b in bars]
    price  = closes[-1]
    ma20   = sum(closes[-20:]) / 20
    ma50   = sum(closes[-min(50, len(closes)):]) / min(50, len(closes))

    score   = 0
    reasons = []
    notes   = []

    if direction == "CALL":
        if price > ma50:
            score += 15; reasons.append(f"above MA50 ${ma50:.2f}")
        else:
            score -= 10; notes.append(f"below MA50 ${ma50:.2f} — counter-trend")
        if ma20 > ma50:
            score += 10; reasons.append("MA20 > MA50 — daily uptrend")
        if closes[-1] > opens[-1]:
            score += 8;  reasons.append("today's daily candle bullish")
        ext = (price - ma20) / ma20 * 100
        if ext > 8:
            score -= 8; notes.append(f"extended {ext:.1f}% above MA20 — wait for pullback")
        elif ext > 0:
            score += 5; reasons.append(f"above MA20 by {ext:.1f}%")
        if closes[-1] > closes[-6]:
            score += 8; reasons.append("5-day trend up")
        if len(highs) >= 10 and max(highs[-5:]) > max(highs[-10:-5]):
            score += 5; reasons.append("recent highs expanding")
        else:
            notes.append("highs not expanding — possible distribution")
    else:
        if price < ma50:
            score += 15; reasons.append(f"below MA50 ${ma50:.2f}")
        else:
            score -= 10; notes.append(f"above MA50 ${ma50:.2f} — counter-trend short")
        if ma20 < ma50:
            score += 10; reasons.append("MA20 < MA50 — daily downtrend")
        if closes[-1] < opens[-1]:
            score += 8;  reasons.append("today's daily candle bearish")
        ext = (ma20 - price) / ma20 * 100
        if ext > 8:
            score -= 8; notes.append(f"extended {ext:.1f}% below MA20 — bounce risk")
        elif ext > 0:
            score += 5; reasons.append(f"below MA20 by {ext:.1f}%")
        if closes[-1] < closes[-6]:
            score += 8; reasons.append("5-day trend down")
        if len(lows) >= 10 and min(lows[-5:]) < min(lows[-10:-5]):
            score += 5; reasons.append("recent lows expanding")
        else:
            notes.append("lows not expanding — possible bottoming")

    return {
        "pass":    score >= 25 and not any("counter-trend" in n for n in notes),
        "score":   score,
        "ma20":    round(ma20, 2),
        "ma50":    round(ma50, 2),
        "price":   round(price, 2),
        "reasons": reasons,
        "notes":   notes,
    }


SWING_WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "swing_watchlist.json")


def _notify_swing_watchlist(setups: list) -> None:
    """Push end-of-day swing watchlist to NTFY."""
    import os as _os
    topic = _os.environ.get("NTFY_TOPIC", "")
    if not topic:
        return
    try:
        import urllib.request as _ur
        lines = [f"Tonight's swing setups ({len(setups)}):"]
        for s in setups:
            arrow = "▲" if s["direction"] == "CALL" else "▼"
            opt   = s.get("option", {})
            lines.append(
                f"{arrow} {s['symbol']}  entry~${s['entry']:.2f}  target ${s['target']:.2f}"
                f"  |  {opt.get('strike')} {s['direction']} {opt.get('expiry')} ~${opt.get('cost', 0):.0f}"
            )
        msg = "\n".join(lines).encode("utf-8")
        req = _ur.Request(
            f"https://ntfy.sh/{topic}", data=msg,
            headers={"Title": f"Swing Watchlist - {len(setups)} setup(s) for tomorrow",
                     "Priority": "default",
                     "Tags": "moon",
                     "Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        _ur.urlopen(req, timeout=5)
        _logger.info("ntfy swing watchlist sent: %d setups", len(setups))
    except Exception as _e:
        _logger.warning("ntfy swing watchlist failed: %s", _e)


def _archive_today_bars(symbols: list) -> None:
    """
    EOD: write today's 5-min bars for every scanned symbol to CSV.

    Yahoo free only serves 5 days of 5-min history — without archiving, real
    intraday backtests are impossible. Each session adds one file per symbol:
        data/bars/YYYY-MM-DD/SYM.csv  (ts,open,high,low,close,volume)
    Idempotent: skips files that already exist for today.
    """
    today = _et_now().strftime("%Y-%m-%d")
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "bars", today)
    os.makedirs(out_dir, exist_ok=True)
    saved = 0
    for sym in symbols:
        path = os.path.join(out_dir, f"{sym.upper()}.csv")
        if os.path.exists(path):
            continue
        try:
            bars, _ = fetch_symbol_data(sym)
            today_bars = [
                b for b in (bars or [])
                if datetime.fromtimestamp(b[0], tz=_ET).strftime("%Y-%m-%d") == today
            ]
            if len(today_bars) < 10:    # half-session minimum — skip dead symbols
                continue
            with open(path, "w") as f:
                f.write("ts,open,high,low,close,volume\n")
                for b in today_bars:
                    f.write(f"{int(b[0])},{b[1]},{b[2]},{b[3]},{b[4]},{int(b[5])}\n")
            saved += 1
        except Exception:
            continue
    if saved:
        _logger.info("bar archive: saved %d symbols to %s", saved, out_dir)


def _eod_swing_sweep() -> None:
    """
    Run at ~3:45 PM: check all today's Grade S intraday signals against the
    daily chart. Passing signals get a 7-14 DTE OTM option recommendation.
    Results → data/swing_watchlist.json + NTFY push.
    """
    today = _et_now().strftime("%Y-%m-%d")
    _logger.info("EOD swing sweep starting")

    try:
        with _day_trades_lock, open(PAPER_FILE) as f:
            data = json.load(f)
        trades = data.get("trades", []) if isinstance(data, dict) else data
        today_sigs = [t for t in trades if t.get("date") == today and t.get("grade") == "S"]
    except Exception as e:
        _logger.warning("swing sweep: could not load trades: %s", e)
        return

    # Deduplicate by symbol — keep highest-scoring signal per symbol
    seen: dict = {}
    for t in today_sigs:
        sym = t["symbol"]
        if sym not in seen or t.get("score", 0) > seen[sym].get("score", 0):
            seen[sym] = t

    setups = []
    for sig in seen.values():
        check = _daily_swing_check(sig["symbol"], sig["direction"])
        _logger.info("swing check %s: pass=%s score=%d", sig["symbol"], check["pass"], check["score"])
        if not check["pass"]:
            continue
        opt = option_suggestion_swing(sig)
        setups.append({
            "symbol":        sig["symbol"],
            "direction":     sig["direction"],
            "intraday_score": sig.get("score"),
            "daily_score":   check["score"],
            "entry":         sig["entry"],
            "stop":          sig["stop"],
            "target":        sig["target"],
            "ma20":          check["ma20"],
            "ma50":          check["ma50"],
            "daily_reasons": check["reasons"],
            "daily_notes":   check["notes"],
            "option":        opt,
            "date":          today,
            "time_fired":    sig.get("time"),
        })

    try:
        with open(SWING_WATCHLIST_FILE, "w") as f:
            json.dump({"date": today, "setups": setups,
                       "generated_at": _et_now().isoformat()}, f, indent=2)
        _logger.info("swing sweep: saved %d setups", len(setups))
    except Exception as e:
        _logger.warning("swing sweep: save failed: %s", e)

    if setups:
        _notify_swing_watchlist(setups)
    else:
        _logger.info("swing sweep: no signals passed daily check")


# ── Notify tracker (independent of log_signal cooldown) ──────────────────────
_notified_ts: dict = {}   # {symbol: last_notify_unix_ts}
_watching_sent: dict = {} # {symbol: date_str} — watching alert fires once per symbol per day

# ── Best-signal-of-the-day tracker ───────────────────────────────────────────
_best_signal_today: dict | None = None
_best_signal_date:  str         = ""
_best_signal_lock               = threading.Lock()

def _update_best_signal(sig: dict) -> None:
    global _best_signal_today, _best_signal_date
    today = _et_now().strftime("%Y-%m-%d")
    with _best_signal_lock:
        if _best_signal_date != today:
            _best_signal_today = None
            _best_signal_date  = today
        if (_best_signal_today is None or
                sig["score"] > _best_signal_today["score"]):
            _best_signal_today = sig


# ═══════════════════════════════════════════════════════════════════════════════
# DISPLAY
# ═══════════════════════════════════════════════════════════════════════════════

_GRADE_STYLE = {"S": "bold magenta", "A": "bold green", "B": "green",
                "C": "yellow",       "D": "dim red"}
_DIR_STYLE   = {"CALL": "bold green", "PUT": "bold red"}


def display(signals, regime_label, next_refresh_min):
    ts = _et_now().strftime("%H:%M ET")

    if not RICH:
        print(f"\n{'─'*72}")
        print(f"  {ts}  |  Regime: {regime_label}  |  Next: {next_refresh_min} min")
        print(f"{'─'*72}")
        if not signals:
            print("  No signals above threshold.")
        for s in sorted(signals, key=lambda x: -x["score"]):
            arrow = "▲" if s["direction"] == "CALL" else "▼"
            print(f"  {arrow} {s['symbol']:<6} {s['direction']:<4}  [{s['grade']}]  "
                  f"{s['score']:.0f}%  entry ${s['entry']:.2f}  "
                  f"stop ${s['stop']:.2f}  tgt ${s['target']:.2f}  {s['rr']}R  "
                  f"{', '.join(s['reasons'][:2])}")
        best = _best_signal_today
        if best and best["direction"] == "CALL":
            opt = option_suggestion(best)
            print(f"\n{'═'*72}")
            print(f"  TODAY'S BEST TRADE")
            print(f"{'═'*72}")
            if opt["affordable"]:
                print(f"  BUY CALL  {best['symbol']}  (score {best['score']:.0f}, fired {best['time']})")
                print(f"  Strike :  ${opt['strike']}  (ATM)")
                print(f"  Expiry :  {opt['expiry']}  ({opt['dte']} days out)")
                print(f"  Est cost: ~${opt['cost']:.0f} for 1 contract")
                print(f"  Exit    : target +50% / stop -40% on the option price")
                print(f"  Stock stop ${best['stop']:.2f}  ·  target ${best['target']:.2f}")
            else:
                print(f"  {best['symbol']} signal: est option ~${opt['cost']:.0f} — too expensive, skip.")
            print(f"{'═'*72}")
        return

    console.clear()
    console.rule(
        f"[bold cyan]📈 DAY TRADING SCANNER[/]  "
        f"[dim]{ts}  ·  next scan in {next_refresh_min} min[/]"
    )
    console.print(f"  Regime: {regime_label}\n")

    if not signals:
        console.print("  [dim]No signals above threshold this scan.[/]\n")
        return

    tbl = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold dim")
    cols = [
        ("Symbol", "left"), ("Dir", "center"), ("Gr", "center"),
        ("Score",  "right"), ("Entry", "right"), ("Stop",   "right"),
        ("Target", "right"), ("R:R",  "right"),  ("RSI",    "right"),
        ("RVol",   "right"), ("ADX",  "right"),  ("Reasons","left"),
        ("Time",   "right"),
    ]
    for col, justify in cols:
        tbl.add_column(col, justify=justify)

    for s in sorted(signals, key=lambda x: -x["score"]):
        arrow = "▲" if s["direction"] == "CALL" else "▼"
        tbl.add_row(
            s["symbol"],
            Text(f"{arrow} {s['direction']}", _DIR_STYLE[s["direction"]]),
            Text(s["grade"], _GRADE_STYLE[s["grade"]]),
            f"{s['score']:.0f}%",
            f"${s['entry']:.2f}",
            f"${s['stop']:.2f}",
            f"${s['target']:.2f}",
            f"{s['rr']:.1f}R",
            f"{s['rsi']:.0f}" if s["rsi"] else "—",
            f"{s['rel_vol']:.1f}x",
            f"{s['adx']:.0f}" if s["adx"] else "—",
            ", ".join(s["reasons"][:2]),
            s["time"],
        )

    console.print(tbl)

    for s in signals:
        if s["warnings"]:
            console.print(
                f"  [yellow]⚠ {s['symbol']}:[/] {' | '.join(s['warnings'])}"
            )
    console.print()

    # ── Best option recommendation ────────────────────────────────────────────
    best = _best_signal_today
    if best and best["direction"] == "CALL":
        opt = option_suggestion(best)
        if opt["affordable"]:
            console.rule("[bold yellow]TODAY'S BEST TRADE[/]")
            console.print(
                f"  [bold green]BUY CALL  {best['symbol']}[/]"
                f"  [dim]score {best['score']:.0f}  fired {best['time']}[/]"
            )
            console.print(
                f"  [bold]Strike :[/]  ${opt['strike']}  (ATM)"
            )
            console.print(
                f"  [bold]Expiry :[/]  {opt['expiry']}  ({opt['dte']} days out)"
            )
            console.print(
                f"  [bold]Est cost:[/] ~${opt['cost']:.0f} for 1 contract"
                f"  [dim](vol {opt['sigma_pct']:.0f}%)[/]"
            )
            console.print(
                f"  [bold]Exit    :[/] target [green]+50%[/]  /  stop [red]-40%[/]  "
                f"on the option price"
            )
            console.print(
                f"  [dim]Stock stop ${best['stop']:.2f}  ·  target ${best['target']:.2f}[/]"
            )
            console.rule()
        else:
            console.print(
                f"  [yellow]Best signal {best['symbol']} est option ~${opt['cost']:.0f}"
                f" — too expensive for ${ACCOUNT_SIZE:.0f} account, skip.[/]"
            )
    console.print()


def _session_summary():
    pt    = _load_day_trades()
    today = _et_now().strftime("%Y-%m-%d")
    trades = [t for t in pt["trades"] if t["date"] == today]
    if not trades:
        return

    closed = [t for t in trades if t.get("status") != "open"]
    won    = [t for t in closed  if t.get("status") == "won"]
    lost   = [t for t in closed  if t.get("status") == "lost"]
    eod_cl = [t for t in closed  if t.get("status") == "closed"]
    open_  = [t for t in trades  if t.get("status") == "open"]

    total_pnl   = sum(t.get("pnl_dollar") or 0 for t in closed)
    win_rate    = len(won) / len(closed) * 100 if closed else 0.0
    avg_win     = (sum(t.get("pnl_dollar") or 0 for t in won)  / len(won)  if won  else 0)
    avg_loss    = (sum(t.get("pnl_dollar") or 0 for t in lost) / len(lost) if lost else 0)

    if RICH:
        console.rule("[bold]Today's Session[/]")
        tbl = Table(box=box.SIMPLE)
        for col in ["Time", "Symbol", "Dir", "Gr", "Score",
                    "Entry", "Exit", "P&L $", "Status"]:
            tbl.add_column(col)
        for t in trades:
            arrow  = "▲" if t["direction"] == "CALL" else "▼"
            status = t.get("status", "open")
            pnl    = t.get("pnl_dollar")
            pnl_s  = (f"[green]+${pnl:.2f}[/]" if pnl and pnl > 0
                      else f"[red]-${abs(pnl):.2f}[/]" if pnl and pnl < 0
                      else "—")
            exit_s = f"${t['exit_price']:.2f}" if t.get("exit_price") else "open"
            status_s = {"won": "[green]✓ won[/]", "lost": "[red]✗ lost[/]",
                        "closed": "[dim]EOD[/]", "open": "[yellow]open[/]"}.get(status, status)
            tbl.add_row(
                t["time"], t["symbol"],
                Text(f"{arrow} {t['direction']}", _DIR_STYLE[t["direction"]]),
                Text(t["grade"], _GRADE_STYLE[t["grade"]]),
                f"{t['score']:.0f}%",
                f"${t['entry']:.2f}", exit_s, pnl_s, status_s,
            )
        console.print(tbl)

        pnl_color = "green" if total_pnl >= 0 else "red"
        console.print(
            f"  Closed: [bold]{len(closed)}[/]  "
            f"[green]W: {len(won)}[/]  [red]L: {len(lost)}[/]  "
            f"[dim]EOD: {len(eod_cl)}[/]  Open: {len(open_)}   "
            f"Win rate: [bold]{win_rate:.0f}%[/]   "
            f"P&L: [{pnl_color}][bold]${total_pnl:+.2f}[/][/]"
        )
        if won:  console.print(f"  [dim]Avg winner: +${avg_win:.2f}[/]")
        if lost: console.print(f"  [dim]Avg loser:  ${avg_loss:.2f}[/]")
        console.print()
    else:
        print(f"\nToday's session ({len(trades)} signals):")
        for t in trades:
            arrow  = "▲" if t["direction"] == "CALL" else "▼"
            pnl    = t.get("pnl_dollar")
            pnl_s  = f"  P&L ${pnl:+.2f}" if pnl is not None else ""
            exit_s = f"  exit ${t['exit_price']:.2f}" if t.get("exit_price") else ""
            print(f"  {t['time']}  {arrow} {t['symbol']:<6} [{t['grade']}] "
                  f"{t['score']:.0f}%  entry ${t['entry']:.2f}"
                  f"{exit_s}{pnl_s}  [{t.get('status','open')}]")
        print(f"\n  Closed {len(closed)} | W {len(won)} L {len(lost)} EOD {len(eod_cl)}"
              f" | Win rate {win_rate:.0f}% | P&L ${total_pnl:+.2f}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN SCAN LOOP
# ═══════════════════════════════════════════════════════════════════════════════

def _notify_signal(sig):
    """ntfy push when a new grade S/A signal fires."""
    import os
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        _logger.warning("NTFY_TOPIC not set — skipping push for %s", sig.get("symbol"))
        return
    if sig.get("grade") not in ("S", "A"):
        return
    try:
        import urllib.request as _ur
        arrow  = "▲" if sig["direction"] == "CALL" else "▼"
        pb     = sig.get("pullback_entry")
        pb_tag = "✅ PULLBACK ENTRY" if pb else "⚠️ EXTENDED — wait for pullback"
        h1_sh  = sig.get("h1_swing_high")
        h1_sl  = sig.get("h1_swing_low")
        h1_tag = ""
        if h1_sh and sig["direction"] == "CALL":
            h1_tag = f"  1H swing high ${h1_sh:.2f}"
        elif h1_sl and sig["direction"] == "PUT":
            h1_tag = f"  1H swing low ${h1_sl:.2f}"
        msg = (f"{arrow} {sig['symbol']} {sig['direction']}  Grade {sig['grade']}  "
               f"score={sig['score']:.0f}  entry=${sig['entry']:.2f}  "
               f"stop=${sig['stop']:.2f}  target=${sig['target']:.2f}  "
               f"{pb_tag}{h1_tag}").encode()
        req = _ur.Request(
            f"https://ntfy.sh/{topic}", data=msg,
            headers={"Title": f"{sig['symbol']} Day Trade Signal",
                     "Priority": "high" if sig["grade"] == "S" else "default",
                     "Tags": "chart_with_upwards_trend"},
            method="POST",
        )
        _ur.urlopen(req, timeout=5)
    except Exception as _e:
        import logging as _lg
        _lg.getLogger(__name__).warning("ntfy push failed: %s", _e)


def _notify_watching(symbol, direction, score, price, reasons):
    """ntfy push when a symbol is building up toward a signal (score 75-89)."""
    import os
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        return
    try:
        import urllib.request as _ur
        arrow  = "▲" if direction == "CALL" else "▼"
        top_r  = ", ".join(reasons[:2]) if reasons else ""
        msg    = (f"{arrow} {symbol} building up — score {score:.0f}  "
                  f"price ${price:.2f}  [{top_r}]  "
                  f"→ watch for pullback entry on EMA9/VWAP").encode()
        req = _ur.Request(
            f"https://ntfy.sh/{topic}", data=msg,
            headers={"Title": f"👀 {symbol} WATCHING ({direction})",
                     "Priority": "default",
                     "Tags": "eyes"},
            method="POST",
        )
        _ur.urlopen(req, timeout=5)
        _logger.info("ntfy watching → %s  score=%.0f", symbol, score)
    except Exception as _e:
        import logging as _lg
        _lg.getLogger(__name__).warning("ntfy watching push failed: %s", _e)


def _notify_exit(trade):
    """ntfy push when a trade hits stop or target."""
    import os
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        return
    try:
        import urllib.request as _ur
        status = trade.get("status", "closed")
        sym    = trade["symbol"]
        dirn   = trade["direction"]
        pnl    = trade.get("pnl_dollar", 0) or 0
        exit_p = trade.get("exit_price", 0)
        emoji  = "✅" if status == "won" else "🛑" if status == "lost" else "🔔"
        label  = "TARGET HIT" if status == "won" else "STOP HIT" if status == "lost" else "EOD CLOSE"
        msg    = (f"{emoji} {sym} {dirn}  {label}  "
                  f"exit=${exit_p:.2f}  P&L ${pnl:+.2f}").encode()
        req = _ur.Request(
            f"https://ntfy.sh/{topic}", data=msg,
            headers={"Title": f"{sym} {label}",
                     "Priority": "high" if status == "won" else "default",
                     "Tags": "white_check_mark" if status == "won" else "x"},
            method="POST",
        )
        _ur.urlopen(req, timeout=5)
    except Exception:
        pass


def run(symbols=None, once=False, interval_min=SCAN_INTERVAL_MIN):
    base_list        = list(symbols) if symbols else list(DEFAULT_SYMBOLS)
    scan_list        = base_list
    cooldown_tracker = {}   # {symbol: last_fired_unix_ts}
    _scan_cycle      = 0    # counter — rebuild universe every 3 cycles (15 min)
    _was_open        = False  # tracks open→close transition for EOD sweep
    _catalyst_primed = False  # pre-market gap cache populated once after 9:35 AM
    _swing_swept     = False  # EOD swing sweep fires once at 3:45 PM
    _bars_archived   = False  # 5-min bar archive fires once at 3:50 PM

    if RICH:
        console.print(Panel(
            "[bold cyan]DAY TRADING SCANNER[/]\n"
            "[dim]5-min primary  ·  15-min HTF  ·  Hybrid Pine + Algo scoring[/]\n"
            "Pine Sniper signals  ×  smart money (CMF/OBV/MFI)\n"
            "Market regime gate  ×  earnings blackout  ×  R:R ≥1.5 required",
            box=box.DOUBLE,
        ))
    else:
        print("=" * 60)
        print("  DAY TRADING SCANNER  (5-min / 15-min hybrid)")
        print("  Pine Sniper + Smart Money + Regime + Earnings gate")
        print("=" * 60)

    while True:
        now   = _et_now()
        h, m  = now.hour, now.minute
        is_open = (h > 9 or (h == 9 and m >= 30)) and h < 16

        if not is_open:
            # Market just closed — run one final EOD sweep before sleeping
            if _was_open:
                final_closed = check_open_trades()
                for t in final_closed:
                    _notify_exit(t)
                _session_summary()
            _was_open = False
            msg = f"Market closed ({now.strftime('%H:%M ET')}). Waiting for 9:30 ET..."
            if RICH: console.print(f"[dim]{msg}[/]")
            else:    print(msg)
            if once:
                break
            time.sleep(120)
            continue

        _was_open = True

        # EOD swing sweep — fires once at 3:45 PM, runs in background thread
        if not _swing_swept and h == 15 and m >= 45:
            _swing_swept = True
            threading.Thread(target=_eod_swing_sweep, daemon=True).start()

        # Bar archive — fires once at 3:50 PM, builds the 5-min backtest dataset
        if not _bars_archived and h == 15 and m >= 50:
            _bars_archived = True
            threading.Thread(target=_archive_today_bars,
                             args=(list(base_list),), daemon=True).start()

        # Prime catalyst + daily bias + IV rank caches once per session after 9:35 AM.
        if not _catalyst_primed and (h > 9 or (h == 9 and m >= 35)):
            try:
                _prime_catalyst_cache(base_list)
                _prime_bias_cache(base_list)
                # Warm IV rank cache in background so first scan has it ready.
                def _warm_iv(syms):
                    try:
                        import ibkr_data as _id
                        for s in syms:
                            _id.get_iv_rank(s)
                    except Exception:
                        pass
                threading.Thread(target=_warm_iv, args=(list(base_list),), daemon=True).start()
                _catalyst_primed = True
                # Expand base_list with top premarket movers not already in universe
                if _premarket_top_symbols:
                    new_syms = [s for s in _premarket_top_symbols if s not in base_list]
                    if new_syms:
                        base_list = list(base_list) + new_syms
                        print(f"  📊 Premarket: added {len(new_syms)} catalyst movers to universe: {' '.join(new_syms)}")
            except Exception:
                pass

        # Close any trades that hit stop/target since last cycle
        newly_closed = check_open_trades()
        for t in newly_closed:
            _notify_exit(t)

        # Rebuild scan universe every 3 cycles to pull in fresh IBKR top gainers
        _scan_cycle += 1
        if _scan_cycle == 1 or _scan_cycle % 3 == 0:
            try:
                scan_list = build_scan_universe(base_list)
            except Exception:
                scan_list = base_list

        # Regime check — intraday (SPY VWAP/EMA) + daily (SPY 50DMA)
        regime_bull, regime_label = intraday_regime()
        daily_bull, _             = market_regime()
        # Block PUTs when daily trend is bullish — short-term intraday dips in a
        # bull market get bought back quickly, stopping out PUT positions.
        if daily_bull and not regime_bull:
            # Intraday temporarily bearish but daily trend is up — treat as bull
            # to prevent PUT signals that will get whipsawed by the larger trend.
            regime_bull = True
            regime_label += "  [PUT gate: daily bull overrides intraday bear]"

        # ── Circuit breaker — 3 stop-outs today = stand down ──────────────────
        # When the day's character is against the system, more signals are more
        # losses. Open trades keep being managed above; only NEW entries halt.
        today_str = _et_now().strftime("%Y-%m-%d")
        _stops_today = stops_today_count()
        if _stops_today >= DAILY_MAX_STOPS:
            msg = (f"🛑 CIRCUIT BREAKER: {_stops_today} stop-outs today — "
                   f"no new entries until tomorrow (open trades still managed)")
            if RICH: console.print(f"[bold red]{msg}[/]")
            else:    print(msg)
            if once:
                break
            time.sleep(interval_min * 60)
            continue

        # Parallel fetch + evaluate
        signals = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {
                pool.submit(evaluate_symbol, sym, cooldown_tracker, regime_bull): sym
                for sym in scan_list
            }
            for fut in as_completed(futures):
                try:
                    result = fut.result()
                except Exception:
                    continue
                if not result:
                    continue
                if result.get("watching"):
                    # Watching tier — push once per symbol per day, don't log as trade
                    sym = result["symbol"]
                    if _watching_sent.get(sym) != today_str:
                        _watching_sent[sym] = today_str
                        _notify_watching(sym, result["direction"], result["score"],
                                         result["entry"], result["reasons"])
                else:
                    signals.append(result)
                    cooldown_tracker[result["symbol"]] = result["ts"]
                    log_signal(result)
                    _now = result.get("ts", time.time())
                    if _now - _notified_ts.get(result["symbol"], 0) >= COOLDOWN_MIN * 60:
                        _notify_signal(result)
                        _notified_ts[result["symbol"]] = _now

        display(signals, regime_label, interval_min)

        if once:
            break

        time.sleep(interval_min * 60)

    _session_summary()


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    args = sys.argv[1:]

    once     = "--once"     in args
    sym_idx  = args.index("--symbols") + 1 if "--symbols" in args else None
    int_idx  = args.index("--interval") + 1 if "--interval" in args else None

    symbols  = None
    interval = SCAN_INTERVAL_MIN

    if sym_idx and sym_idx < len(args):
        symbols = [s.upper() for s in args[sym_idx:] if not s.startswith("--")]

    if int_idx and int_idx < len(args):
        try:
            interval = int(args[int_idx])
        except ValueError:
            pass

    run(symbols=symbols, once=once, interval_min=interval)
