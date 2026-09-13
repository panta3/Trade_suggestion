import { useEffect, useRef, useState } from "react";
import { createChart, CrosshairMode, LineStyle } from "lightweight-charts";

const API = "http://localhost:5000";

const C = {
  bg:     "#0d1117",
  grid:   "#21262d",
  text:   "#e6edf3",
  green:  "#3fb950",
  red:    "#f85149",
  blue:   "#58a6ff",
  purple: "#bc8cff",
  orange: "#e3b341",
  yellow: "#d29922",
  muted:  "#8b949e",
  accent: "#1f6feb",
};

function toETLabel(utcSec) {
  return new Date(utcSec * 1000).toLocaleTimeString("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

// ── Options hint (heuristic — no Black-Scholes here, link to Gap-Fade tab) ───
function OptionsHint({ sig }) {
  if (!sig) return null;
  const isCall = sig.direction === "CALL";
  const entry  = sig.entry;

  // Strike increment by price range
  const inc    = entry > 500 ? 10 : entry > 100 ? 5 : entry > 20 ? 1 : 0.5;
  const strike = isCall
    ? Math.ceil(entry / inc) * inc
    : Math.floor(entry / inc) * inc;

  // Next Friday that is ≥ 5 days away
  const now = new Date();
  const day = now.getDay(); // 0=Sun … 6=Sat
  let daysToFri = ((5 - day + 7) % 7) || 7;
  if (daysToFri < 5) daysToFri += 7;
  const expiry = new Date(now.getTime() + daysToFri * 86400000);
  const expiryStr = expiry.toLocaleDateString("en-US", { month: "short", day: "numeric" });

  return (
    <div className="panel-section">
      <h4>Options Hint</h4>
      <div className="kv-grid">
        <div className="kv-item">
          <div className="kv-label">Strike</div>
          <div className="kv-val">${strike} {isCall ? "C" : "P"}</div>
        </div>
        <div className="kv-item">
          <div className="kv-label">Expiry (~{daysToFri} DTE)</div>
          <div className="kv-val">{expiryStr}</div>
        </div>
      </div>
      <div style={{ fontSize: 10, color: C.muted, marginTop: 5 }}>
        Target delta ~0.40 — refine in the Gap-Fade tab
      </div>
    </div>
  );
}

// ── Indicator KV grid ─────────────────────────────────────────────────────────
function KVGrid({ sig }) {
  if (!sig) return null;
  const items = [
    ["RSI",    sig.rsi     != null ? sig.rsi.toFixed(1)             : "—"],
    ["R.Vol",  sig.rel_vol != null ? sig.rel_vol.toFixed(1) + "x"   : "—"],
    ["ADX",    sig.adx     != null ? sig.adx.toFixed(1)             : "—"],
    ["ATR",    sig.atr     != null ? "$" + sig.atr.toFixed(3)       : "—"],
    ["VWAP",   sig.vwap    != null ? "$" + sig.vwap.toFixed(2)      : "—"],
    ["R:R",    sig.rr      != null ? sig.rr.toFixed(2) + "R"        : "—"],
    ["RS/SPY", sig.rs_vs_spy != null
      ? (sig.rs_vs_spy >= 0 ? "+" : "") + sig.rs_vs_spy.toFixed(1) + "%" : "—"],
  ];
  return (
    <div className="panel-section">
      <h4>Indicators</h4>
      <div className="kv-grid">
        {items.map(([k, v]) => (
          <div key={k} className="kv-item">
            <div className="kv-label">{k}</div>
            <div className="kv-val">{v}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────
export default function ChartPanel({ symbol, timeframe = "5m", sig, onClose, onLevelsLoaded, embedded }) {
  const mainRef    = useRef(null);
  const rsiRef     = useRef(null);
  const pollRef    = useRef(null);
  const refreshRef = useRef(null);
  const seriesRef  = useRef({});  // { candle, vol, vwap, ema9, ema21, rsiLine }

  const [status,    setStatus]    = useState("loading");
  const [errMsg,    setErrMsg]    = useState("");
  const [lastClose, setLastClose] = useState(null);
  const [livePrice, setLivePrice] = useState(null);

  // Toggle state for overlays — EMAs off by default (less clutter)
  const [vis, setVis] = useState({ vwap: true, ema9: false, ema21: false });
  const visRef = useRef({ vwap: true, ema9: false, ema21: false });

  const toggleVis = (key) => {
    const sr = seriesRef.current;
    if (!sr[key]) return;
    setVis(prev => {
      const next = { ...prev, [key]: !prev[key] };
      visRef.current = next;
      sr[key].applyOptions({ visible: next[key] });
      return next;
    });
  };

  // ── Live price polling (every 30s) ────────────────────────────────────────
  useEffect(() => {
    if (!symbol) return;
    clearInterval(pollRef.current);

    const poll = async () => {
      try {
        const r = await fetch(`${API}/api/price?ticker=${encodeURIComponent(symbol)}`);
        if (!r.ok) return;
        const d = await r.json();
        if (d.price) setLivePrice(+d.price);
      } catch {}
    };

    poll();
    const pid = setInterval(poll, 30_000);
    pollRef.current = pid;
    return () => clearInterval(pid);
  }, [symbol]);

  // ── 60s chart-data refresh: only active on intraday (5m) view ───────────
  useEffect(() => {
    if (!symbol || timeframe !== "5m") return;
    clearInterval(refreshRef.current);

    const refresh = async () => {
      try {
        const r = await fetch(`${API}/api/intraday_chart/${encodeURIComponent(symbol)}?tf=5m`);
        if (!r.ok) return;
        const data = await r.json();
        if (data.error) return;

        const bars = data.bars || [];
        if (!bars.length) return;
        const lb = bars[bars.length - 1];
        setLastClose(lb.c);

        const sr = seriesRef.current;
        if (sr.candle) {
          sr.candle.update({ time: lb.t, open: lb.o, high: lb.h, low: lb.l, close: lb.c });
        }
        if (sr.vol) {
          sr.vol.update({ time: lb.t, value: lb.v, color: lb.c >= lb.o ? "#1a4226aa" : "#4a1a1aaa" });
        }
        for (const key of ["vwap", "ema9", "ema21"]) {
          const pts = data.series?.[key];
          if (pts?.length && sr[key]) sr[key].update({ time: pts.at(-1).t, value: pts.at(-1).v });
        }
        const rsiPts = data.series?.rsi;
        if (rsiPts?.length && sr.rsiLine) sr.rsiLine.update({ time: rsiPts.at(-1).t, value: rsiPts.at(-1).v });
      } catch {}
    };

    const rid = setInterval(refresh, 60_000);
    refreshRef.current = rid;
    return () => clearInterval(rid);
  }, [symbol, timeframe]);

  // ── Chart build (re-runs when symbol, timeframe, or signal lines change) ──
  useEffect(() => {
    if (!symbol) return;

    let cancelled = false;
    const obj = { main: null, rsi: null, ro: null };
    seriesRef.current = {};

    setStatus("loading");
    setErrMsg("");
    setLastClose(null);

    // Fetch RTH chart data + extended hours bars in parallel (intraday only)
    const chartFetch   = fetch(`${API}/api/intraday_chart/${encodeURIComponent(symbol)}?tf=${timeframe}`)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); });
    const extFetch = timeframe === "5m"
      ? fetch(`${API}/api/extended_chart/${encodeURIComponent(symbol)}?period=5d`)
          .then(r => r.ok ? r.json() : { bars: [] })
          .catch(() => ({ bars: [] }))
      : Promise.resolve({ bars: [] });

    Promise.all([chartFetch, extFetch])
      .then(([data, extData]) => {
        if (cancelled || !mainRef.current) return;
        if (data.error) throw new Error(data.error);

        // Merge: use extended bars as the full dataset (they include RTH).
        // If extended fetch failed or returned nothing, fall back to RTH-only.
        const extBars = extData.bars || [];
        const allBars = extBars.length > 0 ? extBars : (data.bars || []).map(b => ({ ...b, rth: true }));

        // ── Main chart ────────────────────────────────────────────────────
        const mainEl = mainRef.current;
        const main = createChart(mainEl, {
          autoSize: true,   // fills container; ResizeObserver not needed
          layout: {
            background: { color: "#0b0e17" },
            textColor: "#c9d1d9",
            fontSize: 11,
            fontFamily: "Inter, system-ui, sans-serif",
          },
          grid: {
            vertLines: { color: "#161b27" },
            horzLines: { color: "#161b27" },
          },
          crosshair: {
            mode: CrosshairMode.Normal,
            vertLine: { color: "#3d7ebf", width: 1, style: 3, labelBackgroundColor: "#1e3a5f" },
            horzLine: { color: "#3d7ebf", width: 1, style: 3, labelBackgroundColor: "#1e3a5f" },
          },
          rightPriceScale: {
            borderColor: "#21262d",
            scaleMargins: { top: 0.06, bottom: 0.20 },
            textColor: "#8b949e",
          },
          timeScale: {
            borderColor: "#21262d",
            timeVisible: true,
            secondsVisible: false,
            tickMarkFormatter: t => toETLabel(t),
            rightOffset: 8,
            barSpacing: 8,
            minBarSpacing: 2,
          },
          // mouseWheel = zoom only; click+drag = pan
          handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
          handleScale:  { mouseWheel: true,  pinch: true, axisPressedMouseMove: { time: true, price: true } },
        });
        obj.main = main;

        // Candlestick — TradingView classic palette; extended hours dim
        const candle = main.addCandlestickSeries({
          upColor:        "#26a69a",
          downColor:      "#ef5350",
          borderUpColor:  "#26a69a",
          borderDownColor:"#ef5350",
          wickUpColor:    "#26a69a",
          wickDownColor:  "#ef5350",
        });
        seriesRef.current.candle = candle;
        const bars = allBars.map(b => {
          const bull = b.c >= b.o;
          if (b.rth === false) {
            return {
              time: b.t, open: b.o, high: b.h, low: b.l, close: b.c,
              color:       bull ? "#163028" : "#321818",
              borderColor: bull ? "#1e5040" : "#502020",
              wickColor:   "#333",
            };
          }
          return { time: b.t, open: b.o, high: b.h, low: b.l, close: b.c };
        });
        candle.setData(bars);

        // Last close from RTH bars only (extended close ≠ trading price)
        const rthBars = allBars.filter(b => b.rth !== false);
        if (rthBars.length) setLastClose(rthBars[rthBars.length - 1].c);
        if (onLevelsLoaded && data.levels) onLevelsLoaded(data.levels);

        // Volume — use RTH data (extended volume is thin and misleading on chart)
        const vol = main.addHistogramSeries({
          priceFormat: { type: "volume" }, priceScaleId: "vol",
        });
        seriesRef.current.vol = vol;
        main.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
        vol.setData((data.bars || []).map(b => ({
          time: b.t, value: b.v,
          color: b.c >= b.o ? "#26a69a55" : "#ef535055",
        })));

        // VWAP / EMA9 / EMA21
        const lineOpts = [
          ["vwap",  C.blue,   1.5, "VWAP",  true],
          ["ema9",  C.orange, 1,   "EMA9",  false],
          ["ema21", C.purple, 1,   "EMA21", false],
        ];
        for (const [key, color, lw, title, showLast] of lineOpts) {
          if (!data.series?.[key]?.length) continue;
          const s = main.addLineSeries({
            color, lineWidth: lw, title,
            priceLineVisible: false, lastValueVisible: showLast,
            crosshairMarkerVisible: false,
          });
          s.setData(data.series[key].map(p => ({ time: p.t, value: p.v })));
          seriesRef.current[key] = s;
          // Apply current user visibility preference (persists across symbol changes)
          s.applyOptions({ visible: visRef.current[key] ?? true });
        }

        // Entry / Stop / Target price lines from active signal
        if (sig) {
          [[sig.entry, C.blue,  "Entry"],
           [sig.target,C.green, "Target"],
           [sig.stop,  C.red,   "Stop"]
          ].forEach(([price, color, title]) => {
            if (price) candle.createPriceLine({
              price, color, lineWidth: 1,
              lineStyle: LineStyle.Dashed, axisLabelVisible: true, title,
            });
          });
        }

        // Key levels — Chloe's 4 levels + PDC + 1H swing
        // PDH/PDL solid thick, PMH/PML dashed, PDC dotted yellow, 1H faint
        const lvls = data.levels || {};
        [
          [lvls.pdh,          "#ef4444", "PDH",     2, LineStyle.Solid],
          [lvls.pdl,          "#22c55e", "PDL",     2, LineStyle.Solid],
          [lvls.pmh,          "#f97316", "PMH",     1, LineStyle.Dashed],
          [lvls.pml,          "#3b82f6", "PML",     1, LineStyle.Dashed],
          [lvls.pdc,          "#eab308", "PDC",     1, LineStyle.Dotted],
          [lvls.ah_high,      "#a855f7", "AH High", 1, LineStyle.Dashed],
          [lvls.ah_low,       "#a855f7", "AH Low",  1, LineStyle.Dashed],
          [lvls.h1_swing_high,"#6b7280", "1H High", 1, LineStyle.Dotted],
          [lvls.h1_swing_low, "#6b7280", "1H Low",  1, LineStyle.Dotted],
        ].forEach(([price, color, title, lineWidth, lineStyle]) => {
          if (!price) return;
          candle.createPriceLine({
            price, color, lineWidth, lineStyle, axisLabelVisible: true, title,
          });
        });

        // Signal markers — triangles at the bar where algo fired
        const mkrs = (data.markers || []).map(m => ({
          time:     m.time,
          position: m.direction === "CALL" ? "belowBar" : "aboveBar",
          color:    m.direction === "CALL" ? C.green : C.red,
          shape:    m.direction === "CALL" ? "arrowUp" : "arrowDown",
          text:     `${m.grade} ${m.score?.toFixed(0)}%${m.pnl != null ? ` → ${m.pnl > 0 ? "+" : ""}${m.pnl.toFixed(1)}%` : ""}`,
          size:     m.grade === "S" ? 2 : 1,
        }));
        if (mkrs.length) candle.setMarkers(mkrs);

        main.timeScale().fitContent();
        setStatus("ok");

        // ── RSI sub-chart ─────────────────────────────────────────────────
        if (rsiRef.current && data.series?.rsi?.length) {
          const rsiEl = rsiRef.current;
          const rsiChart = createChart(rsiEl, {
            autoSize: true,
            layout: { background: { color: "#0b0e17" }, textColor: "#8b949e", fontSize: 10 },
            grid:   { vertLines: { color: "transparent" }, horzLines: { color: "#161b27" } },
            crosshair: { mode: CrosshairMode.Normal,
              vertLine: { color: "#3d7ebf", width: 1, style: 3 },
              horzLine: { color: "#3d7ebf", width: 1, style: 3 },
            },
            rightPriceScale: {
              borderColor: "#21262d",
              scaleMargins: { top: 0.1, bottom: 0.1 },
              minimumWidth: 44,
            },
            timeScale: { borderColor: "#21262d", visible: false },
            handleScroll: { mouseWheel: false, pressedMouseMove: true },
            handleScale:  { mouseWheel: true,  pinch: true },
          });
          obj.rsi = rsiChart;

          const rsiLine = rsiChart.addLineSeries({
            color: C.yellow, lineWidth: 1,
            priceLineVisible: false, lastValueVisible: true,
            crosshairMarkerVisible: false,
          });
          seriesRef.current.rsiLine = rsiLine;
          rsiLine.setData(data.series.rsi.map(p => ({ time: p.t, value: p.v })));
          [
            { price: 70, color: C.red,   title: "OB" },
            { price: 30, color: C.green, title: "OS" },
            { price: 50, color: C.muted, title: "" },
          ].forEach(({ price, color, title }) =>
            rsiLine.createPriceLine({ price, color, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: !!title, title })
          );

          // Sync time scales (guard against re-entrancy)
          let syncing = false;
          main.timeScale().subscribeVisibleLogicalRangeChange(r => {
            if (syncing || !r) return;
            syncing = true;
            rsiChart.timeScale().setVisibleLogicalRange(r);
            syncing = false;
          });
          rsiChart.timeScale().subscribeVisibleLogicalRangeChange(r => {
            if (syncing || !r) return;
            syncing = true;
            main.timeScale().setVisibleLogicalRange(r);
            syncing = false;
          });
        }

        // autoSize: true handles resize automatically — no ResizeObserver needed
      })
      .catch(e => {
        if (!cancelled) { setStatus("error"); setErrMsg(e.message); }
      });

    return () => {
      cancelled = true;
      seriesRef.current = {};
      obj.ro?.disconnect();
      obj.rsi?.remove();
      obj.main?.remove();
    };
  }, [symbol, timeframe, sig?.entry, sig?.stop, sig?.target]);

  const isCall       = sig?.direction === "CALL";
  const riskDollar   = sig ? Math.abs(sig.entry - sig.stop).toFixed(2)   : null;
  const rewardDollar = sig ? Math.abs(sig.target - sig.entry).toFixed(2) : null;
  const liveDiff     = livePrice != null && lastClose != null ? livePrice - lastClose : null;

  if (embedded) {
    // Standalone tab mode — just the chart area, no panel chrome or close button
    return (
      <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
        <div className="chart-legend" style={{ padding: "4px 12px", flexShrink: 0 }}>
          {/* Toggleable overlays */}
          {[["vwap","● VWAP",C.blue],["ema9","● EMA9",C.orange],["ema21","● EMA21",C.purple]].map(([k,l,col]) => (
            <button key={k} onClick={() => toggleVis(k)} title={vis[k] ? "Click to hide" : "Click to show"} style={{
              background: vis[k] ? col+"22" : "transparent",
              border: `1px solid ${vis[k] ? col+"55" : C.muted+"33"}`,
              borderRadius: 4, padding: "1px 7px", cursor: "pointer", fontSize: 11,
              color: vis[k] ? col : C.muted,
              textDecoration: vis[k] ? "none" : "line-through",
            }}>{l}</button>
          ))}
          {/* Always-on key levels */}
          <span style={{ color: "#ef4444" }}>─ PDH</span>
          <span style={{ color: "#22c55e" }}>─ PDL</span>
          <span style={{ color: "#eab308" }}>┄ PDC</span>
          <span style={{ color: "#f97316" }}>┄ PMH</span>
          <span style={{ color: "#3b82f6" }}>┄ PML</span>
          {sig && <>
            <span style={{ color: C.blue,  opacity: 0.7 }}>╌ Entry</span>
            <span style={{ color: C.green, opacity: 0.7 }}>╌ Target</span>
            <span style={{ color: C.red,   opacity: 0.7 }}>╌ Stop</span>
          </>}
          <span style={{ color: C.muted, marginLeft: "auto" }}>ET · auto-refreshes 60s</span>
        </div>
        {status === "loading" && (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: C.muted }}>
            Loading {symbol}…
          </div>
        )}
        {status === "error" && (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "#ef4444" }}>
            ⚠ {errMsg}
          </div>
        )}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", minHeight: 0 }}>
          <div ref={mainRef} style={{ flex: 3, minHeight: 0, position: "relative" }} />
          <div ref={rsiRef}  style={{ flex: 1, minHeight: 60, borderTop: "1px solid #21262d", position: "relative" }} />
        </div>
      </div>
    );
  }

  return (
    <div className="chart-panel">
      {/* Header */}
      <div className="chart-header">
        <div className="chart-title">
          <span className="chart-sym">{symbol}</span>
          {sig ? (
            <>
              <span className={isCall ? "dir-call" : "dir-put"}>
                {isCall ? "▲" : "▼"} {sig.direction}
              </span>
              <span className="chart-meta">
                Grade {sig.grade} · {sig.score?.toFixed(0)}%
              </span>
            </>
          ) : (
            <span className="chart-meta">5-min intraday</span>
          )}
          {livePrice != null ? (
            <span className="chart-live">
              ${livePrice.toFixed(2)}
              {liveDiff != null && (
                <span style={{ color: liveDiff >= 0 ? C.green : C.red, marginLeft: 3 }}>
                  {liveDiff >= 0 ? "▲" : "▼"} {Math.abs(liveDiff).toFixed(2)}
                </span>
              )}
            </span>
          ) : lastClose != null ? (
            <span className="chart-last">${lastClose.toFixed(2)}</span>
          ) : null}
        </div>
        <button className="side-close" onClick={onClose}>✕</button>
      </div>

      {/* Body: chart column + details sidebar */}
      <div className="chart-body">
      <div className="chart-col">

      {/* Legend */}
      <div className="chart-legend">
        {[["vwap","● VWAP",C.blue],["ema9","● EMA9",C.orange],["ema21","● EMA21",C.purple]].map(([k,l,col]) => (
          <button key={k} onClick={() => toggleVis(k)} title={vis[k] ? "Click to hide" : "Click to show"} style={{
            background: vis[k] ? col+"22" : "transparent",
            border: `1px solid ${vis[k] ? col+"55" : C.muted+"33"}`,
            borderRadius: 4, padding: "1px 7px", cursor: "pointer", fontSize: 11,
            color: vis[k] ? col : C.muted,
            textDecoration: vis[k] ? "none" : "line-through",
          }}>{l}</button>
        ))}
        <span style={{ color: "#ef4444" }}>─ PDH</span>
        <span style={{ color: "#22c55e" }}>─ PDL</span>
        <span style={{ color: "#eab308" }}>┄ PDC</span>
        <span style={{ color: "#f97316" }}>┄ PMH</span>
        <span style={{ color: "#3b82f6" }}>┄ PML</span>
        {sig && <>
          <span style={{ color: C.blue,  opacity: 0.7 }}>╌ Entry</span>
          <span style={{ color: C.green, opacity: 0.7 }}>╌ Target</span>
          <span style={{ color: C.red,   opacity: 0.7 }}>╌ Stop</span>
        </>}
        <span style={{ color: C.muted, marginLeft: "auto" }}>ET</span>
      </div>

      {/* Chart area: main + RSI stacked */}
      <div className="chart-area">
        {status === "loading" && (
          <div className="chart-overlay"><span className="spinner" /> Loading {symbol}…</div>
        )}
        {status === "error" && (
          <div className="chart-overlay chart-error">⚠ {errMsg}</div>
        )}
        <div className="chart-main" ref={mainRef} />
        <div className="chart-rsi"  ref={rsiRef}  />
      </div>

      </div>{/* end chart-col */}

      {/* Signal details sidebar (only when a signal row was clicked) */}
      {sig && (
        <div className="chart-details">
          {/* Position sizing */}
          {sig.shares > 0 && (
            <div className="panel-section">
              <h4>Position Sizing  (1% risk rule)</h4>
              <div className="kv-grid">
                <div className="kv-item">
                  <div className="kv-label">Shares</div>
                  <div className="kv-val">{sig.shares}</div>
                </div>
                <div className="kv-item">
                  <div className="kv-label">Risk $</div>
                  <div className="kv-val" style={{ color: C.red }}>
                    ${sig.risk_dollar?.toFixed(2) ?? "—"}
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Trade levels */}
          <div className="panel-section">
            <h4>Trade Levels</h4>
            <div className="trade-levels">
              <div className="level-row level-entry">
                <span className="level-label">ENTRY</span>
                <span className="level-price">${sig.entry.toFixed(2)}</span>
              </div>
              <div className="level-row level-target">
                <span className="level-label">TARGET (+${rewardDollar})</span>
                <span className="level-price">${sig.target.toFixed(2)}</span>
              </div>
              <div className="level-row level-stop">
                <span className="level-label">STOP (−${riskDollar})</span>
                <span className="level-price">${sig.stop.toFixed(2)}</span>
              </div>
            </div>
          </div>

          <KVGrid sig={sig} />
          <OptionsHint sig={sig} />

          {(sig.reasons || []).length > 0 && (
            <div className="panel-section">
              <h4>Why this signal</h4>
              <div className="tag-list">
                {sig.reasons.map((r, i) => <span key={i} className="tag">{r}</span>)}
              </div>
            </div>
          )}

          {(sig.warnings || []).length > 0 && (
            <div className="panel-section">
              <h4>Cautions</h4>
              <div className="tag-list">
                {sig.warnings.map((w, i) => (
                  <span key={i} className="tag warn">⚠ {w}</span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
      </div>{/* end chart-body */}
    </div>
  );
}
