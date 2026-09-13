#!/usr/bin/env bash
# index_sweep.sh — run backtest across DJ30, NASDAQ-100, S&P 500
# at multiple hold times (15m / 1h / 2h / full-day), then rank by best OOS Sharpe
#
# Usage:
#   ./scripts/index_sweep.sh                    # default: all indices, all holds, threshold 90
#   ./scripts/index_sweep.sh --precache-only    # just warm the bar cache, no backtest
#   ./scripts/index_sweep.sh --threshold 92 --top 25
#   ./scripts/index_sweep.sh --puts             # include PUT signals

set -euo pipefail
cd "$(dirname "$0")/.."

# ── defaults ──────────────────────────────────────────────────────────────────
THRESHOLD=90
TOP=30
PRECACHE_ONLY=0
ENABLE_PUTS=""

# Hold configs: bars × label  (15-min bars)
#   1  = 15m
#   4  = 1h
#   8  = 2h
#   26 = ~6.5h (rest-of-day)
HOLD_BARS=(1 4 8 26)
HOLD_LABELS=("15m" "1h" "2h" "EOD")

while [[ $# -gt 0 ]]; do
  case $1 in
    --precache-only) PRECACHE_ONLY=1; shift ;;
    --threshold)     THRESHOLD="$2"; shift 2 ;;
    --top)           TOP="$2"; shift 2 ;;
    --puts)          ENABLE_PUTS="--enable-puts"; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

OUT_DIR="data"
mkdir -p "$OUT_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG="$OUT_DIR/index_sweep_${TIMESTAMP}.log"

echo "================================================================" | tee "$LOG"
echo " Index Sweep (DJ30 + NASDAQ-100 + S&P 500) — $(date)" | tee -a "$LOG"
echo " threshold=$THRESHOLD  holds=${HOLD_LABELS[*]}  top=$TOP  puts=${ENABLE_PUTS:-no}" | tee -a "$LOG"
echo "================================================================" | tee -a "$LOG"

# ── 1. fetch symbol lists ─────────────────────────────────────────────────────
echo "" | tee -a "$LOG"
echo "▶ Fetching index constituents..." | tee -a "$LOG"

DJ30=$(python3 - <<'PYEOF'
import sys; sys.path.insert(0, ".")
from ibkr_data import get_index_tickers
print(" ".join(get_index_tickers("dj30")))
PYEOF
)

NQ100=$(python3 - <<'PYEOF'
import sys; sys.path.insert(0, ".")
from ibkr_data import get_index_tickers
print(" ".join(get_index_tickers("nasdaq100")))
PYEOF
)

SP500=$(python3 - <<'PYEOF'
import sys; sys.path.insert(0, ".")
from ibkr_data import get_index_tickers
print(" ".join(get_index_tickers("sp500")))
PYEOF
)

ALL_SYMS=$(echo "$DJ30 $NQ100 $SP500" | tr ' ' '\n' | sort -u | tr '\n' ' ')
N_ALL=$(echo "$ALL_SYMS" | wc -w)

echo "  DJ30:       $(echo $DJ30  | wc -w) symbols" | tee -a "$LOG"
echo "  NASDAQ-100: $(echo $NQ100 | wc -w) symbols" | tee -a "$LOG"
echo "  S&P 500:    $(echo $SP500 | wc -w) symbols" | tee -a "$LOG"
echo "  Union:      $N_ALL unique symbols" | tee -a "$LOG"

# ── 2. pre-cache 2yr IBKR bars ───────────────────────────────────────────────
echo "" | tee -a "$LOG"
echo "▶ Pre-caching 2yr IBKR bars for $N_ALL symbols (skips already-cached)..." | tee -a "$LOG"

IBKR_CLIENT_ID=45 python3 scripts/intraday_backtest_2yr.py \
  --symbols $ALL_SYMS \
  --precache-only 2>&1 | tee -a "$LOG"

if [[ $PRECACHE_ONLY -eq 1 ]]; then
  echo "" | tee -a "$LOG"
  echo "✓ Pre-cache done. Re-run without --precache-only to run backtests." | tee -a "$LOG"
  exit 0
fi

# ── 3. run backtest: each index × each hold ───────────────────────────────────
declare -A INDEX_SYMS
INDEX_SYMS[dj30]="$DJ30"
INDEX_SYMS[nasdaq100]="$NQ100"
INDEX_SYMS[sp500]="$SP500"

echo "" | tee -a "$LOG"
echo "▶ Running backtests (3 indices × ${#HOLD_BARS[@]} hold times = $(( 3 * ${#HOLD_BARS[@]} )) runs)..." | tee -a "$LOG"

# header row
printf "\n%-12s %-6s  %6s  %6s  %6s  %8s\n" "Index" "Hold" "n" "WR%" "PF" "Sharpe" | tee -a "$LOG"
echo "----------------------------------------------" | tee -a "$LOG"

