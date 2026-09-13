import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import * as echarts from "echarts";

const API = "http://localhost:5000";

const QUICK_PICKS = [
  "SPY","QQQ","NVDA","AAPL","MSFT","TSLA","AMD","META","GOOGL","AMZN",
  "ORCL","AVGO","MU","SMCI","SNOW","DDOG","CRWD","PANW","JPM","GS",
];

const TABS = [
  { key: "1m", label: "1-MIN",  sub: "Today + Pre-market" },
  { key: "5m", label: "5-MIN",  sub: "Entry timing"       },
  { key: "1h", label: "1-HOUR", sub: "30-day context"     },
];

const LEVEL_DEFS = {
  pdh: { color: "#22c55e", name: "PDH", width: 1.5, type: "solid"  },
  pdl: { color: "#ef4444", name: "PDL", width: 1.5, type: "solid"  },
  pmh: { color: "#f97316", name: "PMH", width: 1,   type: "dashed" },
  pml: { color: "#3b82f6", name: "PML", width: 1,   type: "dashed" },
  pdc: { color: "#eab308", name: "PDC", width: 1,   type: "dotted" },
};

function fmtET(utcSec) {
  const d = new Date(utcSec * 1000);
  return d.toLocaleString("en-US", {
    timeZone: "America/New_York",
    month: "numeric", day: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function LevelBox({ levelKey, label, sub, value, color, current, active, onToggle }) {
  return (
    <div
      onClick={!current && value != null ? onToggle : undefined}
      style={{
        padding: "8px 16px", minWidth: 110,
        background: current ? "#111" : (active ? color + "0d" : "#0a0a0a"),
        border: `1px solid ${current ? "#2a2a2a" : (active ? color + "33" : "#1f1f1f")}`,
        cursor: current || value == null ? "default" : "pointer",
        opacity: !current && value != null && !active ? 0.35 : 1,
        transition: "opacity 0.15s, border-color 0.15s, background 0.15s",
        userSelect: "none",
        position: "relative",
      }}
    >
      {!current && value != null && (
        <div style={{
          position: "absolute", top: 4, right: 5,
          fontSize: 8, color: active ? color : "#333",
          fontWeight: 700, letterSpacing: 0.5,
        }}>
          {active ? "ON" : "OFF"}
        </div>
      )}
      <div style={{ fontSize: 9, color: current ? "#777" : color, fontWeight: 700, letterSpacing: 1 }}>
        {label}
      </div>
      {sub && <div style={{ fontSize: 9, color: "#444", marginTop: 1 }}>{sub}</div>}
      <div style={{
        fontSize: 20, fontWeight: 800, fontFamily: "monospace",
        color: current ? "#eee" : (value ? color : "#333"),
        marginTop: 4,
      }}>
        {value != null ? `$${value.toFixed(2)}` : "—"}
      </div>
    </div>
  );
}

function buildLevelLines(lvls, visible) {
  return Object.entries(LEVEL_DEFS)
    .filter(([key]) => visible[key] && lvls[key])
    .map(([key, def]) => {
      const v = lvls[key];
      return {
        yAxis: v, name: def.name,
        lineStyle: { color: def.color, width: def.width, type: def.type },
        label: {
          formatter: `${def.name}  $${v.toFixed(2)}`,
          color: "#fff", backgroundColor: def.color,
          padding: [3, 7], borderRadius: 3,
          fontSize: 10, fontWeight: 700, position: "insideStartTop",
        },
      };
    });
}

function buildChart(ecRef, dom, bars, lvls, visible, startPct = 70) {
  if (!dom || !bars?.length) return;
  ecRef.current?.dispose();
  const chart = echarts.init(dom, null, { renderer: "canvas", useDirtyRect: true });
  ecRef.current = chart;

  const clean = cleanBars(bars);
  if (!clean.length) return;

  const times = clean.map(b => fmtET(b.t));

  chart.setOption({
    backgroundColor: "#000",
    animation: false,
    grid: [{ top: 8, bottom: 28, left: 2, right: 112 }],
    xAxis: [{
      type: "category", data: times,
      axisLine:  { lineStyle: { color: "#141414" } },
      splitLine: { lineStyle: { color: "#0d0d0d" } },
      axisLabel: { color: "#3a3a3a", fontSize: 10, interval: "auto" },
      axisTick:  { show: false },
    }],
    yAxis: [{
      scale: true, position: "right",
      axisLine:  { show: false },
      splitLine: { lineStyle: { color: "#0d0d0d" } },
      axisLabel: { color: "#444", fontSize: 10, formatter: v => v.toFixed(2), margin: 6 },
    }],
    dataZoom: [{
      type: "inside", start: startPct, end: 100,
      filterMode: "weakFilter",
      zoomOnMouseWheel: true, moveOnMouseWheel: false,
      maxSpan: 100,
      preventDefaultMouseMove: false,
    }],
    tooltip: {
      trigger: "axis",
      axisPointer: {
        type: "cross",
        crossStyle: { color: "#2a2a2a", width: 1 },
        label: { backgroundColor: "#111", color: "#aaa", fontSize: 10 },
      },
      backgroundColor: "#0d0d0d",
      borderColor: "#1a1a1a",
      textStyle: { color: "#bbb", fontSize: 11 },
      formatter(params) {
        const c = params.find(p => p.seriesIndex === 0);
        if (!c?.value) return "";
        const [o, cl, l, h] = c.value;
        const col = cl >= o ? "#26a69a" : "#ef5350";
        return `<div style="font-size:10px;line-height:1.7">
          <b style="color:#555">${c.name}</b><br>
          O <b>${o.toFixed(2)}</b>  H <b>${h.toFixed(2)}</b>
          L <b>${l.toFixed(2)}</b>  C <b style="color:${col}">${cl.toFixed(2)}</b>
        </div>`;
      },
    },
    series: [{
      type: "candlestick",
      data: candleData(clean),
      itemStyle: {
        color: "#26a69a", color0: "#ef5350",
        borderColor: "#26a69a", borderColor0: "#ef5350",
      },
      barMaxWidth: 14,
      markLine: {
        silent: true, symbol: "none", animation: false,
        lineStyle: { type: "solid" },
        data: buildLevelLines(lvls, visible),
      },
    }],
  });
}

const ALL_ON = { pdh: true, pdl: true, pmh: true, pml: true, pdc: true };

function cleanBars(bars) {
  return (bars || []).filter(b => {
    if (!b.o || !b.h || !b.l || !b.c) return false;
    const mid = (b.h + b.l) / 2;
    return mid > 0 && Math.abs(b.o - mid) / mid < 0.5;
  });
}

function candleData(bars) {
  return bars.map(b => {
    const bull = b.c >= b.o;
    const ext  = b.rth === false;
    return {
      value: [b.o, b.c, b.l, b.h],
      itemStyle: ext ? {
        color:       bull ? "#0f2018" : "#200f0f",
        borderColor: bull ? "#1a3828" : "#381a1a",
      } : undefined,
    };
  });
}

export default function ChartView({ initialSymbol, initialSig }) {
  const [input,         setInput]         = useState(initialSymbol || "SPY");
  const [ticker,        setTicker]        = useState(initialSymbol || "SPY");
  const [loading,       setLoading]       = useState(false);
  const [loaded,        setLoaded]        = useState(false);
  const [levels,        setLevels]        = useState({});
  const [pmNote,        setPmNote]        = useState("");
  const [livePrice,     setLivePrice]     = useState(null);
  const [signals,       setSignals]       = useState({});
  const [activeTab,     setActiveTab]     = useState("1m");
  const [visibleLevels, setVisibleLevels] = useState({ ...ALL_ON });
  const [lastRefresh,   setLastRefresh]   = useState(null);
  const [chartPaused,   setChartPaused]   = useState(false);

  const ref1m = useRef(null);
  const ref5m = useRef(null);
  const ref1h = useRef(null);
  const ec1m  = useRef(null);
  const ec5m  = useRef(null);
  const ec1h  = useRef(null);
  const pollRef        = useRef(null);
  const refreshRef     = useRef(null);
  const chartPausedRef = useRef(false);

  // Refs so the refresh interval can read latest values without stale closures
  const tickerRef        = useRef(ticker);
  const levelsRef        = useRef(levels);
  const visibleRef       = useRef(visibleLevels);
  const loadedRef        = useRef(loaded);
  useEffect(() => { tickerRef.current  = ticker;        }, [ticker]);
  useEffect(() => { levelsRef.current  = levels;        }, [levels]);
  useEffect(() => { visibleRef.current = visibleLevels; }, [visibleLevels]);
  useEffect(() => { loadedRef.current  = loaded;        }, [loaded]);

  const ecMap  = { "1m": ec1m,  "5m": ec5m,  "1h": ec1h  };
  const refMap = { "1m": ref1m, "5m": ref5m, "1h": ref1h };
  const TF_MAP = { "1m": "1m",  "5m": "5m",  "1h": "1h"  };

  // Silently update bars on all three charts — preserves zoom/pan position
  const refreshBars = useCallback(async () => {
    const s = tickerRef.current;
    if (!s || !loadedRef.current) return;
    try {
      const [d1m, d5m, d1h] = await Promise.all([
        fetch(`${API}/api/intraday_chart/${s}?tf=1m`).then(r => r.json()).catch(() => null),
        fetch(`${API}/api/intraday_chart/${s}?tf=5m`).then(r => r.json()).catch(() => null),
        fetch(`${API}/api/intraday_chart/${s}?tf=1h`).then(r => r.json()).catch(() => null),
      ]);

      const lvls    = levelsRef.current;
      const visible = visibleRef.current;

      for (const [ecRef, data] of [[ec1m, d1m], [ec5m, d5m], [ec1h, d1h]]) {
        if (!data?.bars?.length || !ecRef.current) continue;
        const chart = ecRef.current;
        const zoom  = chart.getOption()?.dataZoom?.[0] || {};
        const clean = cleanBars(data.bars);
        chart.setOption({
          xAxis:    [{ data: clean.map(b => fmtET(b.t)) }],
          series:   [{ data: candleData(clean), markLine: { data: buildLevelLines(lvls, visible) } }],
          dataZoom: [{ start: zoom.start ?? 70, end: zoom.end ?? 100 }],
        });
      }
      setLastRefresh(new Date());
    } catch {}
  }, []);

  // Auto-refresh every 60s (bars)
  useEffect(() => {
    refreshRef.current = setInterval(() => {
      if (!chartPausedRef.current) refreshBars();
    }, 60_000);
    return () => clearInterval(refreshRef.current);
  }, [refreshBars]);

  // Update markLines on all charts when visible toggles change
  useEffect(() => {
    if (!loaded) return;
    const lines = buildLevelLines(levels, visibleLevels);
    [ec1m, ec5m, ec1h].forEach(ecRef => {
      ecRef.current?.setOption({ series: [{ markLine: { data: lines } }] });
    });
  }, [visibleLevels, loaded, levels]);

  // Signal badges
  useEffect(() => {
    const load = async () => {
      try {
        const r = await fetch(`${API}/api/today_signals`);
        const d = await r.json();
        const map = {};
        for (const t of (d.trades || []))
          if (!map[t.symbol] || (t.score || 0) > (map[t.symbol].score || 0))
            map[t.symbol] = t;
        setSignals(map);
      } catch {}
    };
    load();
    const id = setInterval(load, 60_000);
    return () => clearInterval(id);
  }, []);

  // Live price poll — every 15s
  useEffect(() => {
    if (!ticker) return;
    clearInterval(pollRef.current);
    const poll = async () => {
      try {
        const r = await fetch(`${API}/api/price?ticker=${encodeURIComponent(ticker)}`);
        if (r.ok) { const d = await r.json(); if (d.price) setLivePrice(+d.price); }
      } catch {}
    };
    poll();
    pollRef.current = setInterval(() => {
      if (!chartPausedRef.current) poll();
    }, 15_000);
    return () => clearInterval(pollRef.current);
  }, [ticker]);

  const loadLevels = useCallback(async (sym) => {
    const s = (sym || input).trim().toUpperCase();
    if (!s) return;
    setTicker(s);
    setInput(s);
    setLoading(true);
    setLoaded(false);
    setLevels({});
    setLivePrice(null);

    try {
      const [d1m, d5m, d1h] = await Promise.all([
        fetch(`${API}/api/intraday_chart/${s}?tf=1m`).then(r => r.json()).catch(() => ({ bars: [], levels: {} })),
        fetch(`${API}/api/intraday_chart/${s}?tf=5m`).then(r => r.json()).catch(() => ({ bars: [], levels: {} })),
        fetch(`${API}/api/intraday_chart/${s}?tf=1h`).then(r => r.json()).catch(() => ({ bars: [], levels: {} })),
      ]);

      const lvls = d1m.levels || d5m.levels || {};
      setLevels(lvls);
      setPmNote(!lvls.pmh ? "No pre-market data yet — PMH/PML will appear after 4:00 AM ET" : "");

      const n1m = (d1m.bars || []).length;
      const n5m = (d5m.bars || []).length;
      const s1m = n1m > 400 ? 100 - Math.round(400 / n1m * 100) : 0;
      const s5m = n5m > 200 ? 100 - Math.round(200 / n5m * 100) : 0;

      buildChart(ec1m, ref1m.current, d1m.bars || [], lvls, visibleLevels, s1m);
      buildChart(ec5m, ref5m.current, d5m.bars || [], lvls, visibleLevels, s5m);
      buildChart(ec1h, ref1h.current, d1h.bars || [], lvls, visibleLevels, 0);

      setLoaded(true);
      setLastRefresh(new Date());
    } finally {
      setLoading(false);
    }
  }, [input, visibleLevels]);

  useEffect(() => { if (initialSymbol) loadLevels(initialSymbol); }, [initialSymbol]);
  useEffect(() => { loadLevels("SPY"); }, []);

  // Resize on tab switch + ResizeObserver
  useEffect(() => {
    if (!loaded) return;
    ecMap[activeTab]?.current?.resize();

    const ro = new ResizeObserver(() => ecMap[activeTab]?.current?.resize());
    const el = refMap[activeTab]?.current;
    if (el) ro.observe(el);
    return () => ro.disconnect();
  }, [loaded, activeTab]);

  const toggleLevel = (key) =>
    setVisibleLevels(prev => ({ ...prev, [key]: !prev[key] }));

  const allOn  = Object.values(visibleLevels).every(Boolean);

  const activeSig = initialSig || signals[ticker];
  const sigColor  = !activeSig ? "#f59e0b"
    : activeSig.direction === "CALL" ? "#22c55e" : "#ef4444";
  const sigLabel  = !activeSig ? "RANGE — WAIT"
    : activeSig.direction === "CALL"
      ? `▲ BULLISH · Grade ${activeSig.grade} · ${activeSig.score?.toFixed(0)}%`
      : `▼ BEARISH · Grade ${activeSig.grade} · ${activeSig.score?.toFixed(0)}%`;

  return (
    <div style={{
      background: "#000", height: "100%", display: "flex", flexDirection: "column",
      fontFamily: "monospace", color: "#ccc", overflow: "hidden",
    }}>

      {/* ── Header ── */}
      <div style={{ padding: "10px 16px 8px", borderBottom: "1px solid #111", flexShrink: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#ddd", marginBottom: 10 }}>
          Levels{" "}
          <span style={{ color: "#333", fontWeight: 400, fontSize: 11 }}>
            — Chloe Protocol (Weekly→Daily→1hr→5min→1min)
          </span>
        </div>

        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <form onSubmit={e => { e.preventDefault(); loadLevels(); }} style={{ display: "flex", gap: 6 }}>
            <input
              value={input}
              onChange={e => setInput(e.target.value.toUpperCase())}
              style={{
                width: 90, padding: "6px 10px", background: "#0a0a0a",
                border: "1px solid #1f1f1f", color: "#ccc",
                fontFamily: "monospace", fontSize: 14, fontWeight: 700, borderRadius: 3,
              }}
            />
            <button type="submit" disabled={loading} style={{
              padding: "6px 16px", background: loading ? "#111" : "#0d2d0d",
              border: `1px solid ${loading ? "#1a1a1a" : "#1a5c1a"}`,
              color: loading ? "#444" : "#22c55e", fontFamily: "monospace",
              fontSize: 12, fontWeight: 700, cursor: loading ? "not-allowed" : "pointer",
              borderRadius: 3, letterSpacing: 0.5,
            }}>
              {loading ? "Loading…" : "Load ▶"}
            </button>
          </form>

          <div style={{
            padding: "6px 14px", border: `1px solid ${sigColor}44`,
            background: sigColor + "11", borderRadius: 3,
            fontSize: 11, fontWeight: 700, color: sigColor, letterSpacing: 0.5,
          }}>
            {sigLabel}
          </div>

          {/* Live indicator + manual refresh + stop */}
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <div style={{
              width: 6, height: 6, borderRadius: "50%",
              background: loaded && !chartPaused ? "#22c55e" : chartPaused ? "#ef4444" : "#333",
              boxShadow: loaded && !chartPaused ? "0 0 5px #22c55e99" : "none",
            }} title={chartPaused ? "Auto-refresh paused" : "Live"} />
            {lastRefresh && (
              <span style={{ fontSize: 9, color: "#333", fontFamily: "monospace" }}>
                {lastRefresh.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}
              </span>
            )}
            <button onClick={refreshBars} disabled={!loaded} title="Refresh bars now" style={{
              padding: "3px 8px", background: "transparent",
              border: "1px solid #1f1f1f", color: "#444", borderRadius: 3,
              fontSize: 9, fontWeight: 700, cursor: loaded ? "pointer" : "not-allowed",
              fontFamily: "monospace",
            }}>↺</button>
            <button
              onClick={() => {
                const next = !chartPausedRef.current;
                chartPausedRef.current = next;
                setChartPaused(next);
              }}
              title={chartPaused ? "Resume auto-refresh (bars every 60s, price every 15s)" : "Stop auto-refresh to reduce API calls"}
              style={{
                padding: "3px 8px", borderRadius: 3, fontSize: 9, fontWeight: 700,
                fontFamily: "monospace", cursor: "pointer",
                border: chartPaused ? "1px solid #7f1d1d" : "1px solid #1f1f1f",
                background: chartPaused ? "#3d1f1f" : "transparent",
                color: chartPaused ? "#f87171" : "#444",
              }}
            >{chartPaused ? "▶" : "⏹"}</button>
          </div>

          <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
            {QUICK_PICKS.map(s => (
              <button key={s} onClick={() => loadLevels(s)} style={{
                padding: "3px 8px", borderRadius: 3, fontSize: 10, fontWeight: 600,
                border: `1px solid ${ticker === s ? "#333" : "#1a1a1a"}`,
                background: ticker === s ? "#1a1a1a" : "transparent",
                color: ticker === s ? "#bbb" : "#444", cursor: "pointer",
                fontFamily: "monospace",
              }}>{s}</button>
            ))}
          </div>
        </div>
      </div>

      {/* ── Level boxes (clickable toggles) ── */}
      <div style={{
        display: "flex", gap: 1, padding: "8px 16px", flexShrink: 0,
        background: "#050505", borderBottom: "1px solid #111",
        alignItems: "center",
      }}>
        <LevelBox label="CURRENT" value={livePrice} color="#fff" current />
        <div style={{ width: 1, background: "#111", margin: "0 6px" }} />
        <LevelBox levelKey="pdh" label="PDH" sub="prev high"  value={levels.pdh} color="#22c55e" active={visibleLevels.pdh} onToggle={() => toggleLevel("pdh")} />
        <LevelBox levelKey="pdl" label="PDL" sub="prev low"   value={levels.pdl} color="#ef4444" active={visibleLevels.pdl} onToggle={() => toggleLevel("pdl")} />
        <div style={{ width: 1, background: "#111", margin: "0 6px" }} />
        <LevelBox levelKey="pmh" label="PMH" sub="pm high"    value={levels.pmh} color="#f97316" active={visibleLevels.pmh} onToggle={() => toggleLevel("pmh")} />
        <LevelBox levelKey="pml" label="PML" sub="pm low"     value={levels.pml} color="#3b82f6" active={visibleLevels.pml} onToggle={() => toggleLevel("pml")} />
        {levels.pdc && <>
          <div style={{ width: 1, background: "#111", margin: "0 6px" }} />
          <LevelBox levelKey="pdc" label="PDC" sub="prev close" value={levels.pdc} color="#eab308" active={visibleLevels.pdc} onToggle={() => toggleLevel("pdc")} />
        </>}

        {/* Reset all button — only shows when something is off */}
        {!allOn && (
          <button
            onClick={() => setVisibleLevels({ ...ALL_ON })}
            style={{
              marginLeft: 12, padding: "4px 10px", background: "transparent",
              border: "1px solid #2a2a2a", color: "#555", borderRadius: 3,
              fontSize: 9, fontWeight: 700, cursor: "pointer", letterSpacing: 0.5,
              fontFamily: "monospace",
            }}
          >
            SHOW ALL
          </button>
        )}
      </div>

      {pmNote && (
        <div style={{ padding: "4px 16px", fontSize: 10, color: "#444", background: "#050505", flexShrink: 0 }}>
          {pmNote}
        </div>
      )}

      {/* ── Tabs ── */}
      <div style={{ display: "flex", gap: 0, borderBottom: "1px solid #111", flexShrink: 0 }}>
        {TABS.map(t => {
          const active = t.key === activeTab;
          return (
            <button key={t.key} onClick={() => setActiveTab(t.key)} style={{
              padding: "8px 24px", border: "none", cursor: "pointer",
              background: active ? "#0a0a0a" : "transparent",
              borderBottom: active ? "2px solid #22c55e" : "2px solid transparent",
              color: active ? "#ddd" : "#444",
              fontFamily: "monospace", fontSize: 12, fontWeight: active ? 700 : 400,
              display: "flex", flexDirection: "column", alignItems: "center", gap: 1,
            }}>
              <span>{t.label}</span>
              <span style={{ fontSize: 9, color: active ? "#555" : "#2a2a2a" }}>{t.sub}</span>
            </button>
          );
        })}
      </div>

      {/* ── Chart area ── */}
      <div style={{ flex: 1, position: "relative", minHeight: 0 }}>
        {TABS.map(t => (
          <div key={t.key} style={{
            position: "absolute", inset: 0,
            display: t.key === activeTab ? "block" : "none",
          }}>
            <div ref={refMap[t.key]} style={{ width: "100%", height: "100%" }} />
          </div>
        ))}
      </div>

    </div>
  );
}
