"""
Backtest Engine for Trading Signal Assistant
=============================================
Tests the scoring logic against historical data to validate
whether the BUY signals actually produce positive returns.

What it does:
  1. Downloads 1 year of daily data per ticker
  2. Walks day-by-day (no lookahead bias)
  3. On each day, computes all indicators using only past data
  4. When score >= threshold → simulates a BUY
  5. Exits at +12% target OR -5% stop OR after max_hold_days
  6. Reports win rate, avg return, Sharpe ratio, drawdown

Setup:  pip install requests rich
Run:
  python3 backtest.py                        # default watchlist
  python3 backtest.py MARA BTE.TO SNAP       # specific tickers
  python3 backtest.py --threshold 6.5        # try different score threshold
  python3 backtest.py --optimize             # find best threshold automatically
"""

import sys, os, json, time, random, requests, argparse
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core_signals import (
    _calc_rsi  as calc_rsi,
    _calc_ema  as calc_ema,
    _calc_sma  as calc_sma,
    _calc_cmf  as calc_cmf,
)

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    from rich.panel import Panel
    from rich.text import Text
    RICH = True
    console = Console()
except ImportError:
    RICH = False

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_TICKERS = [
    # US large-cap (mega-tech, semis, cloud, consumer)
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AMD","AVGO",
    "NOW","CRWD","DDOG","SNOW","ADBE","ZS","NET","COST","NFLX","ISRG",
    # TSX large-cap (banks, energy, gold, tech, telecom)
    "RY.TO","TD.TO","CNQ.TO","ENB.TO","SU.TO",
    "ABX.TO","FNV.TO","SHOP.TO","CP.TO","BCE.TO","T.TO","ATD.TO",
]

MAX_HOLD_DAYS    = 15     # exit after this many days if no target/stop hit
COMMISSION_PCT   = 0.005  # 0.5% round trip (Wealthsimple USD estimate)
SLIPPAGE_PCT     = 0.001  # 0.1% per leg (market impact / spread)
STARTING_CAPITAL = 109.0
MAX_POSITION_PCT = 0.30   # max 30% of capital per trade
DEFAULT_THRESHOLD= 6.5    # calibrated via --optimize on large-cap universe

# ── Helpers ───────────────────────────────────────────────────────────────────
def cprint(msg, style=""):
    if RICH: console.print(msg, style=style)
    else:    print(msg)

_last_req = 0.0
def throttled_get(url, tries=4):
    global _last_req
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for attempt in range(tries):
        gap = 0.4 - (time.time() - _last_req)
        if gap > 0: time.sleep(gap + random.uniform(0.05, 0.15))
        _last_req = time.time()
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 429:
                time.sleep((attempt+1)*2.5)
                continue
            if r.status_code == 200:
                return r
        except Exception:
            time.sleep((attempt+1)*1.5)
    return None

# ── Market regime ────────────────────────────────────────────────────────────
def build_regime_map(spy_rows, xiu_rows, sma_period=50):
    """
    Returns {date_str: bool} — True if SPY or XIU was above its SMA50 on that date.
    Uses only past data at each bar (no lookahead).
    """
    def regime_series(rows):
        closes = [r["close"] for r in rows]
        result = {}
        for i, r in enumerate(rows):
            if i >= sma_period:
                sma = sum(closes[i-sma_period:i]) / sma_period
                result[r["date"]] = closes[i] > sma
            else:
                result[r["date"]] = True  # not enough history → assume bullish
        return result

    spy_map = regime_series(spy_rows) if spy_rows else {}
    xiu_map = regime_series(xiu_rows) if xiu_rows else {}
    all_dates = set(spy_map) | set(xiu_map)
    return {d: spy_map.get(d, True) or xiu_map.get(d, True) for d in all_dates}

# ── Fetch 1 year of daily OHLCV ───────────────────────────────────────────────
def fetch_history(symbol):
    """Returns list of dicts: {date, open, high, low, close, volume}"""
    import ibkr_data as _id
    for duration in ["2 Y", "1 Y", "6 M"]:
        rows = _id.get_daily_bars(symbol.upper().strip(), duration)
        if len(rows) >= 30:
            return rows
    return []

