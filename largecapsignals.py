"""
largecapsignals.py — Large Cap Universe ($10–$9999)
Config-only wrapper. All logic lives in core_signals.AlgoEngine.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core_signals import (
    AlgoEngine,
    fetch_stock, fetch_prices, fetch_weekly_trend,
    fetch_earnings_date, market_regime,
    portfolio_summary, cprint,
)

# ── Data paths ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_HERE, "data")

# ── Universe config ───────────────────────────────────────────────────────────
SECTORS = {
    "semis":      ["NVDA","AMD","AVGO","QCOM","INTC","MU","AMAT","KLAC","LRCX","MRVL","TXN"],
    "megacap":    ["AAPL","MSFT","AMZN","META","GOOGL","TSLA","NVDA"],
    "cloud_saas": ["SNOW","DDOG","NOW","WDAY","ZS","NET","CRWD","PANW","FTNT","ADBE"],
    "tsx_banks":  ["RY.TO","TD.TO","BNS.TO","BMO.TO","CM.TO","NA.TO","MFC.TO","SLF.TO"],
    "tsx_energy": ["CNQ.TO","ENB.TO","SU.TO","CVE.TO","TRP.TO","PPL.TO","ARX.TO"],
    "tsx_gold":   ["ABX.TO","AEM.TO","FNV.TO","WPM.TO","K.TO"],
    "tsx_telecom":["T.TO","BCE.TO","RCI-B.TO"],
    "consumer":   ["COST","PEP","SBUX","NFLX","BKNG","ISRG"],
}

NEWS_FEEDS = [
    "https://news.google.com/rss/search?q=TSX+large+cap+stocks+today&hl=en-CA&gl=CA&ceid=CA:en",
    "https://news.google.com/rss/search?q=NASDAQ+large+cap+stocks+today&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=S%26P+TSX+composite+index&hl=en-CA&gl=CA&ceid=CA:en",
    "https://news.google.com/rss/search?q=NASDAQ+100+earnings+today&hl=en&gl=US&ceid=US:en",
    "https://feeds.reuters.com/reuters/businessNews",
]

US_SEEDS = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA",
    "AVGO","AMD","QCOM","INTC","MU","AMAT","KLAC","LRCX","MRVL","TXN",
    "SNOW","DDOG","NOW","WDAY","ZS","NET","CRWD","PANW","FTNT","ADBE",
    "COST","PEP","SBUX","NFLX","BKNG","ISRG","MRNA",
]

CA_SEEDS = [
    "RY.TO","TD.TO","BNS.TO","BMO.TO","CM.TO","NA.TO","MFC.TO","SLF.TO",
    "CNQ.TO","ENB.TO","SU.TO","CVE.TO","TRP.TO","PPL.TO","ARX.TO",
    "ABX.TO","AEM.TO","FNV.TO","WPM.TO","K.TO","NTR.TO",
    "SHOP.TO","CSU.TO","BAM.TO","BN.TO","CP.TO","CNR.TO","WCN.TO",
    "T.TO","BCE.TO","RCI-B.TO","ATD.TO","DOL.TO","L.TO",
]

STRATEGY = {
    "buy_score": 6.5, "watch_score": 4.5,
    "max_open_positions": 4, "cash_buffer_pct": 0.10,
    "max_position_pct": 0.28, "usd_cad_fallback": 1.37,
}

# ── Engine ────────────────────────────────────────────────────────────────────
_engine = AlgoEngine(
    min_price=10.0, max_price=9999.0,
    sectors=SECTORS,
    us_seeds=US_SEEDS, ca_seeds=CA_SEEDS,
    strategy=STRATEGY,
    news_feeds=NEWS_FEEDS,
    portfolio_file=os.path.join(_DATA, "portfolio.json"),
    accuracy_file=os.path.join(_DATA, "accuracy.json"),
    paper_trades_file=os.path.join(_DATA, "paper_trades.json"),
    scan_title="Large Cap $10–$9999",
    banner_rich=(
        "[bold cyan]📈 LARGE CAP TRADING SIGNAL ASSISTANT[/]\n"
        "[dim]Universe: TSX · S&P/TSX Composite · NASDAQ[/]"
    ),
    banner_plain=[
        "╔══════════════════════════════════════════════════════╗",
        "║  📈 LARGE CAP TRADING SIGNAL ASSISTANT               ║",
        "║     TSX · S&P/TSX Composite · NASDAQ                 ║",
        "╚══════════════════════════════════════════════════════╝",
    ],
)

# ── Module-level shims ────────────────────────────────────────────────────────
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
