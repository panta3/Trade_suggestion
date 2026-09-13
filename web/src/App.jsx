import { useState } from "react";
import Dashboard from "./Dashboard";
import GapFade from "./GapFade";
import TodaySignals from "./TodaySignals";
import SwingWatchlist from "./SwingWatchlist";
import ChartView from "./ChartView";
import UniverseBacktest from "./UniverseBacktest";
import PremarketScan from "./PremarketScan";

const TABS = [
  { id: "premarket", label: "🌅 Pre-market" },
  { id: "scanner",   label: "📈 Day Scanner" },
  { id: "today",     label: "🕐 Today's Signals" },
  { id: "swing",     label: "🌙 Swing Watchlist" },
  { id: "gapfade",   label: "⚡ Gap-Fade Calc" },
  { id: "chart",     label: "🕯️ Chart" },
  { id: "backtest",  label: "🔬 Backtest" },
];

const nav = {
  display: "flex",
  background: "var(--surface)",
  borderBottom: "1px solid var(--border)",
  flexShrink: 0,
};

const tabBtn = (active) => ({
  padding: "10px 20px",
  background: "none",
  border: "none",
  borderBottom: active ? "2px solid var(--accent)" : "2px solid transparent",
  color: active ? "var(--text)" : "var(--muted)",
  cursor: "pointer",
  fontSize: 13,
  fontWeight: active ? 600 : 400,
  transition: "color 0.15s, border-color 0.15s",
  fontFamily: "inherit",
});

export default function App() {
  const [tab, setTab] = useState("scanner");

  // Symbol jump: when user clicks "⚡ Options" in Today's Signals,
  // switch to GapFade tab and pre-load that symbol + direction.
  const [gapFadeSymbol,    setGapFadeSymbol]    = useState(null);
  const [gapFadeDirection, setGapFadeDirection] = useState(null);
  const [gapFadeTrade,     setGapFadeTrade]     = useState(null);

  const handleViewInGapFade = (symbol, direction, trade = null) => {
    setGapFadeSymbol(symbol);
    setGapFadeDirection(direction);
    setGapFadeTrade(trade);
    setTab("gapfade");
  };

  // Symbol jump: when user clicks "📈 Chart" in Today's Signals
  const [chartSymbol, setChartSymbol] = useState(null);
  const [chartSig,    setChartSig]    = useState(null);

  const handleViewInChart = (symbol, trade = null) => {
    setChartSymbol(symbol);
    setChartSig(trade);
    setTab("chart");
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh" }}>
      <nav style={nav}>
        {TABS.map((t) => (
          <button key={t.id} style={tabBtn(tab === t.id)} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </nav>

      <div style={{ flex: 1, overflow: "hidden", position: "relative" }}>

        {/* Pre-market scan */}
        {tab === "premarket" && (
          <div style={{ height: "100%", overflowY: "auto" }}>
            <PremarketScan onViewInChart={handleViewInChart} />
          </div>
        )}

        {/* Keep scanner mounted so it keeps ticking in the background */}
        <div style={{
          display: tab === "scanner" ? "flex" : "none",
          flexDirection: "column",
          height: "100%",
        }}>
          <Dashboard />
        </div>

        {/* Today's Signals — only render when visible */}
        {tab === "today" && (
          <div style={{ height: "100%", overflowY: "auto" }}>
            <TodaySignals
              onViewInGapFade={handleViewInGapFade}
              onViewInChart={handleViewInChart}
            />
          </div>
        )}

        {/* Swing Watchlist — generated EOD */}
        {tab === "swing" && (
          <div style={{ height: "100%", overflowY: "auto" }}>
            <SwingWatchlist />
          </div>
        )}

        {/* GapFade — kept mounted; receives external symbol when jumping from Today's Signals */}
        <div style={{
          display: tab === "gapfade" ? "block" : "none",
          height: "100%",
          overflowY: "auto",
        }}>
          <GapFade
            externalSymbol={gapFadeSymbol}
            externalDirection={gapFadeDirection}
            externalTrade={gapFadeTrade}
          />
        </div>

        {/* Chart — kept mounted so it doesn't reset mid-session */}
        <div style={{
          display: tab === "chart" ? "flex" : "none",
          flexDirection: "column",
          height: "100%",
        }}>
          <ChartView
            initialSymbol={chartSymbol}
            initialSig={chartSig}
          />
        </div>

        {/* Universe Backtest */}
        {tab === "backtest" && (
          <div style={{ height: "100%", overflowY: "auto" }}>
            <UniverseBacktest />
          </div>
        )}
      </div>
    </div>
  );
}