# ── Backtest-specific helpers ─────────────────────────────────────────────────
def calc_macd(closes):
    e12 = calc_ema(closes,12)
    e26 = calc_ema(closes,26)
    return round(e12-e26,4) if e12 and e26 else None

def vol_spike(vols):
    if len(vols)<6: return 1.0
    avg = sum(vols[-6:-1])/5
    return round(vols[-1]/avg,2) if avg>0 else 1.0

# ── Score one day (no lookahead — only uses data up to index i) ───────────────
def score_day(rows, i):
    """
    Compute the signal score for day i using only rows[0:i+1].
    Returns (score, details_dict) or (None, {}) if not enough data.
    """
    if i < 30:   # need at least 30 days of history
        return None, {}

    closes = [r["close"]  for r in rows[:i+1]]
    highs  = [r["high"]   for r in rows[:i+1]]
    lows   = [r["low"]    for r in rows[:i+1]]
    vols   = [r["volume"] for r in rows[:i+1]]
    price  = closes[-1]
    prev   = closes[-2] if len(closes)>=2 else price
    change = (price-prev)/prev*100

    rsi    = calc_rsi(closes)
    e9     = calc_ema(closes,9)
    e21    = calc_ema(closes,21)
    macd   = calc_macd(closes)
    sma20  = calc_sma(closes,20)
    sma50  = calc_sma(closes,50)
    cmf    = calc_cmf(highs,lows,closes,vols,20)
    vs     = vol_spike(vols)

    # Support/resistance from last 30 days
    rh30   = highs[-30:]; rl30 = lows[-30:]
    resist = round(sum(sorted(rh30,reverse=True)[:3])/3,4) if len(rh30)>=3 else max(rh30)
    supp   = round(sum(sorted(rl30)[:3])/3,4)              if len(rl30)>=3 else min(rl30)
    sup_gap= (price-supp)/price*100   if supp   else None
    res_gap= (resist-price)/price*100 if resist else None

    # Avg daily range (volatility proxy)
    ranges = [(highs[j]-lows[j])/lows[j]*100 for j in range(-5,0) if lows[j]>0]
    vol_pct= round(sum(ranges)/len(ranges),2) if ranges else 3.0

    score = 0.0
    details = {"price":price,"rsi":rsi,"e9":e9,"e21":e21,
               "macd":macd,"vs":vs,"cmf":cmf,"change":change}

    # RSI
    if rsi is not None:
        if 30<=rsi<=45:  score+=2.0
        elif rsi<30:     score+=1.2
        elif rsi>72:     score-=1.0

    # EMA crossover
    if e9 and e21:
        if e9>e21:  score+=1.5
        else:       score-=1.0

    # MACD
    if macd is not None:
        if macd>0: score+=0.8
        else:      score-=0.5

    # DMA
    dma=0.0
    if sma20 and price>sma20: dma+=0.5
    if sma50 and price>sma50: dma+=0.7
    score+=dma

    # CMF
    if cmf is not None:
        if cmf>=0.10:  score+=1.0
        elif cmf<=-0.10: score-=0.5

    # Volume spike
    if vs>=1.8:   score+=1.6
    elif vs<1.0:  score-=0.4

    # Support proximity
    if sup_gap is not None:
        if sup_gap<=3.0:   score+=1.5
        elif sup_gap>=8.0: score-=0.8

    # Resistance proximity
    if res_gap is not None:
        if res_gap<=2.0:  score-=1.0
        elif res_gap>=6.0:score+=0.7

    # Day change
    if change>8:   score-=0.5
    elif -2<=change<=4: score+=0.5

    # Volatility-adjusted R/R (mirrors signals.py compute_trade_levels)
    stop_pct_used  = max(4.5, min(9.0, vol_pct * 1.2))
    reward_pct_used= max(stop_pct_used * 1.8, 8.0)
    rr = reward_pct_used / stop_pct_used
    if rr>=1.8:  score+=0.8
    elif rr<1.1: score-=0.5

    details["score"]    = round(score,2)
    details["sup_gap"]  = sup_gap
    details["res_gap"]  = res_gap
    details["vol_pct"]  = vol_pct
    return round(score,2), details

