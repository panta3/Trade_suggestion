import { useState, useEffect } from "react";

const API = "http://localhost:5000";

const C = {
  bg: "#0d0d0d", surface: "#111", border: "#222",
  text: "#e8e8e8", muted: "#555", accent: "#3b82f6",
  call: "#22c55e", put: "#ef4444", gold: "#f59e0b", purple: "#a855f7",
};

const DEFAULT_SYMBOLS = [
  "SPY","QQQ","IWM","AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA",
  "AVGO","ORCL","CRM","ADBE","NFLX","AMD","QCOM","MU","AMAT","KLAC",
  "TXN","SMCI","SNOW","DDOG","WDAY","ZS","NET","CRWD","PANW","FTNT",
  "JPM","BAC","MS","V","MA","XOM","CVX","OXY","SLB","COST","PEP","SBUX","BKNG","ISRG","UBER",
];

function StatBox({ label, value, color, sub }) {
  return (
    <div style={{
      background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: 8, padding: "12px 16px", textAlign: "center", minWidth: 110,
    }}>
      <div style={{ fontSize: 10, color: C.muted, marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 800, color: color || C.text, fontFamily: "monospace" }}>{value}</div>
      {sub && <div style={{ fontSize: 10, color: C.muted, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

function WrBar({ wr }) {
  const pct = Math.min(wr || 0, 100);
  const color = pct >= 55 ? C.call : pct >= 45 ? C.gold : C.put;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <div style={{ width: 60, height: 6, background: C.border, borderRadius: 3, overflow: "hidden" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: color, borderRadius: 3 }} />
      </div>
      <span style={{ fontSize: 11, fontFamily: "monospace", color }}>{pct.toFixed(1)}%</span>
    </div>
  );
}

const UNIVERSES = [
  { key: "default",   label: `Default (${DEFAULT_SYMBOLS.length})`,  est: "~2-5 min",  warn: null },
  { key: "cream",     label: "Cream ★",                              est: "~5-10 min", warn: null },
  { key: "dj30",      label: "Dow Jones 30",                         est: "~5 min",    warn: null },
  { key: "nasdaq100", label: "NASDAQ-100",                           est: "~20-30 min",warn: "First run fetches bars from IBKR (~5s/symbol uncached)" },
  { key: "sp500",     label: "S&P 500",                              est: "~2-4 hrs",  warn: "Needs all bars cached — run overnight or in batches" },
  { key: "custom",    label: "Custom",                               est: null,        warn: null },
];

export default function UniverseBacktest() {
  const [universe,    setUniverse]    = useState("default");
  const [customSym,   setCustomSym]   = useState("");
  const [threshold,   setThreshold]   = useState(90);
  const [hold,        setHold]        = useState(26);
  const [enablePuts,  setEnablePuts]  = useState(true);
  const [running,   setRunning]   = useState(false);
  const [elapsed,   setElapsed]   = useState(0);
  const [results,   setResults]   = useState(null);
  const [error,     setError]     = useState(null);
  const [sortBy,    setSortBy]    = useState("wr");
  const [filterEdge,setFilterEdge]= useState(false);
  const [indexCount,setIndexCount]= useState(null);
  const [livePerf,  setLivePerf]  = useState(null);
  const [liveDays,  setLiveDays]  = useState(14);
  const [liveTab,   setLiveTab]   = useState("backtest"); // "backtest" | "live"
  const [pcJobId,   setPcJobId]   = useState(null);
  const [pcStatus,  setPcStatus]  = useState(null);   // null | {status,n_done,n_total,elapsed,lines}
  const [pcRunning, setPcRunning] = useState(false);

  // Load live performance data on mount and when days changes
  useEffect(() => {
    fetch(`${API}/api/live_performance?days=${liveDays}`)
      .then(r => r.json())
      .then(d => setLivePerf(d))
      .catch(() => {});
  }, [liveDays]);

  const holdLabel = hold <= 4 ? "1h scalp" : hold <= 8 ? "2h" : hold <= 16 ? "4h" : "Full day";
  const selectedUni = UNIVERSES.find(u => u.key === universe) || UNIVERSES[0];

  // Fetch live ticker count when switching to an index universe
  const handleUniverseChange = async (key) => {
    setUniverse(key);
    setIndexCount(null);
    if (["dj30", "nasdaq100", "sp500", "cream"].includes(key)) {
      try {
        const r = await fetch(`${API}/api/universe/${key}`);
        const d = await r.json();
        if (d.count) setIndexCount(d.count);
      } catch {}
    }
  };

  const precache = async () => {
    setPcRunning(true);
    setPcStatus(null);
    try {
      let symParam = "default";
      if (universe === "custom") {
        symParam = customSym.split(",").map(s => s.trim().toUpperCase()).filter(Boolean).join(",");
      } else if (["dj30", "nasdaq100", "sp500", "cream"].includes(universe)) {
        const r = await fetch(`${API}/api/universe/${universe}`);
        const d = await r.json();
        if (d.error) { setPcStatus({ status: "error", lines: [d.error] }); setPcRunning(false); return; }
        symParam = d.tickers.join(",");
      }

      const startR = await fetch(`${API}/api/precache/run?symbols=${symParam}`);
      const startD = await startR.json();
      if (startD.error) { setPcStatus({ status: "error", lines: [startD.error] }); setPcRunning(false); return; }

      const jobId = startD.job_id;
      setPcJobId(jobId);

      await new Promise((resolve) => {
        const poll = setInterval(async () => {
          try {
            const sr = await fetch(`${API}/api/precache/status/${jobId}`);
            const sd = await sr.json();
            setPcStatus(sd);
            if (sd.status === "done" || sd.status === "error") {
              clearInterval(poll);
              resolve();
            }
          } catch { clearInterval(poll); resolve(); }
        }, 2000);
      });
    } catch (e) {
      setPcStatus({ status: "error", lines: [e.message] });
    } finally {
      setPcRunning(false);
    }
  };

  const run = async () => {
    setRunning(true);
    setError(null);
    setResults(null);
    setElapsed(0);
    const t0 = Date.now();
    const tick = setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 1000);

    try {
      let symParam = "default";
      if (universe === "custom") {
        symParam = customSym.split(",").map(s => s.trim().toUpperCase()).filter(Boolean).join(",");
      } else if (["dj30", "nasdaq100", "sp500", "cream"].includes(universe)) {
        const r = await fetch(`${API}/api/universe/${universe}`);
        const d = await r.json();
        if (d.error) { setError(d.error); clearInterval(tick); setRunning(false); return; }
        symParam = d.tickers.join(",");
      }

      // Start async job
      const startR = await fetch(`${API}/api/backtest/run?threshold=${threshold}&hold=${hold}&symbols=${symParam}&puts=${enablePuts}`);
      const startD = await startR.json();
      if (startD.error) { setError(startD.error); clearInterval(tick); setRunning(false); return; }

      const jobId = startD.job_id;

      // Poll every 3 seconds until done
      await new Promise((resolve, reject) => {
        const poll = setInterval(async () => {
          try {
            const sr = await fetch(`${API}/api/backtest/status/${jobId}`);
            const sd = await sr.json();
            if (sd.status === "done") {
              clearInterval(poll);
              setResults(sd.result);
              resolve();
            } else if (sd.status === "error") {
              clearInterval(poll);
              setError(sd.error || "Backtest failed");
              resolve();
            }
            // still running — keep polling
          } catch (e) {
            clearInterval(poll);
            reject(e);
          }
        }, 3000);
      });

    } catch (e) {
      setError(e.message);
    } finally {
      clearInterval(tick);
      setRunning(false);
    }
  };

  const oos     = results?.oos || {};
  const is_     = results?.is  || {};
  const bySymbol= results?.by_symbol || [];
  const byGrade = results?.by_grade  || {};
  const byDir   = results?.by_direction || {};
  const meta    = results?.meta || {};

  let rows = [...bySymbol];
  if (filterEdge) rows = rows.filter(r => r.wr >= 55 && r.pf >= 1.0);
  rows.sort((a, b) =>
    sortBy === "wr"     ? b.wr - a.wr :
    sortBy === "pf"     ? b.pf - a.pf :
    sortBy === "sharpe" ? b.sharpe - a.sharpe :
    sortBy === "n"      ? b.n - a.n :
    b.avg_pnl - a.avg_pnl
  );

  const verdict = oos.pf >= 1.3 && oos.sharpe >= 1.5
    ? { text: "PASS — edge confirmed OOS", color: C.call }
    : oos.pf >= 1.0 && oos.sharpe >= 0
    ? { text: "BORDERLINE — grow sample", color: C.gold }
    : oos.n > 0
    ? { text: "FAIL — no confirmed OOS edge", color: C.put }
    : null;

  const col = {
    padding: "8px 12px", textAlign: "left",
    borderBottom: `1px solid ${C.border}`,
    fontSize: 11, color: C.muted, fontWeight: 600,
  };
  const cell = {
    padding: "8px 12px", borderBottom: `1px solid ${C.border}55`,
    fontSize: 12, color: C.text,
  };

  const liveSymbols = livePerf?.symbols || [];
  const liveKeep    = liveSymbols.filter(s => s.verdict === "KEEP");
  const liveWatch   = liveSymbols.filter(s => s.verdict === "WATCH");
  const liveRemove  = liveSymbols.filter(s => s.verdict === "REMOVE");
  const liveTrades  = livePerf?.total_trades || 0;

  return (
    <div style={{ background: C.bg, minHeight: "100%", padding: 20, fontFamily: "system-ui, sans-serif" }}>

      {/* Header + tab switcher */}
      <div style={{ marginBottom: 20, display: "flex", alignItems: "flex-start", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
        <div>
          <h2 style={{ margin: 0, color: C.text, fontSize: 16, fontWeight: 700 }}>
            📊 Universe Backtest &amp; Pruning
          </h2>
          <div style={{ fontSize: 11, color: C.muted, marginTop: 4 }}>
            Walk-forward backtest (2yr IBKR bars) + live performance tracker for universe pruning
          </div>
        </div>
        <div style={{ display: "flex", border: `1px solid ${C.border}`, borderRadius: 6, overflow: "hidden" }}>
          {[["backtest","📊 Backtest"],["live","📈 Live Perf"]].map(([k,l]) => (
            <button key={k} onClick={() => setLiveTab(k)} style={{
              padding: "6px 16px", fontSize: 12, fontWeight: 600, cursor: "pointer",
              border: "none", borderRight: k === "backtest" ? `1px solid ${C.border}` : "none",
              background: liveTab === k ? C.accent + "22" : "transparent",
              color: liveTab === k ? C.accent : C.muted,
            }}>{l}</button>
          ))}
        </div>
      </div>

      {/* ── LIVE PERFORMANCE TAB ── */}
      {liveTab === "live" && (
        <div>
          {/* Header row */}
          <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 16, flexWrap: "wrap" }}>
            <div style={{ fontSize: 13, color: C.text, fontWeight: 700 }}>
              Live Signal Performance
            </div>
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <span style={{ fontSize: 11, color: C.muted }}>Look-back:</span>
              {[7, 14, 21, 30].map(d => (
                <button key={d} onClick={() => setLiveDays(d)} style={{
                  padding: "3px 10px", borderRadius: 4, fontSize: 11, fontWeight: 600,
                  border: `1px solid ${liveDays === d ? C.accent : C.border}`,
                  background: liveDays === d ? C.accent + "22" : "transparent",
                  color: liveDays === d ? C.accent : C.muted, cursor: "pointer",
                }}>{d}d</button>
              ))}
            </div>
            <div style={{ marginLeft: "auto", fontSize: 11, color: C.muted }}>
              {liveTrades} total trades · since {livePerf?.cutoff || "…"}
            </div>
          </div>

          {/* Verdict summary */}
          {liveSymbols.length > 0 && (
            <div style={{ display: "flex", gap: 12, marginBottom: 16, flexWrap: "wrap" }}>
              {[
                { label: "KEEP", items: liveKeep, color: C.call,
                  tip: "WR≥50% and PF≥1.0 — keep in universe" },
                { label: "WATCH", items: liveWatch, color: C.gold,
                  tip: "Close to threshold or too few trades" },
                { label: "REMOVE", items: liveRemove, color: C.put,
                  tip: "Below threshold — remove after 2 weeks" },
              ].map(({ label, items, color, tip }) => (
                <div key={label} title={tip} style={{
                  background: color + "11", border: `1px solid ${color}33`,
                  borderRadius: 8, padding: "10px 16px", minWidth: 160,
                }}>
                  <div style={{ fontSize: 10, color, fontWeight: 700, marginBottom: 6 }}>
                    {label} ({items.length})
                  </div>
                  <div style={{ fontSize: 11, color: C.muted, lineHeight: 1.6 }}>
                    {items.map(s => s.symbol).join(", ") || "—"}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Per-symbol table */}
          {liveSymbols.length === 0 ? (
            <div style={{ color: C.muted, fontSize: 13, padding: 20 }}>
              No closed trades in the last {liveDays} days yet.
              Run the scanner for a few days then come back here.
            </div>
          ) : (
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                <thead>
                  <tr>
                    {["Symbol","Trades","Win Rate","Avg P&L","Prof. Factor","Best","Worst","Verdict"].map(h => (
                      <th key={h} style={{ ...col, fontSize: 10 }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {liveSymbols.map((s, i) => {
                    const vc = s.verdict === "KEEP" ? C.call : s.verdict === "WATCH" ? C.gold : C.put;
                    return (
                      <tr key={s.symbol} style={{ background: i % 2 === 0 ? "transparent" : C.surface + "88" }}>
                        <td style={{ ...cell, fontWeight: 700, fontFamily: "monospace" }}>{s.symbol}</td>
                        <td style={cell}>{s.n}</td>
                        <td style={cell}>
                          <WrBar wr={s.wr} />
                        </td>
                        <td style={{ ...cell, color: s.avg_pnl >= 0 ? C.call : C.put, fontFamily: "monospace" }}>
                          {s.avg_pnl >= 0 ? "+" : ""}{s.avg_pnl.toFixed(2)}%
                        </td>
                        <td style={{ ...cell, color: s.pf >= 1 ? C.call : C.put, fontFamily: "monospace" }}>
                          {s.pf === 99 ? "∞" : s.pf.toFixed(2)}
                        </td>
                        <td style={{ ...cell, color: C.call, fontFamily: "monospace" }}>+{s.best.toFixed(1)}%</td>
                        <td style={{ ...cell, color: C.put,  fontFamily: "monospace" }}>{s.worst.toFixed(1)}%</td>
                        <td style={cell}>
                          <span style={{
                            padding: "2px 8px", borderRadius: 4, fontSize: 10, fontWeight: 700,
                            background: vc + "22", color: vc,
                          }}>{s.verdict}</span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {liveKeep.length > 0 && (
            <div style={{
              marginTop: 16, padding: "12px 16px", background: C.surface,
              border: `1px solid ${C.border}`, borderRadius: 8, fontSize: 11,
            }}>
              <div style={{ color: C.muted, marginBottom: 6, fontWeight: 700 }}>
                PRUNED UNIVERSE — copy into day_trading.py DEFAULT_SYMBOLS or run:
              </div>
              <code style={{ color: C.call, fontFamily: "monospace", fontSize: 11 }}>
                python3 scripts/prune_universe.py --days {liveDays} --apply
              </code>
              <div style={{ marginTop: 8, color: C.text, fontFamily: "monospace", fontSize: 11, wordBreak: "break-all" }}>
                [{liveKeep.map(s => `"${s.symbol}"`).join(", ")}]
              </div>
            </div>
          )}
        </div>
      )}

      {/* ── BACKTEST TAB ── */}
      {liveTab === "backtest" && <>

      {/* Controls */}
      <div style={{
        background: C.surface, border: `1px solid ${C.border}`,
        borderRadius: 10, padding: "16px 20px", marginBottom: 20,
        display: "flex", gap: 24, flexWrap: "wrap", alignItems: "flex-end",
      }}>

        {/* Symbol set */}
        <div>
          <div style={{ fontSize: 11, color: C.muted, marginBottom: 6, fontWeight: 600 }}>UNIVERSE</div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {UNIVERSES.map(u => {
              const isCream   = u.key === "cream";
              const isActive  = universe === u.key;
              const accentCol = isCream ? C.gold : C.accent;
              return (
                <button key={u.key} onClick={() => handleUniverseChange(u.key)} style={{
                  padding: "5px 14px", borderRadius: 4, fontSize: 11, fontWeight: 600,
                  border: `1px solid ${isActive ? accentCol : isCream ? C.gold + "55" : C.border}`,
                  background: isActive ? accentCol + "22" : "transparent",
                  color: isActive ? accentCol : isCream ? C.gold + "bb" : C.muted,
                  cursor: "pointer",
                }}>
                  {u.key === "default" ? u.label
                    : u.key === "custom" ? "Custom"
                    : `${u.label}${indexCount && isActive ? ` (${indexCount})` : ""}`}
                </button>
              );
            })}
          </div>
          {selectedUni.est && universe !== "custom" && (
            <div style={{ marginTop: 6, fontSize: 10, color: C.muted }}>
              Est. runtime: <span style={{ color: C.gold }}>{selectedUni.est}</span>
              {selectedUni.warn && <span style={{ color: C.put, marginLeft: 8 }}>⚠ {selectedUni.warn}</span>}
            </div>
          )}
          {universe === "custom" && (
            <input
              value={customSym}
              onChange={e => setCustomSym(e.target.value.toUpperCase())}
              placeholder="NVDA, AAPL, TSLA, …"
              style={{
                marginTop: 8, width: 260, padding: "5px 10px", borderRadius: 4,
                background: C.bg, border: `1px solid ${C.border}`, color: C.text,
                fontSize: 12, fontFamily: "monospace",
              }}
            />
          )}
        </div>

        {/* Threshold */}
        <div>
          <div style={{ fontSize: 11, color: C.muted, marginBottom: 6, fontWeight: 600 }}>
            THRESHOLD: <span style={{ color: C.text }}>{threshold}</span>
          </div>
          <input type="range" min={70} max={95} step={5} value={threshold}
            onChange={e => setThreshold(+e.target.value)}
            style={{ width: 140, accentColor: C.accent }}
          />
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: C.muted, marginTop: 2 }}>
            <span>70</span><span>75</span><span>80</span><span>85</span><span>90</span><span>95</span>
          </div>
        </div>

        {/* Hold */}
        <div>
          <div style={{ fontSize: 11, color: C.muted, marginBottom: 6, fontWeight: 600 }}>
            HOLD: <span style={{ color: C.text }}>{hold} bars ({holdLabel})</span>
          </div>
          <input type="range" min={1} max={26} step={1} value={hold}
            onChange={e => setHold(+e.target.value)}
            style={{ width: 140, accentColor: C.accent }}
          />
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 9, color: C.muted, marginTop: 2 }}>
            <span>15m</span><span>1h</span><span>2h</span><span>4h</span><span>6.5h</span>
          </div>
        </div>

        {/* Puts toggle */}
        <div>
          <div style={{ fontSize: 11, color: C.muted, marginBottom: 6, fontWeight: 600 }}>SIGNALS</div>
          <div style={{ display: "flex", border: `1px solid ${C.border}`, borderRadius: 4, overflow: "hidden" }}>
            <button onClick={() => setEnablePuts(true)} style={{
              padding: "5px 12px", fontSize: 11, fontWeight: 600, cursor: "pointer", border: "none",
              borderRight: `1px solid ${C.border}`,
              background: enablePuts ? C.accent + "22" : "transparent",
              color: enablePuts ? C.accent : C.muted,
            }}>CALL + PUT</button>
            <button onClick={() => setEnablePuts(false)} style={{
              padding: "5px 12px", fontSize: 11, fontWeight: 600, cursor: "pointer", border: "none",
              background: !enablePuts ? C.call + "22" : "transparent",
              color: !enablePuts ? C.call : C.muted,
            }}>CALL only</button>
          </div>
        </div>

        {/* Pre-cache + Run */}
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button onClick={precache} disabled={pcRunning || running} style={{
            padding: "10px 18px", borderRadius: 6, fontSize: 12, fontWeight: 700,
            background: pcRunning ? C.border : "#1a3a1a",
            border: `1px solid ${pcRunning ? C.muted : "#22c55e55"}`,
            color: pcRunning ? C.muted : C.call,
            cursor: pcRunning ? "not-allowed" : "pointer",
          }}>
            {pcRunning ? "⏳ Caching…" : "📥 Pre-cache Bars"}
          </button>
          <button onClick={run} disabled={running || pcRunning} style={{
            padding: "10px 28px", borderRadius: 6, fontSize: 13, fontWeight: 700,
            background: running ? C.border : C.accent,
            border: "none", color: running ? C.muted : "#fff",
            cursor: running ? "not-allowed" : "pointer",
          }}>
            {running ? "⏳ Running…" : "▶ Run Backtest"}
          </button>
          {running && (
            <div style={{ fontSize: 11, color: C.gold }}>
              ⏱ {elapsed}s — running backtest in background…
            </div>
          )}
        </div>

        {/* Pre-cache progress */}
        {(pcRunning || pcStatus) && (
          <div style={{
            background: "#0a1a0a", border: `1px solid #22c55e33`,
            borderRadius: 8, padding: "10px 14px", marginTop: 4,
            fontSize: 11, fontFamily: "monospace",
          }}>
            <div style={{ color: C.call, marginBottom: 6, fontWeight: 700 }}>
              {pcRunning ? `📥 Caching bars… ${pcStatus?.n_done || 0}/${pcStatus?.n_total || "?"} symbols  (${pcStatus?.elapsed || 0}s)` : pcStatus?.status === "done" ? `✓ Cache complete — ${pcStatus?.n_done}/${pcStatus?.n_total} symbols ready  (${pcStatus?.elapsed}s)  — now run backtest` : `⚠ Cache error`}
            </div>
            {pcStatus?.n_total > 0 && (
              <div style={{ height: 4, background: C.border, borderRadius: 2, marginBottom: 8, overflow: "hidden" }}>
                <div style={{ height: "100%", borderRadius: 2, background: pcStatus?.status === "done" ? C.call : C.accent, width: `${Math.round(((pcStatus?.n_done || 0) / pcStatus?.n_total) * 100)}%`, transition: "width 0.4s" }} />
              </div>
            )}
            <div style={{ color: C.muted, maxHeight: 100, overflowY: "auto", whiteSpace: "pre-wrap" }}>
              {(pcStatus?.lines || []).slice(-12).join("\n")}
            </div>
          </div>
        )}
      </div>

      {/* Error */}
      {error && (
        <div style={{
          background: C.put + "11", border: `1px solid ${C.put}33`,
          borderRadius: 8, padding: "12px 16px", marginBottom: 16,
          fontSize: 12, color: C.put,
        }}>⚠ {error}</div>
      )}

      {/* Results */}
      {results && (
        <>
          {/* Verdict */}
          {verdict && (
            <div style={{
              background: verdict.color + "11", border: `1px solid ${verdict.color}33`,
              borderRadius: 8, padding: "12px 20px", marginBottom: 16,
              fontSize: 13, fontWeight: 700, color: verdict.color,
            }}>
              {verdict.text}
              <span style={{ fontSize: 11, fontWeight: 400, color: C.muted, marginLeft: 16 }}>
                {meta.date_range} · {meta.symbols} symbols · threshold {meta.threshold} · {meta.elapsed_s}s
              </span>
            </div>
          )}

          {/* Top stat boxes */}
          <div style={{ display: "flex", gap: 10, marginBottom: 20, flexWrap: "wrap" }}>
            <StatBox label="OOS Win Rate"    value={oos.win_rate != null ? `${oos.win_rate.toFixed(1)}%` : "—"}
              color={oos.win_rate >= 55 ? C.call : oos.win_rate >= 45 ? C.gold : C.put}
              sub={`n=${oos.n || 0} trades`} />
            <StatBox label="Profit Factor"  value={oos.pf != null ? oos.pf.toFixed(2) : "—"}
              color={oos.pf >= 1.3 ? C.call : oos.pf >= 1.0 ? C.gold : C.put} />
            <StatBox label="Avg P&L / trade" value={oos.avg_pnl != null ? `${oos.avg_pnl > 0 ? "+" : ""}${oos.avg_pnl.toFixed(3)}%` : "—"}
              color={oos.avg_pnl > 0 ? C.call : C.put} />
            <StatBox label="Sharpe"         value={oos.sharpe != null ? oos.sharpe.toFixed(2) : "—"}
              color={oos.sharpe >= 1.5 ? C.call : oos.sharpe >= 0 ? C.gold : C.put} />
            <StatBox label="Max Drawdown"   value={oos.max_dd != null ? `-${oos.max_dd.toFixed(1)}%` : "—"}
              color={C.put} />
            <StatBox label="IS Win Rate"    value={is_.win_rate != null ? `${is_.win_rate.toFixed(1)}%` : "—"}
              color={C.muted} sub="in-sample" />
          </div>

          {/* By Grade + Direction */}
          <div style={{ display: "flex", gap: 16, marginBottom: 20, flexWrap: "wrap" }}>
            {["S","A","B","C"].map(g => {
              const s = byGrade[g] || {};
              return s.n > 0 ? (
                <div key={g} style={{
                  background: C.surface, border: `1px solid ${C.border}`,
                  borderRadius: 8, padding: "10px 16px", minWidth: 130,
                }}>
                  <div style={{ fontSize: 10, color: C.muted, fontWeight: 700, marginBottom: 6 }}>GRADE {g}</div>
                  <div style={{ fontSize: 14, fontWeight: 700, fontFamily: "monospace",
                    color: s.win_rate >= 55 ? C.call : s.win_rate >= 45 ? C.gold : C.put }}>
                    {s.win_rate?.toFixed(1)}%
                  </div>
                  <div style={{ fontSize: 11, color: C.muted, marginTop: 2 }}>
                    n={s.n} · PF {s.pf?.toFixed(2)} · avg {s.avg_pnl > 0 ? "+" : ""}{s.avg_pnl?.toFixed(2)}%
                  </div>
                </div>
              ) : null;
            })}
            {["CALL","PUT"].map(d => {
              const s = byDir[d] || {};
              return s.n > 0 ? (
                <div key={d} style={{
                  background: C.surface, border: `1px solid ${C.border}`,
                  borderRadius: 8, padding: "10px 16px", minWidth: 130,
                }}>
                  <div style={{ fontSize: 10,
                    color: d === "CALL" ? C.call : C.put, fontWeight: 700, marginBottom: 6 }}>
                    {d === "CALL" ? "▲" : "▼"} {d}
                  </div>
                  <div style={{ fontSize: 14, fontWeight: 700, fontFamily: "monospace",
                    color: s.win_rate >= 55 ? C.call : s.win_rate >= 45 ? C.gold : C.put }}>
                    {s.win_rate?.toFixed(1)}%
                  </div>
                  <div style={{ fontSize: 11, color: C.muted, marginTop: 2 }}>
                    n={s.n} · PF {s.pf?.toFixed(2)} · avg {s.avg_pnl > 0 ? "+" : ""}{s.avg_pnl?.toFixed(2)}%
                  </div>
                </div>
              ) : null;
            })}
          </div>

          {/* Per-symbol table */}
          <div style={{ marginBottom: 10, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <span style={{ fontSize: 12, color: C.muted, fontWeight: 600 }}>SORT BY</span>
            {[["wr","Win Rate"],["pf","Profit Factor"],["sharpe","Sharpe"],["avg_pnl","Avg P&L"],["n","Trades"]].map(([k, l]) => (
              <button key={k} onClick={() => setSortBy(k)} style={{
                padding: "3px 10px", borderRadius: 4, fontSize: 11, fontWeight: 600,
                border: `1px solid ${sortBy === k ? C.accent : C.border}`,
                background: sortBy === k ? C.accent + "22" : "transparent",
                color: sortBy === k ? C.accent : C.muted, cursor: "pointer",
              }}>{l}</button>
            ))}
            <label style={{ marginLeft: 12, fontSize: 11, color: C.muted, cursor: "pointer", display: "flex", alignItems: "center", gap: 5 }}>
              <input type="checkbox" checked={filterEdge} onChange={e => setFilterEdge(e.target.checked)} />
              Edge only (WR≥55% + PF≥1.0)
            </label>
            <span style={{ marginLeft: "auto", fontSize: 11, color: C.muted }}>
              {rows.length} symbols
            </span>
          </div>

          <div style={{ overflowX: "auto", borderRadius: 8, border: `1px solid ${C.border}` }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ background: C.surface }}>
                  <th style={col}>Symbol</th>
                  <th style={col}>OOS WR</th>
                  <th style={col}>Trades</th>
                  <th style={col}>Profit Factor</th>
                  <th style={col}>Avg P&L</th>
                  <th style={col}>Sharpe</th>
                  <th style={col}>Max DD</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => {
                  const edgeColor = r.wr >= 55 && r.pf >= 1.0 ? C.call : r.wr >= 45 ? C.gold : C.put;
                  return (
                    <tr key={r.symbol} style={{ background: i % 2 === 0 ? "transparent" : C.surface + "88" }}>
                      <td style={{ ...cell, fontWeight: 700, fontFamily: "monospace", color: C.text }}>{r.symbol}</td>
                      <td style={cell}><WrBar wr={r.wr} /></td>
                      <td style={{ ...cell, fontFamily: "monospace", color: C.muted }}>{r.n}</td>
                      <td style={{ ...cell, fontFamily: "monospace",
                        color: r.pf >= 1.3 ? C.call : r.pf >= 1.0 ? C.gold : C.put }}>
                        {r.pf.toFixed(2)}
                      </td>
                      <td style={{ ...cell, fontFamily: "monospace",
                        color: r.avg_pnl > 0 ? C.call : C.put }}>
                        {r.avg_pnl > 0 ? "+" : ""}{r.avg_pnl.toFixed(3)}%
                      </td>
                      <td style={{ ...cell, fontFamily: "monospace",
                        color: r.sharpe >= 1.5 ? C.call : r.sharpe >= 0 ? C.gold : C.put }}>
                        {r.sharpe.toFixed(2)}
                      </td>
                      <td style={{ ...cell, fontFamily: "monospace", color: C.put }}>
                        -{r.max_dd.toFixed(1)}%
                      </td>
                    </tr>
                  );
                })}
                {rows.length === 0 && (
                  <tr><td colSpan={7} style={{ ...cell, textAlign: "center", color: C.muted, padding: 40 }}>
                    No symbols match filter
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}

      {!results && !running && !error && (
        <div style={{ color: C.muted, textAlign: "center", padding: 60 }}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>📊</div>
          <div style={{ fontSize: 14, marginBottom: 6 }}>Configure and run backtest above</div>
          <div style={{ fontSize: 12 }}>
            Uses cached 15-min bars — fast if bars already downloaded. First run per symbol fetches from IBKR (~5s/symbol).
          </div>
        </div>
      )}

      </>}

    </div>
  );
}
