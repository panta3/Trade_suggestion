"""
Gap Fade + Multi-Algo Signals — Flask REST API
==============================================
Setup:  pip install flask flask-cors
Run:    python api.py          (starts on http://localhost:5000)

Algos (pass as ?algo= query param):
  largecap  — TSX + NASDAQ large caps        (largecapsignals.py)
  spx       — S&P 500 + NASDAQ pure US       (SPXindex.py)
  smallcap  — small-cap momentum / penny     (signals.py)

Endpoints:
  GET  /api/scan?algo=          full scan for chosen algo (~60s)
  GET  /api/score?ticker=&algo= score a single ticker
  GET  /api/price?ticker=       live price only
  GET  /api/portfolio           portfolio JSON
  GET  /api/options?ticker=     live options chain (IBKR → yfinance fallback)
"""

import sys, os
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context
from flask_cors import CORS
from datetime import datetime

sys.path.insert(0, ".")
import largecapsignals as _lc
import SPXindex        as _spx
import signals         as _sc

_WEB_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "dist")
app = Flask(__name__, static_folder=_WEB_DIST, static_url_path="")
CORS(app)


@app.route("/")
@app.route("/dashboard.html")
def serve_app():
    dist = _WEB_DIST
    if os.path.exists(os.path.join(dist, "index.html")):
        return send_from_directory(dist, "index.html")
    # Fallback to old dashboard.html while dist hasn't been built yet
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"),
        "dashboard.html",
    )

ALGO_LABELS = {
    "largecap": "Large Cap (TSX + NASDAQ)",
    "spx":      "S&P 500 / NASDAQ",
    "smallcap": "Small Cap (momentum)",
}


def _mod(algo):
    """Return the right signal module for the given algo key."""
    if algo == "spx":
        return _spx
    if algo == "smallcap":
        return _sc
    return _lc  # default


def _serialize(rec):
    s   = rec["stock"]
    sym = rec["symbol"]
    market = ("TSX"  if sym.endswith(".TO") else
              "TSXV" if sym.endswith(".V")  else
              "CSE"  if sym.endswith(".CN") else "US")
    return {
        "symbol":        sym,
        "signal":        rec["signal"],
        "score":         rec["score"],
        "price":         s.get("price"),
        "change":        s.get("change"),
        "stop":          rec["stop"],
        "target":        rec["target"],
        "risk_reward":   rec["risk_reward"],
        "stop_pct":      rec["stop_pct"],
        "reasons":       rec["reasons"],
        "warnings":      rec["warnings"],
        "entry_tip":     rec["entry_tip"],
        "rsi_d":         s.get("rsi_d"),
        "rsi_1h":        s.get("rsi_1h"),
        "vol_spike":     s.get("vol_spike"),
        "sector":        rec.get("sector"),
        "market":        market,
        "earnings_days": rec.get("earnings_days"),
        "support":       s.get("support"),
        "resistance":    s.get("resistance"),
        "ema9":          s.get("ema9"),
        "ema21":         s.get("ema21"),
        "cmf20":         s.get("cmf20"),
        "ret7":          s.get("ret7"),
        "ret30":         s.get("ret30"),
        "options":       rec.get("options"),
    }


@app.route("/api/scan")
def api_scan():
    algo = request.args.get("algo", "largecap")
    m    = _mod(algo)
    try:
        stocks, headlines, _ = m.scan_all()
        if not stocks:
            return jsonify({
                "signals": [], "regime": "no data", "algo": algo,
                "scanned_at": datetime.now().isoformat(), "headlines": [],
            })

        portfolio    = m.load_portfolio()
        usd_cad      = m.fetch_usdcad_rate()
        sector_ranks = m.build_sector_rank_map(stocks)
        regime_bull, regime_label = m.market_regime()
        syms = [s["symbol"] for s in stocks]

        # Pre-warm weekly trends in parallel
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(m.fetch_weekly_trend, syms))

        # Fast pass — no earnings, find top-15 shortlist
        fast = sorted(
            [m.evaluate_stock(s, portfolio["cash"], usd_cad, sector_ranks,
                              include_earnings=False, regime_bullish=regime_bull,
                              all_stocks=stocks)
             for s in stocks],
            key=lambda x: x["score"], reverse=True,
        )
        shortlist = {r["symbol"] for r in fast[:15]}

        # Pre-warm earnings for shortlist only (15 stocks, not all)
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(m.fetch_earnings_date, list(shortlist)))

        # Full pass with earnings for shortlist
        recs = sorted(
            [m.evaluate_stock(s, portfolio["cash"], usd_cad, sector_ranks,
                              include_earnings=(s["symbol"] in shortlist),
                              regime_bullish=regime_bull, all_stocks=stocks)
             for s in stocks],
            key=lambda x: x["score"], reverse=True,
        )

        # Sector cap — downgrade 3rd+ BUY in the same sector to WATCH
        sector_buy_count: dict[str, int] = {}
        for r in recs:
            if r["signal"] == "BUY":
                sec = r.get("sector") or "unknown"
                sector_buy_count[sec] = sector_buy_count.get(sec, 0) + 1
                if sector_buy_count[sec] > 2:
                    r["signal"] = "WATCH"
                    r["warnings"] = list(r.get("warnings", []))
                    r["warnings"].append(f"sector cap: >2 BUYs already flagged in {sec}")

        # Enrich top options-eligible BUY signals with a live contract
        eligible = [r for r in recs if r.get("options", {}).get("eligible")][:5]
        if eligible:
            def _fetch_contract(r):
                opts = r["options"]
                c = _best_options_contract(r["symbol"], opts.get("direction", "CALL"),
                                           opts.get("dte_min", 30))
                if c:
                    opts["contract"] = c
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(_fetch_contract, eligible))

        return jsonify({
            "signals":    [_serialize(r) for r in recs],
            "regime":     regime_label,
            "algo":       algo,
            "algo_label": ALGO_LABELS.get(algo, algo),
            "scanned_at": datetime.now().isoformat(),
            "headlines":  headlines[:5],
        })
    except Exception as e:
        return jsonify({"error": str(e), "algo": algo}), 500


@app.route("/api/score")
def api_score():
    ticker = request.args.get("ticker", "").upper().strip()
    algo   = request.args.get("algo", "largecap")
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    m = _mod(algo)
    try:
        s = m.fetch_stock(ticker)
        if not s.get("ok"):
            return jsonify({"error": f"no data for {ticker}"}), 404
        s["sentiment"]    = {"score": 0, "label": "⚪ neutral", "headlines": []}
        portfolio         = m.load_portfolio()
        usd_cad           = m.fetch_usdcad_rate()
        regime_bull, regime_label = m.market_regime()
        sector_ranks = m.build_sector_rank_map([s])
        rec = m.evaluate_stock(s, portfolio["cash"], usd_cad, sector_ranks,
                               regime_bullish=regime_bull, all_stocks=[s])
        return jsonify({"signal": _serialize(rec), "regime": regime_label, "algo": algo})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/price")