# ── Simulate one trade ────────────────────────────────────────────────────────
def simulate_trade(rows, entry_idx, capital):
    """
    Enter at open of entry_idx+1 (next day open, realistic).
    Exit when:
      - High touches take-profit  → win
      - Low  touches stop-loss    → loss
      - max_hold_days reached     → exit at close
    Returns dict with trade result.
    """
    if entry_idx+1 >= len(rows):
        return None

    entry_price = rows[entry_idx+1]["open"]
    if entry_price <= 0:
        return None
    entry_price_eff = round(entry_price * (1 + SLIPPAGE_PCT), 4)

    # Volatility-based stop/target — matches signals.py compute_trade_levels()
    signal_row  = rows[entry_idx]
    window      = rows[max(0, entry_idx-4):entry_idx+1]
    ranges      = [(r["high"]-r["low"])/r["low"]*100 for r in window if r["low"]>0]
    vol_pct     = sum(ranges)/len(ranges) if ranges else 3.0
    stop_pct    = max(4.5, min(9.0, vol_pct * 1.2)) / 100
    reward_pct  = max(stop_pct * 1.8, 0.08)
    stop        = round(entry_price * (1 - stop_pct),  4)
    target      = round(entry_price * (1 + reward_pct), 4)

    # Position size
    pos_dollars = min(capital*MAX_POSITION_PCT,
                      capital*0.90)   # never risk >90% of capital
    shares = max(1, int(pos_dollars/entry_price_eff))
    cost   = shares*entry_price_eff*(1+COMMISSION_PCT/2)

    if cost > capital:
        shares = max(1, int(capital*0.90/entry_price_eff))
        cost   = shares*entry_price_eff*(1+COMMISSION_PCT/2)

    exit_price  = None
    exit_reason = "timeout"
    hold_days   = 0
    trail_stop  = stop   # trailing stop starts at initial stop

    for j in range(entry_idx+2, min(entry_idx+2+MAX_HOLD_DAYS, len(rows))):
        hold_days += 1
        day = rows[j]

        # Trail the stop upward as price moves in our favour
        # +5% gain  → move stop to breakeven
        # +8% gain  → lock in 3% profit
        # +11% gain → lock in 6% profit
        high_so_far = day["high"]
        gain_pct = (high_so_far - entry_price) / entry_price * 100
        if gain_pct >= 11:
            trail_stop = max(trail_stop, round(entry_price * 1.06, 4))
        elif gain_pct >= 8:
            trail_stop = max(trail_stop, round(entry_price * 1.03, 4))
        elif gain_pct >= 5:
            trail_stop = max(trail_stop, entry_price)   # breakeven

        if day["low"] <= trail_stop:
            exit_price  = trail_stop
            exit_reason = "trail_stop" if trail_stop > stop else "stop"
            break
        if day["high"] >= target:
            exit_price  = target
            exit_reason = "target"
            break
    else:
        exit_price = rows[min(entry_idx+1+MAX_HOLD_DAYS, len(rows)-1)]["close"]

    exit_price_eff = round(exit_price * (1 - SLIPPAGE_PCT), 4)
    proceeds = shares*exit_price_eff*(1-COMMISSION_PCT/2)
    pnl      = proceeds - cost
    ret_pct  = pnl/cost*100

    return {
        "entry_date":  rows[entry_idx+1]["date"],
        "exit_date":   rows[min(entry_idx+2+hold_days, len(rows)-1)]["date"],
        "entry_price": round(entry_price_eff,4),
        "exit_price":  round(exit_price_eff,4),
        "shares":      shares,
        "cost":        round(cost,2),
        "proceeds":    round(proceeds,2),
        "pnl":         round(pnl,2),
        "ret_pct":     round(ret_pct,2),
        "exit_reason": exit_reason,
        "hold_days":   hold_days,
    }