for idx in dj30 nasdaq100 sp500; do
  syms="${INDEX_SYMS[$idx]}"
  for i in "${!HOLD_BARS[@]}"; do
    hold="${HOLD_BARS[$i]}"
    label="${HOLD_LABELS[$i]}"

    OUT_FILE="$OUT_DIR/bt_${idx}_h${hold}_${TIMESTAMP}.json"

    IBKR_CLIENT_ID=45 python3 scripts/intraday_backtest_2yr.py \
      --symbols $syms \
      --threshold "$THRESHOLD" \
      --hold "$hold" \
      --json \
      $ENABLE_PUTS > "$OUT_FILE" 2>>"$LOG" || true

    python3 - "$OUT_FILE" "$idx" "$label" <<'PYEOF'
import json, sys

fpath, idx, label = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(fpath) as fh:
        raw = fh.read().strip()
    if not raw:
        print(f"{idx:<12} {label:<6}  (no output — check log)")
    else:
        d = json.loads(raw)
        if d.get("no_trades"):
            print(f"{idx:<12} {label:<6}  (no trades at this threshold)")
        else:
            o = d.get("oos", {})
            pf = o.get("pf", o.get("profit_factor", 0))
            colour = "\033[32m" if pf >= 1.0 else "\033[31m"
            print(f"{colour}{idx:<12} {label:<6}  {o.get('n',0):>6}  {o.get('win_rate',0):>5.1f}%  {pf:>6.2f}  {o.get('sharpe',0):>8.2f}\033[0m")
except Exception as e:
    print(f"{idx:<12} {label:<6}  (err: {e})")
PYEOF
  done | tee -a "$LOG"
  echo "" | tee -a "$LOG"
done

# ── 4. rank symbols — best hold wins per symbol ───────────────────────────────
echo "▶ Ranking all symbols by best OOS Sharpe across all holds (min 5 trades)..." | tee -a "$LOG"

python3 - "${TIMESTAMP}" "${TOP}" <<'PYEOF' | tee -a "$LOG"
import json, glob, os, sys

ts, top_n = sys.argv[1], int(sys.argv[2])
files = glob.glob(f"data/bt_*_{ts}.json")
all_syms = {}   # sym -> best row seen

for f in files:
    parts = os.path.basename(f).split("_")   # bt_idx_hN_ts.json
    idx   = parts[1]
    hold  = parts[2]   # e.g. h8
    hold_min = int(hold[1:]) * 15
    hold_label = f"{hold_min//60}h{hold_min%60:02d}m" if hold_min >= 60 else f"{hold_min}m"

    try:
        with open(f) as fh:
            raw = fh.read().strip()
        if not raw:
            continue
        d = json.loads(raw)
    except Exception:
        continue

    for row in d.get("by_symbol", []):
        sym = row["symbol"]
        if row.get("n", 0) < 5:
            continue
        entry = {**row, "index": idx, "best_hold": hold_label}
        if sym not in all_syms or row["sharpe"] > all_syms[sym]["sharpe"]:
            all_syms[sym] = entry

ranked = sorted(all_syms.values(), key=lambda x: -x["sharpe"])
top    = ranked[:top_n]

print(f"\n{'Rank':<5} {'Symbol':<8} {'Index':<12} {'Hold':<6} {'n':>5} {'WR%':>6} {'PF':>6} {'Sharpe':>8} {'MaxDD':>8}")
print("-" * 70)
for i, r in enumerate(top, 1):
    star = " ★" if r["sharpe"] >= 2.0 else ""
    print(f"{i:<5} {r['symbol']:<8} {r['index']:<12} {r['best_hold']:<6} "
          f"{r['n']:>5} {r['wr']:>5.1f}% {r['pf']:>6.2f} {r['sharpe']:>8.2f} {r['max_dd']:>7.2f}%{star}")

syms_only = [r["symbol"] for r in top]
print(f"\n✓ Top {len(syms_only)} cream-of-the-crop symbols:")
print("  " + "  ".join(syms_only))

out = {
    "generated_at":   ts,
    "threshold":      90,
    "holds_tested":   ["15m","1h","2h","EOD"],
    "source_indices": ["dj30","nasdaq100","sp500"],
    "ranked":         ranked,
    f"top_{top_n}":   syms_only,
}
with open("data/cream_universe.json", "w") as fh:
    json.dump(out, fh, indent=2)
print(f"\n  Full ranking saved → data/cream_universe.json")
PYEOF

echo "" | tee -a "$LOG"
echo "================================================================" | tee -a "$LOG"
echo " Done. Log: $LOG" | tee -a "$LOG"
echo "================================================================" | tee -a "$LOG"