def api_price():
    ticker = request.args.get("ticker", "").upper().strip()
    algo   = request.args.get("algo", "largecap")
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    m = _mod(algo)
    try:
        s = m.fetch_stock(ticker, include_hourly=False)
        if not s.get("ok"):
            return jsonify({"error": f"no data for {ticker}"}), 404
        return jsonify({
            "ticker": ticker,
            "price":  s["price"],
            "change": s["change"],
            "rsi_d":  s.get("rsi_d"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/quote")
def api_quote():
    ticker = request.args.get("ticker", "").upper().strip()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    try:
        s = _lc.fetch_stock(ticker, include_hourly=False)
        if not s.get("ok"):
            return jsonify({"error": f"no data for {ticker}"}), 404
        price  = s["price"]
        change = s["change"]
        prev   = round(price / (1 + change / 100), 2) if change != -100 else price
        closes = s.get("close_series", [])[-30:]
        return jsonify({
            "ticker":    ticker,
            "price":     price,
            "prevClose": prev,
            "closes":    closes,
            "rsi":       s.get("rsi_d"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/portfolio")
def api_portfolio():
    try:
        return jsonify(_lc.load_portfolio())
    except Exception as e:
        return jsonify({"error": str(e)}), 500




def _options_yfinance(ticker: str, target_dte: int) -> "dict | None":
    """Full options chain from yfinance for the expiry closest to target_dte."""
    try:
        import yfinance as yf
        from datetime import date as _date
        today = _date.today()

        def _dte(d):
            try:    return (_date.fromisoformat(d) - today).days
            except: return 9999

        t = yf.Ticker(ticker)
        expiries = t.options          # single network call — get all available dates
        if not expiries:
            return None

        # Pick the expiry closest to target_dte (≥ 1 day away)
        valid = [e for e in expiries if _dte(e) >= 1]
        if not valid:
            return None
        expiry     = min(valid, key=lambda e: abs(_dte(e) - (target_dte or 1)))
        actual_dte = _dte(expiry)

        chain = t.option_chain(expiry)

        def _rows(df, itm_col):
            rows = []
            for _, r in df.iterrows():
                bid  = float(r.get("bid") or 0)
                ask  = float(r.get("ask") or 0)
                last = float(r.get("lastPrice") or 0)
                mid  = (bid + ask) / 2 if bid + ask > 0 else last
                sp   = round((ask - bid) / mid * 100, 1) if mid > 0 else None
                rows.append({
                    "strike":       float(r.get("strike", 0)),
                    "lastPrice":    round(last, 2),
                    "bid":          round(bid, 2),
                    "ask":          round(ask, 2),
                    "mid":          round(mid, 2),
                    "spread_pct":   sp,
                    "iv":           round(float(r.get("impliedVolatility") or 0) * 100, 1),
                    "volume":       int(r.get("volume") or 0),
                    "openInterest": int(r.get("openInterest") or 0),
                    "inTheMoney":   bool(r.get("inTheMoney", False)),
                })
            return sorted(rows, key=lambda x: x["strike"])

        calls = _rows(chain.calls, "inTheMoney")
        puts  = _rows(chain.puts,  "inTheMoney")
        if not calls and not puts:
            return None

        all_expiries = sorted(valid)[:8]
        return {"ticker": ticker, "expiry": expiry, "dte": actual_dte,
                "calls": calls, "puts": puts,
                "all_expiries": all_expiries, "source": "yfinance"}
    except Exception:
        return None


@app.route("/api/options")
def api_options():
    ticker     = request.args.get("ticker", "").upper().strip()
    target_dte = request.args.get("dte", type=int, default=None)
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    try:
        # IBKR full-chain (live quotes, all near-ATM strikes)
        import ibkr_data as _id
        tgt = target_dte if target_dte is not None else 1
        result = _id.get_options_chain(ticker, tgt)
        if result:
            return jsonify(result)

        # yfinance full-chain fallback
        result = _options_yfinance(ticker, tgt)
        if result:
            return jsonify(result)

        return jsonify({"error": f"no options chain for {ticker}"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# CORE SIGNALS SCAN LOG  (save/validate BUY signals from largecap/spx/smallcap)
# ═══════════════════════════════════════════════════════════════════════════════

import threading as _threading
import time as _time
import json as _json
import queue as _queue
import logging as _logging
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo as _ZoneInfo
_ET = _ZoneInfo("America/New_York")

_SCAN_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "scan_results.json")
_scan_log_lock = _threading.Lock()


@app.route("/api/save_scan_signals", methods=["POST"])
def api_save_scan_signals():
    """Save current scan results (with open_px = current price) to scan_results.json."""
    body    = request.get_json(force=True, silent=True) or {}
    signals = body.get("signals", [])
    algo    = body.get("algo", "largecap")
    if not signals:
        return jsonify({"error": "no signals provided"}), 400

    today    = datetime.today().strftime("%Y-%m-%d")
    now_str  = datetime.now(tz=_ET).strftime("%H:%M:%S ET")

    rows = []
    for s in signals:
        if s.get("signal") == "SKIP":
            continue
        rows.append({
            "symbol":     s.get("symbol"),
            "signal":     s.get("signal"),
            "score":      s.get("score"),
            "price":      s.get("price"),
            "open_px":    s.get("price"),   # price at time of save = open
            "close_px":   None,
            "stop":       s.get("stop"),
            "target":     s.get("target"),
            "risk_reward": s.get("risk_reward"),
            "sector":     s.get("sector"),
            "market":     s.get("market"),
            "rsi_d":      s.get("rsi_d"),
            "change":     s.get("change"),
        })

    record = {"date": today, "algo": algo, "saved_at": now_str, "signals": rows}

    try:
        with _scan_log_lock:
            # Keep history — append today's record (replace if same date+algo)
            existing = []
            if os.path.exists(_SCAN_LOG_FILE):
                with open(_SCAN_LOG_FILE) as f:
                    existing = _json.load(f)
            if not isinstance(existing, list):
                existing = []
            # Remove old record for today+algo if present
            existing = [r for r in existing if not (r.get("date") == today and r.get("algo") == algo)]
            existing.append(record)
            with open(_SCAN_LOG_FILE, "w") as f:
                _json.dump(existing, f, indent=2)
        return jsonify({"saved": len(rows), "date": today, "algo": algo, "time": now_str})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scan_log_close", methods=["POST"])
def api_scan_log_close():
    """Fetch live prices for today's saved scan signals and stamp close_px."""
    import yfinance as _yf
    body  = request.get_json(force=True, silent=True) or {}
    algo  = body.get("algo", "largecap")
    today = datetime.today().strftime("%Y-%m-%d")

    try:
        with _scan_log_lock:
            if not os.path.exists(_SCAN_LOG_FILE):
                return jsonify({"error": "no scan results saved yet — press Save Open first"}), 404
            with open(_SCAN_LOG_FILE) as f:
                existing = _json.load(f)
            record = next((r for r in existing if r.get("date") == today and r.get("algo") == algo), None)
            if not record:
                return jsonify({"error": f"no saved scan for {today} / {algo} — press Save Open first"}), 404

            symbols = list({s["symbol"] for s in record["signals"] if s.get("symbol")})
            prices  = {}
            for sym in symbols:
                try:
                    px = _yf.Ticker(sym.replace(".TO", "").replace(".V", "").replace(".CN", "")).fast_info.last_price
                    if px:
                        prices[sym] = round(float(px), 2)
                except Exception:
                    pass

            updated = 0
            for s in record["signals"]:
                sym = s.get("symbol")
                if sym in prices:
                    s["close_px"] = prices[sym]
                    updated += 1

            record["close_logged_at"] = datetime.now(tz=_ET).strftime("%H:%M:%S ET")
            with open(_SCAN_LOG_FILE, "w") as f:
                _json.dump(existing, f, indent=2)

        return jsonify({"updated": updated, "prices": prices,
                        "time": record["close_logged_at"], "algo": algo, "date": today})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scan_validate")
def api_scan_validate():
    """Validate today's saved scan signals: close_px vs open_px per direction."""
    algo  = request.args.get("algo", "largecap")
    today = datetime.today().strftime("%Y-%m-%d")
    try:
        if not os.path.exists(_SCAN_LOG_FILE):
            return jsonify({"results": [], "date": today, "validated": 0})
        with _scan_log_lock, open(_SCAN_LOG_FILE) as f:
            existing = _json.load(f)
        record = next((r for r in existing if r.get("date") == today and r.get("algo") == algo), None)
        if not record:
            return jsonify({"results": [], "date": today, "validated": 0,
                            "error": f"No scan saved for {today}/{algo}"})

        results = []
        for s in record["signals"]:
            open_px  = s.get("open_px")
            close_px = s.get("close_px")
            signal   = s.get("signal", "BUY")
            stop     = s.get("stop")
            target   = s.get("target")
            rec = {
                "symbol":  s["symbol"],  "signal": signal,
                "score":   s.get("score"), "sector": s.get("sector"),
                "open_px": open_px, "close_px": close_px,
                "stop":    stop,    "target":   target,
            }
            if open_px and close_px:
                move_pct = round((close_px - open_px) / open_px * 100, 2)
                # BUY signals are long (call direction)
                win        = close_px > open_px
                hit_target = bool(target and close_px >= target)
                hit_stop   = bool(stop   and close_px <= stop)
                rec.update({
                    "move_pct":   move_pct,
                    "win":        win,
                    "hit_target": hit_target,
                    "hit_stop":   hit_stop,
                    "outcome":    "TARGET" if hit_target else ("STOP" if hit_stop else ("WIN" if win else "LOSS")),
                })
            else:
                rec["outcome"] = "NO_DATA"
            results.append(rec)

        results.sort(key=lambda x: x.get("score") or 0, reverse=True)
        validated = [r for r in results if r["outcome"] != "NO_DATA"]
        wins = sum(1 for r in validated if r.get("win"))
        return jsonify({
            "results":   results,
            "date":      today,
            "algo":      algo,
            "saved_at":  record.get("saved_at"),
            "total":     len(results),
            "validated": len(validated),
            "wins":      wins,
            "win_rate":  round(wins / len(validated) * 100, 1) if validated else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# INTRADAY DAY TRADING ENDPOINTS
# Uses day_trading.py logic; background thread refreshes every 5 min.
# ═══════════════════════════════════════════════════════════════════════════════

# ── Load data/config.env (KEY=value lines, # comments ignored) ───────────────
_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "config.env")
def _load_config():
    if not os.path.exists(_CONFIG_FILE):
        return
    with open(_CONFIG_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
_load_config()

# ── Rotating log ──────────────────────────────────────────────────────────────
_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "scanner.log")
_log_handler = _RotatingFileHandler(_LOG_FILE, maxBytes=5_000_000, backupCount=3)
_log_handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_logger = _logging.getLogger("scanner")
_logger.addHandler(_log_handler)
_logger.setLevel(_logging.INFO)

try:
    import day_trading as _dt
    _DT_AVAILABLE = True
except Exception as _e:
    _DT_AVAILABLE = False
    _logger.error("day_trading import failed: %s", _e)

# Shared state updated by background scanner
_intraday_cache = {
    "signals":      [],
    "regime":       "unknown",
    "scanned_at":   None,
    "scanning":     False,
    "error":        None,
}
_intraday_lock    = _threading.Lock()
_cooldowns_lock   = _threading.Lock()
_notify_sent      = {}
_notify_lock      = _threading.Lock()

# SSE client registry — each connected browser tab gets a queue
_sse_clients      = []
_sse_clients_lock = _threading.Lock()

def _sse_push(event_type: str, **kwargs):
    msg = _json.dumps({"type": event_type, **kwargs})
    with _sse_clients_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except _queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)
_COOLDOWNS_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cooldowns.json")

def _load_cooldowns():
    try:
        with open(_COOLDOWNS_FILE) as f:
            return {k: float(v) for k, v in _json.load(f).items()}
    except Exception:
        return {}

def _save_cooldowns(cd):
    try:
        with open(_COOLDOWNS_FILE, "w") as f:
            _json.dump(cd, f)
    except Exception as e:
        _logger.warning("cooldowns save failed: %s", e)

_intraday_cooldowns = _load_cooldowns()


def _notify(sig):
    """Push a grade S/A signal to ntfy.sh (set NTFY_TOPIC env var to enable)."""
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        _logger.warning("NTFY_TOPIC not set — skipping push for %s", sig.get("symbol"))
        return
    if sig.get("grade") not in ("S", "A"):
        _logger.info("ntfy skipped %s grade=%s (not S/A)", sig.get("symbol"), sig.get("grade"))
        return
    _logger.info("ntfy → %s  grade=%s  topic=%s", sig.get("symbol"), sig.get("grade"), topic)
    try:
        import urllib.request as _ur
        arrow  = "▲" if sig["direction"] == "CALL" else "▼"
        msg    = (f"{arrow} {sig['symbol']} {sig['direction']}  Grade {sig['grade']}  "
                  f"score={sig['score']:.0f}  entry=${sig['entry']:.2f}  "
                  f"stop=${sig['stop']:.2f}  target=${sig['target']:.2f}").encode()
        req = _ur.Request(
            f"https://ntfy.sh/{topic}", data=msg,
            headers={"Title": f"{sig['symbol']} Day Trade Signal",
                     "Priority": "high" if sig["grade"] == "S" else "default",
                     "Tags": "chart_with_upwards_trend"},
            method="POST",
        )
        _ur.urlopen(req, timeout=5)
    except Exception as e:
        _logger.warning("ntfy push failed: %s", e)


def _do_scan(force=False):
    """
    Run one full intraday scan and update the cache.
    force=True skips the market-hours guard but still applies in_session
    inside evaluate_symbol so after-hours signals are never logged.
    """
    if not _DT_AVAILABLE:
        return

    # Circuit breaker — same gate as the standalone scanner: 3 real stop-outs
    # today means no new entries (open trades are still managed in the loop).
    if _dt.stops_today_count() >= _dt.DAILY_MAX_STOPS:
        _logger.warning("circuit breaker: %d stop-outs today — scan skipped",
                        _dt.stops_today_count())
        with _intraday_lock:
            _intraday_cache["scanning"] = False
            _intraday_cache["error"]    = "circuit breaker: no new entries today"
        return

    with _intraday_lock:
        _intraday_cache["scanning"] = True

    _logger.info("scan started  force=%s", force)
    try:
        regime_bull, regime_label = _dt.intraday_regime()
        universe = _dt.build_scan_universe()   # pre-screened by rel-vol
        _logger.info("universe=%d symbols  regime=%s", len(universe), regime_label)
        results = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            with _cooldowns_lock:
                cd_snapshot = dict(_intraday_cooldowns)
            futs = {
                pool.submit(_dt.evaluate_symbol, sym, cd_snapshot, regime_bull): sym
                for sym in universe
            }
            for fut in futs:
                try:
                    sig = fut.result()
                except Exception as exc:
                    _logger.warning("evaluate_symbol error: %s", exc)
                    sig = None
                if sig and not sig.get("watching"):
                    results.append(sig)
                    with _cooldowns_lock:
                        _intraday_cooldowns[sig["symbol"]] = sig["ts"]
                    _dt.log_signal(sig)
                    _logger.info("signal  %s %s  score=%.0f  grade=%s",
                                 sig["symbol"], sig["direction"], sig["score"], sig["grade"])
                    _sse_push("new_signal",
                              symbol=sig["symbol"], direction=sig["direction"],
                              grade=sig["grade"], score=sig["score"])
                    # Notify independently — don't tie to log_signal's wrote flag.
                    # day_trading.py may have logged first (wrote=False here) but
                    # ntfy must still fire from whichever process sees the signal.
                    with _notify_lock:
                        last = _notify_sent.get(sig["symbol"], 0)
                        if sig["ts"] - last >= _dt.COOLDOWN_MIN * 60:
                            _notify(sig)
                            _notify_sent[sig["symbol"]] = sig["ts"]

        with _cooldowns_lock:
            _save_cooldowns(_intraday_cooldowns)

        _logger.info("scan done  signals=%d", len(results))
        with _intraday_lock:
            _intraday_cache.update({
                "signals":    sorted(results, key=lambda x: -x["score"]),
                "regime":     regime_label,
                "scanned_at": datetime.now().isoformat(),
                "scanning":   False,
                "error":      None,
                "market_open": True,
            })
    except Exception as exc:
        _logger.error("scan failed: %s", exc, exc_info=True)
        with _intraday_lock:
            _intraday_cache["scanning"] = False
            _intraday_cache["error"]    = str(exc)


def _run_intraday_scan():
    """Background worker: re-scan every 5 min. Always updates regime; signals only during hours."""
    _swing_swept   = False
    _caches_primed = False
    _bars_archived = False
    while True:
        now = datetime.now(tz=_ET)
        h, m = now.hour, now.minute
        in_session = (h > 9 or (h == 9 and m >= 30)) and h < 16

        # Reset flags at start of each new session
        if not in_session and h < 9:
            _swing_swept   = False
            _caches_primed = False
            _bars_archived = False

        if _DT_AVAILABLE:
            if in_session:
                # Prime catalyst/bias/IV caches once per session after 9:35 —
                # mirrors the standalone day_trading.py loop so scoring has
                # gap, daily-bias and IV-rank context from the first scan.
                if not _caches_primed and (h > 9 or (h == 9 and m >= 35)):
                    _caches_primed = True
                    def _prime():
                        try:
                            base = list(_dt.DEFAULT_SYMBOLS)
                            _dt._prime_catalyst_cache(base)
                            _dt._prime_bias_cache(base)
                            import ibkr_data as _id
                            for s in base:
                                _id.get_iv_rank(s)
                        except Exception as exc:
                            _logger.warning("cache prime failed: %s", exc)
                    _threading.Thread(target=_prime, daemon=True).start()
                    _logger.info("session cache prime started")

                # 5-min bar archive at 3:50 PM — builds the backtest dataset
                if not _bars_archived and h == 15 and m >= 50:
                    _bars_archived = True
                    _threading.Thread(target=_dt._archive_today_bars,
                                      args=(list(_dt.DEFAULT_SYMBOLS),),
                                      daemon=True).start()
                    _logger.info("bar archive triggered from api.py")
                # Check every cycle if any open trade hit its stop or target
                try:
                    closed = _dt.check_open_trades()
                    for t in closed:
                        _dt._notify_exit(t)
                        _sse_push("trade_closed",
                                  symbol=t["symbol"], direction=t["direction"],
                                  status=t["status"], pnl_pct=t.get("pnl_pct"))
                        _logger.info("trade closed  %s %s  status=%s  pnl=%.2f%%",
                                     t["symbol"], t["direction"],
                                     t["status"], t.get("pnl_pct") or 0)
                except Exception as exc:
                    _logger.warning("check_open_trades error: %s", exc)

                # EOD swing sweep — fires once at 3:45 PM
                if not _swing_swept and h == 15 and m >= 45:
                    _swing_swept = True
                    _threading.Thread(target=_dt._eod_swing_sweep, daemon=True).start()
                    _logger.info("EOD swing sweep triggered from api.py")
                _do_scan()
            else:
                # Outside hours: still update regime so badge isn't stuck on "unknown"
                try:
                    _, regime_label = _dt.intraday_regime()
                    with _intraday_lock:
                        _intraday_cache["regime"]      = regime_label
                        _intraday_cache["market_open"] = False
                except Exception:
                    pass

        _time.sleep(300)   # 5-minute cadence


# Start scanner daemon on first import
_scanner_thread = _threading.Thread(target=_run_intraday_scan, daemon=True)
_scanner_thread.start()


@app.route("/api/intraday_scan")
def api_intraday_scan():
    """Returns latest cached intraday signals, merged with today's log file.
    Merging ensures signals from the standalone day_trading.py runner also appear."""
    if not _DT_AVAILABLE:
        return jsonify({"error": "day_trading.py not found"}), 500
    with _intraday_lock:
        data = dict(_intraday_cache)
        data["signals"] = list(data.get("signals", []))

    import json as _json
    today = datetime.today().strftime("%Y-%m-%d")
    try:
        paper_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "day_trades.json")
        if os.path.exists(paper_file):
            with _dt._day_trades_lock, open(paper_file) as f:
                log_data = _json.load(f)
            trades = log_data.get("trades", []) if isinstance(log_data, dict) else log_data
            today_sigs = [t for t in trades if t.get("date") == today]
            cache_keys = {(s["symbol"], s["direction"]) for s in data["signals"]}
            for t in today_sigs:
                key = (t.get("symbol"), t.get("direction"))
                if key not in cache_keys:
                    data["signals"].append(t)
                    cache_keys.add(key)
            data["signals"].sort(key=lambda x: -x.get("score", 0))
    except Exception:
        pass

    return jsonify(data)


@app.route("/api/events")
def api_events():
    """SSE stream — pushes new_signal events to connected browser tabs instantly."""
    def stream():
        q = _queue.Queue(maxsize=20)
        with _sse_clients_lock:
            _sse_clients.append(q)
        try:
            yield "data: {\"type\": \"connected\"}\n\n"
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except _queue.Empty:
                    yield "data: {\"type\": \"heartbeat\"}\n\n"
        finally:
            with _sse_clients_lock:
                try:
                    _sse_clients.remove(q)
                except ValueError:
                    pass
    return Response(
        stream_with_context(stream()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/intraday_force_scan", methods=["POST"])
def api_intraday_force_scan():
    """Trigger an immediate scan regardless of market hours (for testing)."""
    if not _DT_AVAILABLE:
        return jsonify({"error": "day_trading.py not found"}), 500
    _threading.Thread(target=_do_scan, kwargs={"force": True}, daemon=True).start()
    return jsonify({"status": "scan started"})


@app.route("/api/intraday_regime")
def api_intraday_regime():
    """Returns current intraday regime (SPY VWAP + EMA)."""
    if not _DT_AVAILABLE:
        return jsonify({"error": "day_trading.py not found"}), 500
    try:
        bull, label = _dt.intraday_regime()
        return jsonify({"bull": bull, "label": label})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/swing_watchlist")
def api_swing_watchlist():
    """Returns the EOD swing trade watchlist generated at 3:45 PM."""
    import json as _json
    wl_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "swing_watchlist.json")
    try:
        with open(wl_file) as f:
            return jsonify(_json.load(f))
    except FileNotFoundError:
        return jsonify({"date": None, "setups": [],
                        "message": "No watchlist yet — generated at 3:45 PM ET after Grade S signals"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/today_signals")
def api_today_signals():
    """Returns all intraday signals logged today from day_trades.json."""
    import json as _json
    today = datetime.today().strftime("%Y-%m-%d")
    try:
        paper_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "day_trades.json")
        if not os.path.exists(paper_file):
            return jsonify({"trades": [], "date": today})
        with _dt._day_trades_lock, open(paper_file) as f:
            data = _json.load(f)
        trades = data.get("trades", []) if isinstance(data, dict) else data
        today_trades = [t for t in trades if t.get("date") == today]
        today_trades.sort(key=lambda x: x.get("time", ""), reverse=True)
        # Attach swing option suggestion (OTM 7-14 DTE) to each trade on-the-fly
        for t in today_trades:
            if not t.get("swing_option") and t.get("entry"):
                try:
                    t["swing_option"] = _dt.option_suggestion_swing(t)
                except Exception:
                    pass
        return jsonify({"trades": today_trades, "date": today})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/best_trade")
def api_best_trade():
    """Returns today's highest-score CALL signal with option suggestion."""
    import json as _json
    today = datetime.today().strftime("%Y-%m-%d")
    try:
        paper_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "day_trades.json")
        if not os.path.exists(paper_file):
            return jsonify({"trade": None, "date": today})
        with _dt._day_trades_lock, open(paper_file) as f:
            data = _json.load(f)
        trades = data.get("trades", []) if isinstance(data, dict) else data
        calls = [t for t in trades if t.get("date") == today and t.get("direction") == "CALL"]
        if not calls:
            return jsonify({"trade": None, "date": today})
        best = max(calls, key=lambda t: t.get("score", 0))
        return jsonify({"trade": best, "date": today})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/intraday_chart/<symbol>")
def api_intraday_chart(symbol):
    """
    OHLCV bars + indicators + key levels + signal markers.

    Query params:
      tf  — "5m" (default, 10-day intraday) | "1d" (daily bars, 6 months)
    """
    if not _DT_AVAILABLE:
        return jsonify({"error": "day_trading.py not found"}), 500
    symbol = symbol.upper()
    tf     = request.args.get("tf", "5m")

    try:
        import json as _json
        import ibkr_data as _ibd

        # ── 1-min view: 2 days of 1-min bars (extended hours included) ──────────
        if tf == "1m":
            raw = _ibd.get_extended_hours_bars(symbol, "2d", interval="1m")
            if not raw:
                return jsonify({"error": f"no 1m data for {symbol}"}), 404
            bars_out = [{"t": b["t"], "o": round(b["o"],4), "h": round(b["h"],4),
                         "l": round(b["l"],4), "c": round(b["c"],4), "v": b["v"],
                         "rth": b.get("rth", True)} for b in raw]
            primary = _dt.fetch_intraday(symbol, "5m", "5d") or []
            ind     = _dt.compute_indicators(primary, []) if len(primary) >= 20 else None
            levels  = _build_levels(symbol, primary, ind)
            return jsonify({"symbol": symbol, "tf": "1m", "bars": bars_out,
                            "series": {}, "levels": levels, "markers": [], "indicators": {}})

        # ── 1-hour view: 30 days of 1-hour bars ──────────────────────────────
        if tf == "1h":
            raw = _ibd.get_intraday_bars(symbol, "1h", "30d")
            if not raw:
                return jsonify({"error": f"no 1h data for {symbol}"}), 404
            bars_out = [{"t": b[0], "o": round(b[1],4), "h": round(b[2],4),
                         "l": round(b[3],4), "c": round(b[4],4), "v": b[5]} for b in raw]
            primary = _dt.fetch_intraday(symbol, "5m", "5d") or []
            ind     = _dt.compute_indicators(primary, []) if len(primary) >= 20 else None
            levels  = _build_levels(symbol, primary, ind)
            return jsonify({"symbol": symbol, "tf": "1h", "bars": bars_out,
                            "series": {}, "levels": levels, "markers": [], "indicators": {}})

        # ── Daily view: 6 months of daily bars ───────────────────────────────
        if tf == "1d":
            daily_raw = _ibd.get_daily_bars(symbol, "6 M")
            if not daily_raw:
                return jsonify({"error": f"no daily data for {symbol}"}), 404
            bars_out = [
                {"t": int(b["date"].timestamp()) if hasattr(b["date"], "timestamp")
                      else int(b["date"]),
                 "o": round(b["open"],   4), "h": round(b["high"],   4),
                 "l": round(b["low"],    4), "c": round(b["close"],  4),
                 "v": b.get("volume", 0)}
                for b in daily_raw
            ]
            # Levels still computed from RTH bars + catalyst cache
            primary, htf = _dt.fetch_symbol_data(symbol)
            ind = _dt.compute_indicators(primary, htf) if primary else None
            levels = _build_levels(symbol, primary or [], ind)
            return jsonify({
                "symbol":  symbol, "tf": "1d",
                "bars":    bars_out,
                "series":  {},
                "levels":  levels,
                "markers": [],
                "indicators": {},
            })

        # ── Intraday view: 10 trading days of 5-min bars ─────────────────────
        primary = _dt.fetch_intraday(symbol, "5m", "10d")
        if not primary:
            # Fall back to 5d if broker doesn't have 10d
            primary = _dt.fetch_intraday(symbol, "5m", "5d")
        if not primary:
            return jsonify({"error": f"no intraday data for {symbol}"}), 404

        htf = _dt.fetch_intraday(symbol, "15m", "5d")
        htf = (htf or [])[-13:]
        ind = _dt.compute_indicators(primary, htf)

        bars_out = [
            {"t": b[0], "o": round(b[1], 4), "h": round(b[2], 4),
             "l": round(b[3], 4), "c": round(b[4], 4), "v": b[5]}
            for b in primary
        ]

        levels  = _build_levels(symbol, primary, ind)
        markers = _build_markers(symbol, primary)

        return jsonify({
            "symbol":     symbol, "tf": "5m",
            "bars":       bars_out,
            "series":     _dt.compute_chart_series(primary),
            "levels":     levels,
            "markers":    markers,
            "indicators": {
                k: ind.get(k)
                for k in ("vwap", "ema9", "ema21", "atr", "adx", "rsi", "rel_vol")
            } if ind else {},
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _build_levels(symbol, primary, ind):
    """
    Build the key-levels dict for the chart.
    Priority: IBKR extended-hours data (get_premarket_gap) > bar-computed > cache.
    Levels returned:
      pdh  — Previous Day High      (thick red line)
      pdl  — Previous Day Low       (thick green line)
      pdc  — Previous Day Close     (yellow dotted)
      pmh  — Pre-market High        (orange dashed)
      pml  — Pre-market Low         (blue dashed)
      ah_high — After-hours High    (purple dashed)  — best-effort
      ah_low  — After-hours Low     (purple dashed)
      vwap, h1_swing_high, h1_swing_low
    """
    import ibkr_data as _ibd
    _today = datetime.now(tz=_ET).date()

    # ── Compute from RTH bars (always available) ──────────────────────────────
    _day_map, _pm_bars = {}, []
    for b in primary:
        bdt = datetime.fromtimestamp(b[0], tz=_ET)
        _day_map.setdefault(bdt.date(), []).append(b)
        if (bdt.date() == _today and
                (bdt.hour < 9 or (bdt.hour == 9 and bdt.minute < 30))):
            _pm_bars.append(b)

    _prev = sorted((d for d in _day_map if d < _today), reverse=True)

    pdh = pdl = pdc = None
    if _prev:
        _pb  = _day_map[_prev[0]]
        pdh  = round(max(b[2] for b in _pb), 4)
        pdl  = round(min(b[3] for b in _pb), 4)
        pdc  = round(_pb[-1][4], 4)           # last close of previous RTH day

    pmh = round(max(b[2] for b in _pm_bars), 4) if _pm_bars else None
    pml = round(min(b[3] for b in _pm_bars), 4) if _pm_bars else None

    # ── Prefer IBKR extended-hours data (get_premarket_gap has PMH/PML/PDC) ──
    ah_high = ah_low = None
    try:
        gp = _ibd.get_premarket_gap(symbol)
        if gp:
            pdh  = gp.get("prev_day_high") or pdh
            pdl  = gp.get("prev_day_low")  or pdl
            pdc  = gp.get("prev_close")    or pdc
            pmh  = gp.get("pm_high")       or pmh
            pml  = gp.get("pm_low")        or pml
    except Exception:
        pass

    # ── Catalyst cache as final merge ─────────────────────────────────────────
    with _dt._catalyst_lock:
        cat = dict(_dt._catalyst_cache.get(symbol, {}))
    pdh = cat.get("prev_day_high") or pdh
    pdl = cat.get("prev_day_low")  or pdl
    pmh = cat.get("pm_high")       or pmh
    pml = cat.get("pm_low")        or pml

    return {
        "pdh":          pdh,
        "pdl":          pdl,
        "pdc":          pdc,
        "pmh":          pmh,
        "pml":          pml,
        "ah_high":      ah_high,
        "ah_low":       ah_low,
        "vwap":         ind.get("vwap")          if ind else None,
        "h1_swing_high": ind.get("h1_swing_high") if ind else None,
        "h1_swing_low":  ind.get("h1_swing_low")  if ind else None,
    }


def _build_markers(symbol, primary):
    """Signal triangles from day_trades.json for the intraday chart."""
    import json as _json
    today   = datetime.now(tz=_ET).strftime("%Y-%m-%d")
    markers = []
    try:
        paper_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "day_trades.json")
        with _dt._day_trades_lock:
            with open(paper_file) as _f:
                _raw = _json.load(_f)
        for t in (_raw.get("trades", []) if isinstance(_raw, dict) else _raw):
            if t.get("symbol") == symbol and t.get("date") == today:
                try:
                    from datetime import datetime as _dt2
                    bar_date = datetime.fromtimestamp(primary[-1][0], tz=_ET).date()
                    hm = t.get("time", "09:30").split(":")
                    ts = int(_dt2(bar_date.year, bar_date.month, bar_date.day,
                                  int(hm[0]), int(hm[1]), tzinfo=_ET).timestamp())
                    markers.append({
                        "time": ts, "direction": t.get("direction"),
                        "grade": t.get("grade"), "score": t.get("score"),
                        "entry": t.get("entry"), "stop": t.get("stop"),
                        "target": t.get("target"), "pnl": t.get("pnl_pct"),
                        "status": t.get("status"),
                    })
                except Exception:
                    pass
    except Exception:
        pass
    return markers


# ── Price snapshot helpers (Log Open / Log 3:45 buttons) ──────────────────────

def _snapshot_prices(field):
    """Fetch live prices for today's signals and stamp field onto day_trades.json."""
    import json as _json, yfinance as _yf
    today = datetime.today().strftime("%Y-%m-%d")
    paper_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "day_trades.json")
    try:
        with _dt._day_trades_lock:
            if not os.path.exists(paper_file):
                return jsonify({"error": "no trades file", "updated": 0})
            with open(paper_file) as f:
                data = _json.load(f)
            raw = data.get("trades", data) if isinstance(data, dict) else data
            today_trades = [t for t in raw if t.get("date") == today]
            if not today_trades:
                return jsonify({"updated": 0, "field": field, "date": today, "prices": {}})

            symbols = list({t["symbol"] for t in today_trades})
            prices = {}
            for sym in symbols:
                try:
                    px = _yf.Ticker(sym).fast_info.last_price
                    if px:
                        prices[sym] = round(float(px), 2)
                except Exception:
                    pass

            updated = 0
            for t in raw:
                if t.get("date") == today and t.get("symbol") in prices:
                    t[field] = prices[t["symbol"]]
                    updated += 1

            out = dict(data) if isinstance(data, dict) else raw
            if isinstance(data, dict):
                out["trades"] = raw
            with open(paper_file, "w") as f:
                _json.dump(out, f, indent=2)

        now_str = datetime.now(tz=_ET).strftime("%H:%M:%S ET")
        return jsonify({"updated": updated, "field": field, "date": today,
                        "prices": prices, "time": now_str})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/log_open", methods=["POST"])
def api_log_open():
    """Snapshot live prices as open_px for all of today's signals."""
    if not _DT_AVAILABLE:
        return jsonify({"error": "day_trading.py not found"}), 500
    return _snapshot_prices("open_px")


@app.route("/api/log_close", methods=["POST"])
def api_log_close():
    """Snapshot live prices as close_px for all of today's signals."""
    if not _DT_AVAILABLE:
        return jsonify({"error": "day_trading.py not found"}), 500
    return _snapshot_prices("close_px")


@app.route("/api/validate_today")
def api_validate_today():
    """Validate today's signals: compare close_px vs open_px/entry per direction."""
    import json as _json
    today = datetime.today().strftime("%Y-%m-%d")
    paper_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "day_trades.json")
    try:
        if not os.path.exists(paper_file):
            return jsonify({"results": [], "date": today, "validated": 0})
        with _dt._day_trades_lock, open(paper_file) as f:
            data = _json.load(f)
        raw = data.get("trades", data) if isinstance(data, dict) else data
        today_trades = [t for t in raw if t.get("date") == today]

        results = []
        for t in today_trades:
            entry  = t.get("open_px") or t.get("entry")
            close  = t.get("close_px")
            dirn   = t.get("direction", "CALL")
            stop   = t.get("stop")
            target = t.get("target")
            rec = {
                "symbol": t["symbol"], "direction": dirn,
                "grade":  t.get("grade"), "score": t.get("score"),
                "time":   t.get("time"),
                "entry":  entry, "close": close,
                "open_px": t.get("open_px"), "close_px": close,
                "stop": stop, "target": target,
            }
            if entry and close:
                move_pct = round((close - entry) / entry * 100, 2)
                if dirn == "CALL":
                    hit_target = target and close >= target
                    hit_stop   = stop   and close <= stop
                    win        = close > entry
                    pnl        = move_pct
                else:
                    hit_target = target and close <= target
                    hit_stop   = stop   and close >= stop
                    win        = close < entry
                    pnl        = -move_pct
                rec.update({
                    "pnl_pct":    round(pnl, 2),
                    "win":        win,
                    "hit_target": bool(hit_target),
                    "hit_stop":   bool(hit_stop),
                    "outcome":    "TARGET" if hit_target else ("STOP" if hit_stop else ("WIN" if win else "LOSS")),
                })
            else:
                rec["outcome"] = "NO_DATA"
            results.append(rec)

        results.sort(key=lambda x: x.get("score", 0) or 0, reverse=True)
        validated = [r for r in results if r["outcome"] != "NO_DATA"]
        wins      = sum(1 for r in validated if r.get("win"))
        targets   = sum(1 for r in validated if r.get("hit_target"))
        stops     = sum(1 for r in validated if r.get("hit_stop"))
        return jsonify({
            "results":  results,
            "date":     today,
            "total":    len(results),
            "validated": len(validated),
            "wins":     wins,
            "targets":  targets,
            "stops":    stops,
            "win_rate": round(wins / len(validated) * 100, 1) if validated else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Async backtest job store ──────────────────────────────────────────────────
_bt_jobs = {}   # job_id → {status, result, error, started, elapsed}

@app.route("/api/backtest/run")
def api_backtest_run():
    """
    Starts a backtest in the background and returns a job_id immediately.
    Poll GET /api/backtest/status/<job_id> for result.
    """
    import subprocess, json as _json2, sys as _sys, uuid as _uuid
    symbols_param = request.args.get("symbols", "default")
    threshold     = request.args.get("threshold", "90")
    hold          = request.args.get("hold", "26")
    enable_puts   = request.args.get("puts", "true").lower() in ("1", "true", "yes")

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "intraday_backtest_2yr.py")
    if not os.path.exists(script):
        return jsonify({"error": "intraday_backtest_2yr.py not found"}), 500

    syms = []
    if symbols_param != "default":
        syms = [s.strip().upper() for s in symbols_param.split(",") if s.strip()]

    job_id = _uuid.uuid4().hex[:10]
    _bt_jobs[job_id] = {"status": "running", "result": None, "error": None,
                        "started": _time.time(), "n_symbols": len(syms) or "default"}

    def _run():
        cmd = [_sys.executable, script, "--threshold", threshold, "--hold", hold, "--json"]
        if enable_puts:
            cmd.append("--enable-puts")
        if syms:
            cmd += ["--symbols"] + syms
        try:
            env = os.environ.copy()
            env["IBKR_CLIENT_ID"] = "45"
            # 10-min timeout covers even uncached S&P500 in batches
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
            elapsed = round(_time.time() - _bt_jobs[job_id]["started"], 1)
            if proc.returncode != 0:
                _bt_jobs[job_id].update({"status": "error",
                    "error": proc.stderr[-600:] or "backtest process failed", "elapsed": elapsed})
                return
            try:
                # Script prints human-readable headers first, then a JSON blob.
                # Find the first '{' so we skip the progress text.
                raw = proc.stdout
                json_start = raw.find('{')
                if json_start == -1:
                    raise _json2.JSONDecodeError("no JSON object found", raw, 0)
                data = _json2.loads(raw[json_start:])
                _bt_jobs[job_id].update({"status": "done", "result": data, "elapsed": elapsed})

                # ── Auto-save to backtest_history.json ───────────────────
                if not data.get("no_trades"):
                    try:
                        _hist_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "data", "backtest_history.json")
                        try:
                            with open(_hist_file) as _hf:
                                _history = _json2.load(_hf)
                        except (FileNotFoundError, _json2.JSONDecodeError):
                            _history = []
                        _history.append({
                            "run_at":    datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "symbols":   syms or "default",
                            "threshold": float(threshold),
                            "hold":      int(hold),
                            "elapsed_s": elapsed,
                            "result":    data,
                        })
                        with open(_hist_file, "w") as _hf:
                            _json2.dump(_history, _hf, indent=2)
                    except Exception:
                        pass  # never block the response for a save failure
            except _json2.JSONDecodeError:
                _bt_jobs[job_id].update({"status": "error",
                    "error": "output not parseable: " + proc.stdout[-400:], "elapsed": elapsed})
        except subprocess.TimeoutExpired:
            _bt_jobs[job_id].update({"status": "error",
                "error": "Timed out after 10 min — pre-cache bars first or use fewer symbols",
                "elapsed": 600})
        except Exception as exc:
            _bt_jobs[job_id].update({"status": "error", "error": str(exc),
                "elapsed": round(_time.time() - _bt_jobs[job_id]["started"], 1)})

    _threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "status": "running"})


@app.route("/api/backtest/status/<job_id>")
def api_backtest_status(job_id):
    job = _bt_jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    out = {"status": job["status"], "n_symbols": job["n_symbols"],
           "elapsed": round(_time.time() - job["started"], 1)}
    if job["status"] == "done":
        out.update({"result": job["result"], "elapsed": job["elapsed"]})
    elif job["status"] == "error":
        out.update({"error": job["error"], "elapsed": job.get("elapsed", 0)})
    return jsonify(out)


@app.route("/api/backtest/history")
def api_backtest_history():
    """Return all saved backtest runs from data/backtest_history.json."""
    import json as _json3
    hist_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "backtest_history.json")
    try:
        with open(hist_file) as f:
            history = _json3.load(f)
        return jsonify({"runs": history})
    except FileNotFoundError:
        return jsonify({"runs": []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Pre-cache job store ───────────────────────────────────────────────────────
_pc_jobs = {}   # job_id → {status, lines, n_done, n_total, started}

@app.route("/api/precache/run")
def api_precache_run():
    """Start a background bar pre-cache for the given symbol list.  Returns job_id."""
    import subprocess as _sub, sys as _sys2, uuid as _uuid2

    symbols_param = request.args.get("symbols", "default")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "intraday_backtest_2yr.py")
    if not os.path.exists(script):
        return jsonify({"error": "backtest script not found"}), 500

    syms = []
    if symbols_param != "default":
        syms = [s.strip().upper() for s in symbols_param.split(",") if s.strip()]

    job_id = _uuid2.uuid4().hex[:10]
    n_total = len(syms) + 1 if syms else len(__import__("day_trading").DEFAULT_SYMBOLS) + 1
    _pc_jobs[job_id] = {"status": "running", "lines": [], "n_done": 0,
                        "n_total": n_total, "started": _time.time()}

    def _run():
        cmd = [_sys2.executable, script, "--precache-only"]
        if syms:
            cmd += ["--symbols"] + syms
        try:
            env = os.environ.copy()
            env["IBKR_CLIENT_ID"] = "45"
            proc = _sub.Popen(cmd, stdout=_sub.PIPE, stderr=_sub.STDOUT,
                              text=True, env=env)
            for line in proc.stdout:
                line = line.rstrip()
                _pc_jobs[job_id]["lines"].append(line)
                if line.startswith("    ["):   # fetch progress lines
                    try:
                        done = int(line.split("[")[1].split("/")[0].strip())
                        _pc_jobs[job_id]["n_done"] = done
                    except Exception:
                        pass
                if line.startswith("PRECACHE_DONE"):
                    _pc_jobs[job_id]["status"] = "done"
            proc.wait()
            if _pc_jobs[job_id]["status"] == "running":
                _pc_jobs[job_id]["status"] = "done"
        except Exception as exc:
            _pc_jobs[job_id].update({"status": "error", "lines": [str(exc)]})

    _threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "n_total": n_total})


@app.route("/api/precache/status/<job_id>")
def api_precache_status(job_id):
    job = _pc_jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    elapsed = round(_time.time() - job["started"], 1)
    return jsonify({
        "status":  job["status"],
        "n_done":  job["n_done"],
        "n_total": job["n_total"],
        "elapsed": elapsed,
        "lines":   job["lines"][-30:],   # last 30 lines only
    })



# ── Pre-market scan ───────────────────────────────────────────────────────────
_pm_jobs = {}   # job_id → {status, result, error, started}

@app.route("/api/premarket_scan/run")
def api_premarket_scan_run():
    """Start a background pre-market scan and return job_id immediately."""
    import uuid as _uuid3
    import ibkr_data as _ibd2
    from day_trading import DEFAULT_SYMBOLS as _DS

    min_gap = float(request.args.get("min_gap", 1.0))
    min_vol = float(request.args.get("min_vol", 1.0))
    top     = int(request.args.get("top", 15))

    # Canadian exchange stocks have no US pre-market session — skip them
    _DS = [s for s in _DS if not any(s.endswith(x) for x in (".TO", ".V", ".CN"))]

    job_id = _uuid3.uuid4().hex[:10]
    _pm_jobs[job_id] = {"status": "running", "result": None, "error": None,
                        "started": _time.time()}

    def _run():
        import yfinance as _yf
        import ibkr_data as _ibd3

        # Phase 1 — parallel Yahoo fetch for gap% (fast, ~3s for all symbols)
        def _gap_only(sym):
            try:
                info       = _yf.Ticker(sym).fast_info
                prev_close = float(info.previous_close or 0)
                pm_price   = float(info.last_price or prev_close)
                avg_vol    = float(info.three_month_average_volume or 1)
                if not prev_close:
                    return None
                gap = (pm_price - prev_close) / prev_close * 100
                return {"symbol": sym, "gap_pct": round(gap, 2),
                        "pm_price": round(pm_price, 2),
                        "prev_close": round(prev_close, 2),
                        "avg_vol": avg_vol}
            except Exception:
                return None

        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=10) as ex:
            phase1 = [r for r in ex.map(_gap_only, _DS) if r]

        # Phase 2 — IBKR volume only for symbols that pass gap filter
        candidates = [r for r in phase1 if abs(r["gap_pct"]) >= min_gap]

        for r in candidates:
            try:
                d = _ibd3.get_premarket_gap(r["symbol"])
                if d and d.get("pm_volume", 0) > 0:
                    r["pm_volume"] = d["pm_volume"]
                    r["vol_ratio"] = round(d["vol_ratio"], 2)
                else:
                    r["pm_volume"] = 0
                    r["vol_ratio"] = 0.0
            except Exception:
                r["pm_volume"] = 0
                r["vol_ratio"] = 0.0

        results = []
        for r in candidates:
            vol = r.get("vol_ratio", 0.0)
            if abs(r["gap_pct"]) < min_gap and vol < min_vol:
                continue
            r["score"] = round(abs(r["gap_pct"]) * max(vol, 0.1), 2)
            r.pop("avg_vol", None)
            results.append(r)

        results.sort(key=lambda r: r["score"], reverse=True)
        elapsed = round(_time.time() - _pm_jobs[job_id]["started"], 1)
        _pm_jobs[job_id].update({"status": "done", "result": results[:top],
                                  "elapsed": elapsed})

    _threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id, "n_symbols": len(_DS)})