# ── Backtest one ticker ───────────────────────────────────────────────────────
def _backtest_rows(rows, symbol, threshold, regime_map=None):
    """Score and simulate trades on pre-fetched rows. No network I/O."""
    trades          = []
    last_signal_day = -999
    exit_day        = -1
    capital         = STARTING_CAPITAL   # compounds with each trade result

    for i in range(30, len(rows)-2):
        if i <= exit_day:          # still holding a position
            continue
        if i - last_signal_day < 3:
            continue

        # Skip signals on bear-regime days
        if regime_map is not None:
            date = rows[i]["date"]
            if not regime_map.get(date, True):
                continue

        score, details = score_day(rows, i)
        if score is None:
            continue

        if score >= threshold:
            trade = simulate_trade(rows, i, capital)
            if trade:
                trade["symbol"]  = symbol
                trade["score"]   = score
                trade["details"] = details
                trade["capital_before"] = round(capital, 2)
                capital += trade["pnl"]   # compound: wins grow bankroll, losses shrink it
                capital  = max(capital, 1.0)  # floor at $1 so we never divide by zero
                trade["capital_after"] = round(capital, 2)
                trades.append(trade)
                last_signal_day  = i
                exit_day         = i + 1 + trade["hold_days"]

    return trades


def backtest_ticker(symbol, threshold=DEFAULT_THRESHOLD):
    print(f"  Fetching {symbol}...", end=" ", flush=True)
    rows = fetch_history(symbol)
    if len(rows) < 40:
        print(f"❌ not enough data ({len(rows)} rows)")
        return None
    print(f"✓ {len(rows)} days")
    return _backtest_rows(rows, symbol, threshold)

