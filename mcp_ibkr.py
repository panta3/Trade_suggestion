#!/usr/bin/env python3
"""
mcp_ibkr.py — MCP server for Interactive Brokers TWS / IB Gateway.

Gives Claude direct access to IBKR: historical bars for backtesting,
live quotes, account summary, and open positions.

── TWS SETUP (do this once) ──────────────────────────────────────────
1. Open TWS or IB Gateway on Windows
2. File → Global Configuration → API → Settings
   ✓  Enable ActiveX and Socket Clients
   ✓  Socket port: 7496  (live)  or  7497  (paper)
   ✓  Trusted IP Addresses → add:  172.18.176.171   (your WSL2 IP)
   ✓  Read-Only API: OFF  (if you want order placement)
3. Apply & restart TWS/Gateway

── CLAUDE CODE SETUP ─────────────────────────────────────────────────
Run once in terminal:
    claude mcp add ibkr -- python3 /home/aaravpant01/2026/Trade_suggestion/mcp_ibkr.py

── TOOLS EXPOSED ─────────────────────────────────────────────────────
  ibkr_status              check connection to TWS
  ibkr_historical_bars     OHLCV history for backtesting (5-min, daily, etc.)
  ibkr_live_quote          real-time bid/ask/last/volume
  ibkr_account_summary     cash, NLV, buying power, margin
  ibkr_positions           all open positions with P&L
  ibkr_option_chain        strikes + IV for a symbol (for auto-recommend)
"""

import asyncio, sys, json
from datetime import datetime, timezone
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.lowlevel.server import NotificationOptions

# ── IB connection config ──────────────────────────────────────────────────────
TWS_HOST = "172.18.176.1"   # Windows host IP from WSL2  (ip route | grep default)
TWS_PORT = 7496              # 7497 = paper trading,  7496 = live
CLIENT_ID = 42               # any integer not used by another client

# ── Helpers ───────────────────────────────────────────────────────────────────
def _ib():
    """Return a connected IB instance (connects fresh each call, auto-disconnects)."""
    from ib_insync import IB
    ib = IB()
    ib.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID, timeout=10, readonly=True)
    return ib


def _contract(symbol: str, sec_type: str = "STK", currency: str = "USD",
               exchange: str = "SMART"):
    from ib_insync import Stock, Contract
    if sec_type == "STK":
        return Stock(symbol, exchange, currency)
    # Generic fallback
    c = Contract()
    c.symbol   = symbol
    c.secType  = sec_type
    c.currency = currency
    c.exchange = exchange
    return c


def _fmt_bars(bars) -> list[dict]:
    out = []
    for b in bars:
        out.append({
            "date":   str(b.date),
            "open":   round(float(b.open),  4),
            "high":   round(float(b.high),  4),
            "low":    round(float(b.low),   4),
            "close":  round(float(b.close), 4),
            "volume": int(b.volume),
        })
    return out


# ── MCP server ────────────────────────────────────────────────────────────────
server = Server("ibkr")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="ibkr_status",
            description="Check whether TWS/IB Gateway is reachable and return account info.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="ibkr_historical_bars",
            description=(
                "Fetch OHLCV historical bars from IBKR for backtesting. "
                "bar_size options: '1 min', '5 mins', '15 mins', '1 hour', '1 day'. "
                "duration examples: '5 D', '1 M', '6 M', '1 Y', '2 Y'. "
                "Returns list of {date, open, high, low, close, volume}."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol":    {"type": "string",  "description": "Ticker e.g. AAPL, SPY"},
                    "bar_size":  {"type": "string",  "description": "Bar size string e.g. '5 mins'"},
                    "duration":  {"type": "string",  "description": "Lookback e.g. '1 Y'"},
                    "end_date":  {"type": "string",  "description": "End date YYYYMMDD HH:MM:SS (optional, defaults to now)"},
                    "what_to_show": {"type": "string", "description": "TRADES | MIDPOINT | BID | ASK (default TRADES)"},
                },
                "required": ["symbol", "bar_size", "duration"],
            },
        ),
        types.Tool(
            name="ibkr_live_quote",
            description="Get real-time bid/ask/last/volume/IV for a stock or ETF.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Ticker e.g. TSLA"},
                },
                "required": ["symbol"],
            },
        ),
        types.Tool(
            name="ibkr_account_summary",
            description="Return account cash, net liquidation value, buying power, and margin.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="ibkr_positions",
            description="Return all open positions with average cost and unrealized P&L.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="ibkr_option_chain",
            description=(
                "Fetch the options chain for a symbol (strikes, expiries, bid/ask, IV, delta). "
                "Use this to find the best contract for a trade signal."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol":     {"type": "string",  "description": "Underlying ticker e.g. AAPL"},
                    "expiry":     {"type": "string",  "description": "Expiry YYYYMMDD e.g. 20260523"},
                    "right":      {"type": "string",  "description": "C (call) or P (put)"},
                    "min_strike": {"type": "number",  "description": "Filter strikes above this (optional)"},
                    "max_strike": {"type": "number",  "description": "Filter strikes below this (optional)"},
                },
                "required": ["symbol", "expiry", "right"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, _dispatch, name, arguments
        )
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]


def _dispatch(name: str, args: dict) -> Any:
    if name == "ibkr_status":           return _status()
    if name == "ibkr_historical_bars":  return _historical_bars(**args)
    if name == "ibkr_live_quote":       return _live_quote(**args)
    if name == "ibkr_account_summary":  return _account_summary()
    if name == "ibkr_positions":        return _positions()
    if name == "ibkr_option_chain":     return _option_chain(**args)
    raise ValueError(f"Unknown tool: {name}")