@app.route("/api/premarket_scan/status/<job_id>")
def api_premarket_scan_status(job_id):
    job = _pm_jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    elapsed = round(_time.time() - job["started"], 1)
    out = {"status": job["status"], "elapsed": elapsed}
    if job["status"] == "done":
        out["result"]  = job["result"]
        out["elapsed"] = job["elapsed"]
    elif job["status"] == "error":
        out["error"] = job["error"]
    return jsonify(out)


@app.route("/api/live_performance")
def api_live_performance():
    """
    Per-symbol live performance stats from day_trades.json.
    Query params:
      days      — look-back window (default 14)
      min_trades — minimum closed trades to include (default 2)
      grade     — filter by grade S/A/B/C (optional)
    """
    import json as _json
    from datetime import timezone, timedelta
    from collections import defaultdict

    days       = int(request.args.get("days", 14))
    min_trades = int(request.args.get("min_trades", 2))
    grade_f    = request.args.get("grade", None)

    paper_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "day_trades.json")
    try:
        if _DT_AVAILABLE:
            with _dt._day_trades_lock:
                with open(paper_file) as f:
                    raw = _json.load(f)
        else:
            with open(paper_file) as f:
                raw = _json.load(f)
    except Exception:
        return jsonify({"symbols": [], "days": days})

    trades = raw.get("trades", raw) if isinstance(raw, dict) else raw
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    trades = [t for t in trades if t.get("date", "") >= cutoff and t.get("pnl_pct") is not None]
    if grade_f:
        trades = [t for t in trades if t.get("grade") == grade_f]

    by_sym = defaultdict(list)
    for t in trades:
        if t.get("symbol"):
            by_sym[t["symbol"]].append(t)

    symbols_out = []
    for sym, sym_trades in sorted(by_sym.items()):
        closed = sym_trades
        if len(closed) < min_trades:
            continue
        wins   = [t["pnl_pct"] for t in closed if t["pnl_pct"] > 0]
        losses = [t["pnl_pct"] for t in closed if t["pnl_pct"] <= 0]
        n      = len(closed)
        wr     = round(len(wins) / n * 100, 1)
        avg    = round(sum(t["pnl_pct"] for t in closed) / n, 3)
        gross_w = sum(wins) if wins else 0
        gross_l = abs(sum(losses)) if losses else 0
        pf      = round(gross_w / gross_l if gross_l > 0 else (99 if gross_w > 0 else 0), 2)

        verdict = ("KEEP"   if wr >= 50 and pf >= 1.0 else
                   "WATCH"  if wr >= 45 or pf >= 0.9  else "REMOVE")

        symbols_out.append({
            "symbol":  sym, "n": n, "wr": wr, "avg_pnl": avg, "pf": pf,
            "best":    round(max(t["pnl_pct"] for t in closed), 2),
            "worst":   round(min(t["pnl_pct"] for t in closed), 2),
            "verdict": verdict,
        })

    symbols_out.sort(key=lambda r: (
        0 if r["verdict"] == "KEEP" else 1 if r["verdict"] == "WATCH" else 2,
        -r["wr"]
    ))
    return jsonify({
        "symbols": symbols_out,
        "days":    days,
        "cutoff":  cutoff,
        "total_trades": len(trades),
    })


