#!/bin/bash
# weekly_review.sh — runs every Friday at 4:30 PM automatically
# Prints intraday accuracy + threshold optimizer + swing trade summary

cd "$(dirname "$(dirname "$(realpath "$0")")")"

echo "========================================"
echo " WEEKLY REVIEW — $(date '+%Y-%m-%d %H:%M')"
echo "========================================"

echo ""
echo "── INTRADAY ACCURACY ────────────────────"
python3 scripts/intraday_validate.py

echo ""
echo "── THRESHOLD OPTIMIZER ──────────────────"
python3 scripts/optimize_threshold.py

echo ""
echo "── SWING TRADE SUMMARY ──────────────────"
python3 scripts/daily_validate.py

echo ""
echo "========================================"
echo " REVIEW COMPLETE"
echo "========================================"
