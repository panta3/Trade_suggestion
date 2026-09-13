#!/usr/bin/env bash
# backtest_sweep.sh — runs the 2yr backtest across hold times and thresholds
# Usage:
#   ./scripts/backtest_sweep.sh                        # default symbols, CALL only
#   ./scripts/backtest_sweep.sh --puts                 # include PUTs
#   ./scripts/backtest_sweep.sh --symbols AAPL NVDA    # specific symbols

set -euo pipefail
cd "$(dirname "$0")/.."

PUTS_FLAG=""
SYMBOLS_FLAG=""

# parse args
while [[ $# -gt 0 ]]; do
  case $1 in
    --puts) PUTS_FLAG="--enable-puts"; shift ;;
    --symbols) shift; syms="$*"; SYMBOLS_FLAG="--symbols $syms"; break ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# Hold values: 1bar=15m, 2=30m, 4=1h, 8=2h, 16=4h, 26=rest-of-day
HOLDS=(1 2 4 8 16 26)
HOLD_LABELS=("15m" "30m" "1h" "2h" "4h" "EOD")

# Thresholds to sweep
THRESHOLDS=(85 90 95)

OUT_DIR="scripts/sweep_results"
mkdir -p "$OUT_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
SUMMARY="$OUT_DIR/sweep_${TIMESTAMP}.txt"

echo "================================================================" | tee "$SUMMARY"
echo " Backtest Hold × Threshold Sweep — $(date)" | tee -a "$SUMMARY"
echo " Puts: ${PUTS_FLAG:-none}  Symbols: ${SYMBOLS_FLAG:-DEFAULT}" | tee -a "$SUMMARY"
echo "================================================================" | tee -a "$SUMMARY"
printf "\n%-10s %-8s  %6s  %6s  %6s  %6s\n" "Threshold" "Hold" "n" "WR%" "PF" "Sharpe" | tee -a "$SUMMARY"
echo "------------------------------------------------------------" | tee -a "$SUMMARY"

for thresh in "${THRESHOLDS[@]}"; do
  for i in "${!HOLDS[@]}"; do
    hold="${HOLDS[$i]}"
    label="${HOLD_LABELS[$i]}"

    result=$(python3 scripts/intraday_backtest_2yr.py \
      --threshold "$thresh" \
      --hold "$hold" \
      --json \
      $PUTS_FLAG \
      $SYMBOLS_FLAG 2>/dev/null) || true

    if echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if not d.get('no_trades') else 1)" 2>/dev/null; then
      n=$(echo "$result"      | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('oos',{}).get('n',0))")
      wr=$(echo "$result"     | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d.get('oos',{}).get('win_rate',0):.1f}\")")
      pf=$(echo "$result"     | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d.get('oos',{}).get('profit_factor',0):.2f}\")")
      sharpe=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d.get('oos',{}).get('sharpe',0):.2f}\")")
    else
      n=0; wr="—"; pf="—"; sharpe="—"
    fi

    # colour-code in terminal: green if PF>1, red if PF<1
    if [[ "$pf" != "—" ]] && python3 -c "exit(0 if float('$pf') >= 1.0 else 1)" 2>/dev/null; then
      colour="\033[32m"
    else
      colour="\033[31m"
    fi

    printf "${colour}%-10s %-8s  %6s  %6s  %6s  %6s\033[0m\n" \
      "t=$thresh" "$label" "$n" "$wr" "$pf" "$sharpe" | tee -a "$SUMMARY"

    # also save raw JSON for later
    echo "$result" > "$OUT_DIR/t${thresh}_h${hold}_${TIMESTAMP}.json" 2>/dev/null || true
  done
  echo "" | tee -a "$SUMMARY"
done

echo "================================================================" | tee -a "$SUMMARY"
echo " Results saved to: $OUT_DIR/" | tee -a "$SUMMARY"
echo "================================================================" | tee -a "$SUMMARY"