@app.route("/api/universe/<index>")
def api_universe(index):
    """
    Return constituent tickers for a major index.
    index: sp500 | nasdaq100 | dj30
    Used by the Backtest UI to populate universe selectors.
    """
    try:
        import ibkr_data as _ibd
        tickers = _ibd.get_index_tickers(index.lower())
        if not tickers:
            return jsonify({"error": f"unknown index: {index}"}), 400
        return jsonify({"index": index, "count": len(tickers), "tickers": tickers})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/extended_chart/<symbol>")
def api_extended_chart(symbol):
    """
    5-min bars with pre-market + after-hours included.
    Each bar has {t,o,h,l,c,v,rth} — rth=false for extended sessions.
    Used exclusively by the Chart tab for visual context.
    """
    symbol = symbol.upper()
    period = request.args.get("period", "5d")
    try:
        import ibkr_data as _ibd
        bars = _ibd.get_extended_hours_bars(symbol, period)
        return jsonify({"symbol": symbol, "bars": bars, "count": len(bars)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("\n  Multi-Algo Signals API")
    print("  Algos: largecap | spx | smallcap")
    print("  App (React build): http://localhost:5000/")
    print("  Dev mode (Vite):   cd web && npm run dev  → http://localhost:3000\n")
    app.run(debug=False, port=5000)