# ── Tool implementations ──────────────────────────────────────────────────────

def _status() -> dict:
    ib = _ib()
    try:
        accounts = ib.managedAccounts()
        return {
            "connected": True,
            "host":      TWS_HOST,
            "port":      TWS_PORT,
            "accounts":  accounts,
            "server_version": ib.client.serverVersion(),
        }
    finally:
        ib.disconnect()


def _historical_bars(symbol: str, bar_size: str, duration: str,
                     end_date: str = "", what_to_show: str = "TRADES") -> dict:
    from ib_insync import util
    ib = _ib()
    try:
        contract = _contract(symbol)
        ib.qualifyContracts(contract)
        bars = ib.reqHistoricalData(
            contract,
            endDateTime   = end_date or "",
            durationStr   = duration,
            barSizeSetting= bar_size,
            whatToShow    = what_to_show,
            useRTH        = True,
            formatDate    = 1,
        )
        data = _fmt_bars(bars)
        return {
            "symbol":   symbol,
            "bar_size": bar_size,
            "duration": duration,
            "count":    len(data),
            "bars":     data,
        }
    finally:
        ib.disconnect()


def _live_quote(symbol: str) -> dict:
    ib = _ib()
    try:
        contract = _contract(symbol)
        ib.qualifyContracts(contract)
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(2)   # wait for snapshot
        return {
            "symbol": symbol,
            "bid":    ticker.bid,
            "ask":    ticker.ask,
            "last":   ticker.last,
            "close":  ticker.close,
            "volume": ticker.volume,
            "iv":     ticker.impliedVolatility,
            "time":   datetime.now(tz=timezone.utc).isoformat(),
        }
    finally:
        ib.disconnect()


def _account_summary() -> dict:
    ib = _ib()
    try:
        summary = ib.accountSummary()
        want = {"TotalCashValue", "NetLiquidation", "BuyingPower",
                "MaintMarginReq", "AvailableFunds", "UnrealizedPnL", "RealizedPnL"}
        return {
            row.tag: {"value": row.value, "currency": row.currency}
            for row in summary if row.tag in want
        }
    finally:
        ib.disconnect()


def _positions() -> dict:
    ib = _ib()
    try:
        positions = ib.positions()
        out = []
        for p in positions:
            out.append({
                "account":      p.account,
                "symbol":       p.contract.symbol,
                "sec_type":     p.contract.secType,
                "position":     p.position,
                "avg_cost":     round(p.avgCost, 4),
                "market_value": round(p.position * p.avgCost, 2),
            })
        return {"positions": out, "count": len(out)}
    finally:
        ib.disconnect()


def _option_chain(symbol: str, expiry: str, right: str,
                  min_strike: float = None, max_strike: float = None) -> dict:
    from ib_insync import Option
    ib = _ib()
    try:
        # Get the underlying price first
        stock = _contract(symbol)
        ib.qualifyContracts(stock)
        ticker = ib.reqMktData(stock, "", False, False)
        ib.sleep(1)
        spot = ticker.last or ticker.close or 0

        # Get available strikes for the expiry
        chains = ib.reqSecDefOptParams(symbol, "", "STK", stock.conId)
        chain  = next((c for c in chains if c.exchange == "SMART"), None)
        if not chain:
            return {"error": f"No option chain found for {symbol}"}

        if expiry not in chain.expirations:
            nearest = min(chain.expirations, key=lambda e: abs(int(e) - int(expiry)))
            expiry  = nearest

        strikes = sorted(chain.strikes)
        if min_strike: strikes = [s for s in strikes if s >= min_strike]
        if max_strike: strikes = [s for s in strikes if s <= max_strike]
        # Narrow to 20 strikes around spot
        if spot:
            strikes = sorted(strikes, key=lambda s: abs(s - spot))[:20]
            strikes = sorted(strikes)

        contracts = [
            Option(symbol, expiry, strike, right.upper(), "SMART")
            for strike in strikes
        ]
        ib.qualifyContracts(*contracts)
        tickers = ib.reqTickers(*contracts)
        ib.sleep(2)

        rows = []
        for t in tickers:
            c = t.contract
            rows.append({
                "strike":    c.strike,
                "expiry":    c.lastTradeDateOrContractMonth,
                "right":     c.right,
                "bid":       t.bid,
                "ask":       t.ask,
                "mid":       round((t.bid + t.ask) / 2, 3) if t.bid and t.ask else None,
                "last":      t.last,
                "volume":    t.volume,
                "open_int":  t.callOpenInterest if right.upper() == "C" else t.putOpenInterest,
                "iv":        round(t.impliedVolatility * 100, 1) if t.impliedVolatility else None,
                "delta":     round(t.modelGreeks.delta, 3) if t.modelGreeks else None,
                "gamma":     round(t.modelGreeks.gamma, 4) if t.modelGreeks else None,
                "theta":     round(t.modelGreeks.theta, 4) if t.modelGreeks else None,
            })
        rows.sort(key=lambda r: r["strike"])
        return {
            "symbol":  symbol,
            "expiry":  expiry,
            "right":   right.upper(),
            "spot":    spot,
            "strikes": rows,
            "count":   len(rows),
        }
    finally:
        ib.disconnect()


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name    = "ibkr",
                server_version = "1.0.0",
                capabilities   = server.get_capabilities(
                    notification_options = NotificationOptions(),
                    experimental_capabilities = {},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
