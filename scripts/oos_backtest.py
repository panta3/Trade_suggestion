"""
oos_backtest.py — out-of-sample backtest on tickers never used to calibrate.
Run: python3 scripts/oos_backtest.py

Compares win rate / avg return vs the in-sample DEFAULT_TICKERS result.
A gap of >8 percentage points signals the threshold is overfit.
"""
import sys, os, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PYTHON = sys.executable
BACKTEST = os.path.join(os.path.dirname(os.path.dirname(__file__)), "Backtest.py")

OOS_UNIVERSES = {
    "oos_us_largecap": [
        "CRM","ORCL","PANW","FTNT","UBER","ABNB","COIN","SQ",
        "PLTR","MELI","BABA","V","MA","JPM","GS","BAC","WFC","UNH","LLY","PFE",
    ],
    "oos_tsx": [
        "BMO.TO","BNS.TO","MFC.TO","SLF.TO","POW.TO",
        "WPM.TO","AEM.TO","FM.TO","CCO.TO","TRP.TO","PPL.TO","KEY.TO",
    ],
    "oos_smallcap_us": [
        "MARA","RIOT","CLSK","HUT","SOUN","NVAX","PLUG","IONQ",
        "ACHR","JOBY","RKLB","LUNR","ASTS","ARQT","RXRX",
    ],
}

def run(label, tickers):
    print(f"\n{'='*55}")
    print(f"  OOS: {label}  ({len(tickers)} tickers)")
    print(f"{'='*55}")
    subprocess.run(
        [PYTHON, BACKTEST, "--walk-forward"] + tickers,
        cwd=os.path.dirname(BACKTEST)
    )

if __name__ == "__main__":
    print("Out-of-sample backtest — tickers never used to calibrate threshold")
    print("Compare these results to your in-sample run to check for overfitting\n")
    for label, tickers in OOS_UNIVERSES.items():
        run(label, tickers)
