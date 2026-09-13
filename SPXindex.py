"""
SPXindex.py — S&P 500 / NASDAQ Universe ($5–$10000)
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
    "megacap":    ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AVGO"],
    "semis":      ["AMD","QCOM","INTC","MU","AMAT","KLAC","LRCX","MRVL","TXN","ON","NXPI","ADI","MPWR"],
    "cloud_saas": ["NOW","SNOW","DDOG","WDAY","ZS","NET","CRWD","PANW","FTNT","ADBE","INTU","CDNS","SNPS","VEEV","HUBS","TEAM"],
    "consumer":   ["COST","PEP","KO","WMT","TGT","SBUX","MCD","NKE","LOW","HD","NFLX","BKNG","ABNB","UBER","DASH"],
    "healthcare": ["ISRG","MRNA","LLY","ABBV","JNJ","UNH","AMGN","GILD","REGN","MDT","DXCM","ABT"],
    "financials": ["JPM","GS","MS","BAC","WFC","V","MA","PYPL","AXP","COF","COIN"],
    "energy":     ["XOM","CVX","COP","SLB","OXY","EOG","HAL","MPC","PSX","VLO"],
    "industrials":["CAT","DE","HON","GE","RTX","LMT","NOC","BA","UPS","FDX","CSX","NSC"],
}

NEWS_FEEDS = [
    "https://news.google.com/rss/search?q=S%26P+500+stocks+today&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=nasdaq+stocks+today&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=stock+earnings+today&hl=en&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=tech+stocks+market&hl=en&gl=US&ceid=US:en",
    "https://feeds.reuters.com/reuters/businessNews",
]

US_SEEDS = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AVGO",
    "AMD","QCOM","INTC","MU","AMAT","KLAC","LRCX","MRVL","TXN","ON","NXPI","ADI","MPWR",
    "NOW","SNOW","DDOG","WDAY","ZS","NET","CRWD","PANW","FTNT","ADBE","INTU","CDNS","SNPS","VEEV","HUBS","TEAM",
    "NFLX","BKNG","ABNB","UBER","DASH","COIN","RBLX","PINS","SNAP","RDDT",
    "COST","PEP","KO","WMT","TGT","SBUX","MCD","NKE","LOW","HD","DG","DLTR",
    "ISRG","MRNA","LLY","ABBV","JNJ","UNH","AMGN","GILD","REGN","MDT","DXCM","ABT",
    "JPM","GS","MS","BAC","WFC","V","MA","PYPL","AXP","COF",
    "XOM","CVX","COP","SLB","OXY","EOG","HAL","MPC","PSX","VLO",
    "CAT","DE","HON","GE","RTX","LMT","NOC","BA","UPS","FDX","CSX","NSC",
    "FCX","NEM","NUE","ALB","MP",
    "DIS","CMCSA","T","VZ",
]

CA_SEEDS = []  # SPX focuses on S&P 500 and NASDAQ only

STRATEGY = {
    "buy_score": 6.5, "watch_score": 4.5,
    "max_open_positions": 4, "cash_buffer_pct": 0.10,
    "max_position_pct": 0.28, "usd_cad_fallback": 1.37,
}

# ── Engine ────────────────────────────────────────────────────────────────────
_engine = AlgoEngine(
    min_price=5.0, max_price=10000.0,
    sectors=SECTORS,
    us_seeds=US_SEEDS, ca_seeds=CA_SEEDS,
    strategy=STRATEGY,
    news_feeds=NEWS_FEEDS,
    portfolio_file=os.path.join(_DATA, "portfolio.json"),
    accuracy_file=os.path.join(_DATA, "accuracy.json"),
    paper_trades_file=os.path.join(_DATA, "paper_trades.json"),
    scan_title="S&P 500 / NASDAQ $5–$10000",
    banner_rich=(
        "[bold cyan]📈 S&P 500 / NASDAQ SIGNAL ASSISTANT[/]\n"
        "[dim]Universe: S&P 500 · NASDAQ 100 · Full sector coverage[/]"
    ),
    banner_plain=[
        "╔══════════════════════════════════════════════════════╗",
        "║  📈 S&P 500 / NASDAQ SIGNAL ASSISTANT                ║",
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
