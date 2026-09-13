"""
signals.py — Small Cap Universe ($2–$40)
Config-only wrapper. All logic lives in core_signals.AlgoEngine.
"""
import os, sys, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core_signals import (
    AlgoEngine,
    fetch_stock, fetch_prices, fetch_weekly_trend,
    fetch_earnings_date, market_regime,
    portfolio_summary, cprint,
)

# ── Data paths ────────────────────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.abspath(__file__))
_DATA     = os.path.join(_HERE, "data")

# ── Universe config ───────────────────────────────────────────────────────────
SECTORS = {
    "crypto":  ["MARA","RIOT","CLSK","CIFR","HUT","BITF","BTBT"],
    "ev":      ["RIVN","LCID","NKLA","FFIE","WKHS","GOEV"],
    "cannabis":["ACB.TO","TLRY.TO","CRON.TO","APHA","CGC","SNDL"],
    "energy":  ["BTE.TO","TVE.TO","GTE.TO","CPG.TO","ERF.TO","VET.TO","ARX.TO"],
    "ai_tech": ["SOUN","BBAI","IONQ","PLTR","AI","UPST"],
    "fintech": ["SOFI","HOOD","OPEN","AFRM","UPST","UWMC"],
    "social":  ["SNAP","GRAB","PINS","BMBL"],
}

NEWS_FEEDS = [
    "https://news.google.com/rss/search?q=stock+market+today&hl=en-CA&gl=CA&ceid=CA:en",
    "https://news.google.com/rss/search?q=TSX+stocks+today&hl=en-CA&gl=CA&ceid=CA:en",
    "https://news.google.com/rss/search?q=penny+stocks+momentum&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=stock+earnings+today&hl=en&gl=US&ceid=US:en",
    "https://feeds.reuters.com/reuters/businessNews",
]

US_SEEDS = [
    "SOFI","RIVN","LCID","BB","SOUN","BBAI","MARA","RIOT","CLSK",
    "OPEN","CLOV","GPRO","SNAP","GRAB","SENS","WKHS","IONQ","CIFR",
    "HUT","BITF","NKLA","FFIE","DKNG","HOOD","UWMC","RKT",
    "XPEV","NIO","BTBT","AI","UPST","AFRM","PINS","BMBL",
]

CA_SEEDS = [
    "BB.TO","ACB.TO","TLRY.TO","CRON.TO","BTE.TO","TVE.TO","GTE.TO",
    "ARX.TO","BIR.TO","VET.TO","ERF.TO","CPG.TO","WCP.TO","SDE.TO",
    "CR.TO","AAV.TO","LSPD.TO","DCBO.TO","ALYA.TO","VERY.TO","HIVE.TO",
    "NXE.TO","MAU.TO","QQC-F.TO","VFV.TO",
]

STRATEGY = {
    "buy_score": 5.0, "watch_score": 3.5,
    "max_open_positions": 4, "cash_buffer_pct": 0.10,
    "max_position_pct": 0.28, "usd_cad_fallback": 1.37,
}

# ── Engine ────────────────────────────────────────────────────────────────────
_engine = AlgoEngine(
    min_price=2.0, max_price=40.0,
    sectors=SECTORS,
    us_seeds=US_SEEDS, ca_seeds=CA_SEEDS,
    strategy=STRATEGY,
    news_feeds=NEWS_FEEDS,
    portfolio_file=os.path.join(_DATA, "portfolio.json"),
    accuracy_file=os.path.join(_DATA, "accuracy.json"),
    paper_trades_file=os.path.join(_DATA, "paper_trades.json"),
    scan_title="Small Cap $2–$40",
    banner_rich=(
        "[bold cyan]📈 TRADING SIGNAL ASSISTANT — Small Cap[/]\n"
        "[dim]Universe: $2–$40  ·  TSX Venture + US momentum[/]"
    ),
    banner_plain=[
        "╔══════════════════════════════════════════════════════╗",
        "║  📈 TRADING SIGNAL ASSISTANT — Small Cap ($2–$40)    ║",
        "╚══════════════════════════════════════════════════════╝",
    ],
)

# ── Module-level shims (api.py / daily_scan.py import these directly) ─────────
scan_all              = _engine.scan_all
evaluate_stock        = _engine.evaluate_stock
load_portfolio        = _engine.load_portfolio
save_portfolio        = _engine.save_portfolio
fetch_usdcad_rate     = _engine.fetch_usdcad_rate
build_sector_rank_map = _engine.build_sector_rank_map
fetch_news            = _engine.fetch_news
run_analysis          = _engine.run_analysis
log_paper_trade       = _engine.log_paper_trade
monitor_stop_losses   = _engine.monitor_stop_losses
chat_loop             = _engine.chat_loop

# ── CLI entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    _engine.main()