# ── Analytics ─────────────────────────────────────────────────────────────────
def compute_stats(all_trades):
    if not all_trades:
        return {}

    wins    = [t for t in all_trades if t["pnl"]>0]
    losses  = [t for t in all_trades if t["pnl"]<=0]
    rets    = [t["ret_pct"] for t in all_trades]
    targets = [t for t in all_trades if t["exit_reason"]=="target"]
    stops   = [t for t in all_trades if t["exit_reason"]=="stop"]
    timeouts= [t for t in all_trades if t["exit_reason"]=="timeout"]

    win_rate    = len(wins)/len(all_trades)*100
    avg_ret     = sum(rets)/len(rets)
    avg_win     = sum(t["ret_pct"] for t in wins)/len(wins)  if wins   else 0
    avg_loss    = sum(t["ret_pct"] for t in losses)/len(losses) if losses else 0
    profit_factor=(sum(t["pnl"] for t in wins)/abs(sum(t["pnl"] for t in losses))
                   if losses and sum(t["pnl"] for t in losses)!=0 else 999)

    # Sharpe (annualised, assume 252 trading days, daily returns)
    if len(rets)>1:
        mean_r = avg_ret/100
        std_r  = (sum((r/100-mean_r)**2 for r in rets)/(len(rets)-1))**0.5
        sharpe = round((mean_r/std_r)*252**0.5,2) if std_r>0 else 0
    else:
        sharpe = 0

    # Max drawdown (simulate equity curve with fixed $109 per trade)
    equity = STARTING_CAPITAL
    peak   = equity
    max_dd = 0.0
    for t in all_trades:
        equity += t["pnl"]
        if equity>peak: peak=equity
        dd = (peak-equity)/peak*100
        if dd>max_dd: max_dd=dd

    avg_hold = sum(t["hold_days"] for t in all_trades)/len(all_trades)

    return {
        "total":          len(all_trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(win_rate,1),
        "avg_ret":        round(avg_ret,2),
        "avg_win":        round(avg_win,2),
        "avg_loss":       round(avg_loss,2),
        "profit_factor":  round(profit_factor,2),
        "sharpe":         sharpe,
        "max_drawdown":   round(max_dd,1),
        "targets_hit":    len(targets),
        "stops_hit":      len(stops),
        "timeouts":       len(timeouts),
        "avg_hold_days":  round(avg_hold,1),
        "total_pnl":      round(sum(t["pnl"] for t in all_trades),2),
    }

def print_stats(stats, threshold):
    if not stats:
        cprint("[red]No trades generated.[/]" if RICH else "No trades generated.")
        return

    verdict = "✅ LOOKS PROMISING" if (stats["win_rate"]>=50 and stats["profit_factor"]>=1.2) \
              else "⚠️  NEEDS WORK"   if (stats["win_rate"]>=40) \
              else "❌ NOT PROFITABLE"

    if RICH:
        table = Table(title=f"Backtest Results  |  Threshold {threshold}  |  {verdict}",
                      box=box.SIMPLE_HEAVY)
        table.add_column("Metric",   style="cyan", width=22)
        table.add_column("Value",    justify="right", width=12)
        table.add_column("Target",   justify="right", width=12, style="dim")
        rows_data = [
            ("Total trades",       str(stats["total"]),          "≥ 20"),
            ("Win rate",           f"{stats['win_rate']}%",       "≥ 50%"),
            ("Avg return/trade",   f"{stats['avg_ret']:+.2f}%",   "> 0%"),
            ("Avg winner",         f"{stats['avg_win']:+.2f}%",   "> +5%"),
            ("Avg loser",          f"{stats['avg_loss']:+.2f}%",  "> -3%"),
            ("Profit factor",      f"{stats['profit_factor']}",   "≥ 1.5"),
            ("Sharpe ratio",       f"{stats['sharpe']}",          "≥ 1.0"),
            ("Max drawdown",       f"{stats['max_drawdown']}%",   "< 20%"),
            ("Targets hit",        str(stats["targets_hit"]),     ""),
            ("Stops hit",          str(stats["stops_hit"]),       ""),
            ("Timeouts",           str(stats["timeouts"]),        ""),
            ("Avg hold days",      str(stats["avg_hold_days"]),   ""),
            ("Total P&L ($109/trade)", f"${stats['total_pnl']:+.2f}", "> $0"),
        ]
        for metric, val, target in rows_data:
            style = ""
            if "%" in val and "rate" in metric.lower():
                style = "green" if float(val.strip("%"))>=50 else "red"
            elif "profit" in metric.lower():
                style = "green" if float(val)>=1.2 else "red"
            table.add_row(metric, Text(val, style=style), target)
        console.print(table)
    else:
        print(f"\n{'═'*50}")
        print(f"  BACKTEST RESULTS  |  Threshold {threshold}  |  {verdict}")
        print(f"{'═'*50}")
        print(f"  Total trades    : {stats['total']}")
        print(f"  Win rate        : {stats['win_rate']}%  (target ≥50%)")
        print(f"  Avg return      : {stats['avg_ret']:+.2f}%")
        print(f"  Avg winner      : {stats['avg_win']:+.2f}%")
        print(f"  Avg loser       : {stats['avg_loss']:+.2f}%")
        print(f"  Profit factor   : {stats['profit_factor']}  (target ≥1.5)")
        print(f"  Sharpe ratio    : {stats['sharpe']}  (target ≥1.0)")
        print(f"  Max drawdown    : {stats['max_drawdown']}%  (target <20%)")
        print(f"  Total P&L       : ${stats['total_pnl']:+.2f}")
        print(f"{'═'*50}")

def print_trade_log(trades, show_all=False):
    if not trades: return
    show = trades if show_all else trades[:15]
    if RICH:
        table = Table(title="Trade Log", box=box.SIMPLE)
        for col in ["Symbol","Entry Date","Exit Date","Entry $","Exit $",
                    "Return","P&L","Reason","Score","Hold"]:
            table.add_column(col, justify="right" if col not in ("Symbol","Entry Date","Exit Date","Reason") else "left")
        for t in show:
            style = "green" if t["pnl"]>0 else "red"
            table.add_row(
                t["symbol"],
                t["entry_date"], t["exit_date"],
                f"${t['entry_price']:.2f}", f"${t['exit_price']:.2f}",
                Text(f"{t['ret_pct']:+.1f}%", style=style),
                Text(f"${t['pnl']:+.2f}",     style=style),
                t["exit_reason"],
                str(t["score"]),
                str(t["hold_days"])+"d",
            )
        console.print(table)
        if len(trades)>15 and not show_all:
            console.print(f"  [dim]... {len(trades)-15} more trades. Run with --all to see all.[/]")
    else:
        print(f"\n  {'SYM':<8} {'DATE':<12} {'ENTRY':>7} {'EXIT':>7} {'RET':>7} {'P&L':>7} {'REASON':<8} SCORE")
        for t in show:
            icon = "✓" if t["pnl"]>0 else "✗"
            print(f"  {t['symbol']:<8} {t['entry_date']:<12} "
                  f"${t['entry_price']:>6.2f} ${t['exit_price']:>6.2f} "
                  f"{t['ret_pct']:>+6.1f}% ${t['pnl']:>+6.2f} "
                  f"{icon} {t['exit_reason']:<8} {t['score']}")

# ── Threshold optimiser ────────────────────────────────────────────────────────
def optimize_threshold(all_trades_by_threshold):
    """Find the threshold with best risk-adjusted return."""
    results = []
    for threshold, trades in all_trades_by_threshold.items():
        stats = compute_stats(trades)
        if stats and stats["total"]>=20:
            # Score = win_rate * profit_factor / max(drawdown,1), penalise sparse samples
            composite = (stats["win_rate"]/100 * stats["profit_factor"]
                         / max(stats["max_drawdown"]/100, 0.01))
            results.append((threshold, composite, stats))
    if not results:
        return None, None
    results.sort(key=lambda x:x[1], reverse=True)
    best_threshold, best_composite, best_stats = results[0]

    if RICH:
        table = Table(title="Threshold Optimisation", box=box.SIMPLE)
        for col in ["Threshold","Trades","Win Rate","Profit Factor","Drawdown","Composite Score"]:
            table.add_column(col, justify="right")
        for thr,comp,st in results:
            style = "bold green" if thr==best_threshold else ""
            table.add_row(
                Text(str(thr), style=style),
                str(st["total"]),
                f"{st['win_rate']}%",
                str(st["profit_factor"]),
                f"{st['max_drawdown']}%",
                Text(f"{comp:.3f}", style=style),
            )
        console.print(table)
    else:
        print(f"\n{'─'*60}")
        print(f"  {'THRESHOLD':>10} {'TRADES':>7} {'WIN%':>7} {'PF':>6} {'DD%':>6} {'SCORE':>8}")
        for thr,comp,st in results:
            mark = " ← BEST" if thr==best_threshold else ""
            print(f"  {thr:>10} {st['total']:>7} {st['win_rate']:>6.1f}% "
                  f"{st['profit_factor']:>6.2f} {st['max_drawdown']:>5.1f}% {comp:>8.3f}{mark}")

    return best_threshold, best_stats

# ── Per-ticker summary ────────────────────────────────────────────────────────
def print_ticker_summary(per_ticker_trades):
    if RICH:
        table = Table(title="Results by Ticker", box=box.SIMPLE)
        for col in ["Ticker","Trades","Win Rate","Avg Ret","Total P&L","Best","Worst"]:
            table.add_column(col, justify="right" if col!="Ticker" else "left")
        for sym, trades in sorted(per_ticker_trades.items()):
            if not trades: continue
            wins = [t for t in trades if t["pnl"]>0]
            wr   = len(wins)/len(trades)*100
            avg  = sum(t["ret_pct"] for t in trades)/len(trades)
            total_pnl = sum(t["pnl"] for t in trades)
            best  = max(t["ret_pct"] for t in trades)
            worst = min(t["ret_pct"] for t in trades)
            style = "green" if total_pnl>0 else "red"
            table.add_row(
                sym, str(len(trades)),
                Text(f"{wr:.0f}%", style=style),
                Text(f"{avg:+.1f}%", style=style),
                Text(f"${total_pnl:+.2f}", style=style),
                f"{best:+.1f}%", f"{worst:+.1f}%",
            )
        console.print(table)
    else:
        print(f"\n  {'TICKER':<12} {'TRADES':>6} {'WIN%':>7} {'AVG':>7} {'TOTAL P&L':>10}")
        for sym, trades in sorted(per_ticker_trades.items()):
            if not trades: continue
            wins = [t for t in trades if t["pnl"]>0]
            wr   = len(wins)/len(trades)*100
            avg  = sum(t["ret_pct"] for t in trades)/len(trades)
            total_pnl = sum(t["pnl"] for t in trades)
            print(f"  {sym:<12} {len(trades):>6} {wr:>6.0f}% {avg:>+6.1f}% ${total_pnl:>+8.2f}")

# ── Walk-forward validation ───────────────────────────────────────────────────
def walk_forward(ticker_rows, threshold, regime_map, train_pct=0.67):
    """
    Split each ticker's history into train (first 67%) and test (last 33%).
    Optimise threshold on train, validate on test — prevents overfitting.
    """
    def split_rows(rows):
        cut = int(len(rows) * train_pct)
        return rows[:cut], rows[cut:]

    # Train: find best threshold on training window
    train_by_thr = {}
    for thr in [round(t*0.5,1) for t in range(8,17)]:
        train_trades = []
        for sym, rows in ticker_rows.items():
            train_rows, _ = split_rows(rows)
            # Build a regime_map slice for training dates
            train_dates = {r["date"] for r in train_rows}
            train_regime = {d:v for d,v in regime_map.items() if d in train_dates}
            train_trades.extend(_backtest_rows(train_rows, sym, thr, train_regime))
        train_by_thr[thr] = train_trades

    best_thr, _ = optimize_threshold(train_by_thr)
    if not best_thr:
        best_thr = threshold
        print("  ⚠  Walk-forward: not enough train trades, using default threshold")

    # Test: run best threshold on held-out test window
    test_trades = []
    for sym, rows in ticker_rows.items():
        _, test_rows = split_rows(rows)
        test_dates  = {r["date"] for r in test_rows}
        test_regime = {d:v for d,v in regime_map.items() if d in test_dates}
        test_trades.extend(_backtest_rows(test_rows, sym, best_thr, test_regime))

    test_stats = compute_stats(test_trades)

    if RICH:
        from rich.panel import Panel
        lines = [
            f"[bold]Train period:[/] first {int(train_pct*100)}% of data  →  best threshold: [cyan]{best_thr}[/]",
            f"[bold]Test period :[/] last  {int((1-train_pct)*100)}% of data  →  {len(test_trades)} trades",
        ]
        if test_stats:
            verdict = "✅ Generalises" if test_stats["win_rate"]>=45 else "⚠️  Weaker on unseen data"
            lines += [
                f"[bold]Win rate    :[/] [{'green' if test_stats['win_rate']>=45 else 'red'}]{test_stats['win_rate']:.1f}%[/]   (train target ≥47%)",
                f"[bold]Profit fac. :[/] {test_stats['profit_factor']}",
                f"[bold]Max DD      :[/] {test_stats['max_drawdown']}%",
                f"[bold]Verdict     :[/] {verdict}",
            ]
        console.print(Panel("\n".join(lines), title="🔁 Walk-Forward Result", border_style="cyan"))
    else:
        print(f"\n  Walk-Forward — train {int(train_pct*100)}% / test {int((1-train_pct)*100)}%")
        print(f"  Best threshold from train: {best_thr}")
        if test_stats:
            print(f"  Test win rate : {test_stats['win_rate']:.1f}%")
            print(f"  Test PF       : {test_stats['profit_factor']}")
            print(f"  Test max DD   : {test_stats['max_drawdown']}%")

    return best_thr, test_stats, test_trades

# ── Save results ──────────────────────────────────────────────────────────────
def save_results(all_trades, stats, threshold):
    fname = f"backtest_results_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    out   = {"threshold":threshold, "stats":stats,
             "trades":[{k:v for k,v in t.items() if k!="details"}
                        for t in all_trades],
             "generated":datetime.now().isoformat()}
    with open(fname,"w") as f:
        json.dump(out, f, indent=2)
    print(f"\n  💾 Results saved to {fname}")
    return fname

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Backtest trading signals")
    parser.add_argument("tickers",    nargs="*",   default=DEFAULT_TICKERS)
    parser.add_argument("--threshold",type=float,  default=DEFAULT_THRESHOLD)
    parser.add_argument("--optimize", action="store_true",
                        help="Test thresholds 4.0–8.0 and find the best")
    parser.add_argument("--all",      action="store_true",
                        help="Show full trade log")
    parser.add_argument("--save",         action="store_true",
                        help="Save results to JSON")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Train on first 67%% of data, validate on last 33%%")
    args = parser.parse_args()

    tickers = [t.upper() for t in args.tickers]

    if RICH:
        console.print(Panel.fit(
            "[bold cyan]📊 BACKTEST ENGINE[/]\n"
            f"[dim]Tickers: {', '.join(tickers)}\n"
            f"Stop/target: volatility-based (4.5–9% stop, 1.8R reward)  "
            f"Max hold: {MAX_HOLD_DAYS}d  "
            f"Commission: {COMMISSION_PCT*100:.1f}%[/]",
            border_style="cyan"))
    else:
        print(f"\n{'═'*55}")
        print(f"  📊 BACKTEST ENGINE")
        print(f"  Stop/target: volatility-based  Hold ≤{MAX_HOLD_DAYS}d  Commission {COMMISSION_PCT*100:.1f}%")
        print(f"{'═'*55}")

    # Build market regime map from SPY + XIU
    print(f"\nFetching market regime (SPY + XIU.TO)...")
    spy_rows = fetch_history("SPY")
    xiu_rows = fetch_history("XIU.TO")
    regime_map = build_regime_map(spy_rows, xiu_rows)
    bull_days  = sum(1 for v in regime_map.values() if v)
    print(f"  ✓ Regime: {bull_days}/{len(regime_map)} days bullish\n")

    # Fetch + score all tickers
    print(f"Fetching historical data...\n")
    per_ticker = {}
    for sym in tickers:
        print(f"  Fetching {sym}...", end=" ", flush=True)
        rows = fetch_history(sym)
        if len(rows) >= 40:
            trades = _backtest_rows(rows, sym, args.threshold, regime_map)
            per_ticker[sym] = trades
            print(f"✓ {len(rows)} days → {len(trades)} trades")
        else:
            print(f"❌ not enough data")

    all_trades = [t for trades in per_ticker.values() for t in trades]
    all_trades.sort(key=lambda x:x["entry_date"])

    if not all_trades:
        cprint("\n[red]No trades generated. Try lowering --threshold or adding more tickers.[/]"
               if RICH else "\nNo trades generated.")
        return

    print(f"\n  Generated {len(all_trades)} trades across {len(per_ticker)} tickers\n")

    # Build ticker_rows dict once — reused by both --optimize and --walk-forward
    ticker_rows = {sym: rows for sym, rows in
                   zip(tickers, [fetch_history(sym) for sym in tickers])
                   if len(rows) >= 40}

    if args.optimize or getattr(args, 'walk_forward', False):
        if args.optimize:
            print("\nOptimising threshold (testing 4.0 → 8.0)...\n")
            by_threshold = {}
            for thr in [round(t*0.5,1) for t in range(8,17)]:
                trades_at_thr = []
                for sym, rows in ticker_rows.items():
                    trades_at_thr.extend(_backtest_rows(rows, sym, thr, regime_map))
                by_threshold[thr] = trades_at_thr
            best_thr, best_stats = optimize_threshold(by_threshold)
            if best_thr:
                print(f"\n  ✅ Best threshold: {best_thr}")
                print(f"  Update DEFAULT_THRESHOLD = {best_thr} in signals.py")
                print_stats(best_stats, best_thr)

        if getattr(args, 'walk_forward', False):
            print("\nRunning walk-forward validation...\n")
            walk_forward(ticker_rows, args.threshold, regime_map)
    else:
        stats = compute_stats(all_trades)
        print_stats(stats, args.threshold)
        print_ticker_summary(per_ticker)
        print_trade_log(all_trades, show_all=args.all)

        # Guidance
        print("\n" + ("─"*55))
        if stats["win_rate"] >= 55 and stats["profit_factor"] >= 1.5:
            cprint("[bold green]  ✅ Strategy looks viable for paper testing[/]"
                   if RICH else "  ✅ Strategy looks viable for paper testing")
            cprint("  Consider lowering --threshold slightly for more trades."
                   if RICH else "  Consider lowering --threshold for more trades.")
        elif stats["win_rate"] >= 45:
            cprint("[bold yellow]  ⚠️  Borderline — run --optimize to find better threshold[/]"
                   if RICH else "  ⚠️  Borderline — run --optimize")
            cprint(f"  python3 backtest.py --optimize {' '.join(tickers)}"
                   if RICH else f"  python3 backtest.py --optimize {' '.join(tickers)}")
        else:
            cprint("[bold red]  ❌ Not profitable at this threshold[/]"
                   if RICH else "  ❌ Not profitable at this threshold")
            cprint("  Run: python3 backtest.py --optimize   to find a better setting"
                   if RICH else "  Run: python3 backtest.py --optimize")
        print("─"*55)
        print(f"\n  ⚠️  Backtest = historical simulation only.")
        print(f"  Past performance does not guarantee future results.\n")

        if args.save:
            save_results(all_trades, stats, args.threshold)

if __name__=="__main__":
    main()