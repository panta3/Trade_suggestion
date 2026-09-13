import { useState, useEffect } from "react";

const API = "http://localhost:5000";

const C = {
  bg: "#000", surface: "#080808", border: "#141414",
  text: "#e0e0e0", muted: "#444", accent: "#3b82f6",
  call: "#22c55e", put: "#ef4444", gold: "#f59e0b",
};

function Stars({ n }) {
  return (
    <span style={{ color: C.gold, fontSize: 14, letterSpacing: -1 }}>
      {"★".repeat(n)}{"☆".repeat(3 - n)}
    </span>
  );
}

function GapBar({ pct, max }) {
  const w   = Math.min(Math.abs(pct) / Math.max(max, 0.1) * 100, 100);
  const col = pct >= 0 ? C.call : C.put;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <div style={{ width: 80, height: 5, background: "#111", borderRadius: 3, overflow: "hidden" }}>
        <div style={{ width: `${w}%`, height: "100%", background: col, borderRadius: 3 }} />
      </div>
      <span style={{
        fontSize: 12, fontFamily: "monospace", fontWeight: 700,
        color: col, minWidth: 52,
      }}>
        {pct >= 0 ? "+" : ""}{pct.toFixed(2)}%
      </span>
    </div>
  );
}

function VolBar({ ratio, max }) {
  const w = Math.min(ratio / Math.max(max, 0.1) * 100, 100);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <div style={{ width: 60, height: 5, background: "#111", borderRadius: 3, overflow: "hidden" }}>
        <div style={{ width: `${w}%`, height: "100%", background: C.accent, borderRadius: 3 }} />
      </div>
      <span style={{ fontSize: 12, fontFamily: "monospace", color: "#888", minWidth: 40 }}>
        {ratio.toFixed(1)}×
      </span>
    </div>
  );
}

export default function PremarketScan({ onViewInChart }) {
  const [running,  setRunning]  = useState(false);
  const [elapsed,  setElapsed]  = useState(0);
  const [results,  setResults]  = useState(null);
  const [error,    setError]    = useState(null);
  const [minGap,   setMinGap]   = useState(1.0);
  const [minVol,   setMinVol]   = useState(1.0);
  const [scannedAt,setScannedAt]= useState(null);

  const run = async () => {
    setRunning(true);
    setError(null);
    setResults(null);
    setElapsed(0);
    const t0   = Date.now();
    const tick = setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 1000);

    try {
      const sr = await fetch(`${API}/api/premarket_scan/run?min_gap=${minGap}&min_vol=${minVol}`);
      const sd = await sr.json();
      if (sd.error) { setError(sd.error); return; }

      const jobId = sd.job_id;
      await new Promise((resolve) => {
        const poll = setInterval(async () => {
          try {
            const pr = await fetch(`${API}/api/premarket_scan/status/${jobId}`);
            const pd = await pr.json();
            if (pd.status === "done") {
              clearInterval(poll);
              setResults(pd.result || []);
              setScannedAt(new Date().toLocaleTimeString("en-US", {
                hour: "2-digit", minute: "2-digit", timeZone: "America/New_York",
              }));
              resolve();
            } else if (pd.status === "error") {
              clearInterval(poll);
              setError(pd.error || "Scan failed");
              resolve();
            }
          } catch { clearInterval(poll); resolve(); }
        }, 2000);
      });
    } catch (e) {
      setError(e.message);
    } finally {
      clearInterval(tick);
      setRunning(false);
    }
  };

  const maxGap = results ? Math.max(...results.map(r => Math.abs(r.gap_pct)), 0.1) : 1;
  const maxVol = results ? Math.max(...results.map(r => r.vol_ratio), 0.1) : 1;

  const priority = (i) => i < 3 ? 3 : i < 6 ? 2 : 1;

  return (
    <div style={{
      background: C.bg, minHeight: "100%", padding: 24,
      fontFamily: "system-ui, sans-serif", color: C.text,
    }}>

      {/* Header */}
      <div style={{ marginBottom: 20 }}>
        <h2 style={{ margin: 0, fontSize: 17, fontWeight: 700, color: C.text }}>
          🌅 Pre-market Scan
        </h2>
        <div style={{ fontSize: 11, color: C.muted, marginTop: 4 }}>
          Run at 9:00–9:20 AM ET — finds stocks with gap + volume catalyst before open
        </div>
      </div>

      {/* Controls */}
      <div style={{
        display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap",
        marginBottom: 20, padding: "14px 16px",
        background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8,
      }}>
        <div>
          <div style={{ fontSize: 10, color: C.muted, marginBottom: 4 }}>MIN GAP %</div>
          <input
            type="number" step="0.5" min="0" max="20" value={minGap}
            onChange={e => setMinGap(+e.target.value)}
            style={{
              width: 70, padding: "6px 10px", background: "#0a0a0a",
              border: `1px solid ${C.border}`, color: C.text,
              fontFamily: "monospace", fontSize: 13, borderRadius: 4,
            }}
          />
        </div>
        <div>
          <div style={{ fontSize: 10, color: C.muted, marginBottom: 4 }}>MIN VOL RATIO</div>
          <input
            type="number" step="0.1" min="0" max="10" value={minVol}
            onChange={e => setMinVol(+e.target.value)}
            style={{
              width: 70, padding: "6px 10px", background: "#0a0a0a",
              border: `1px solid ${C.border}`, color: C.text,
              fontFamily: "monospace", fontSize: 13, borderRadius: 4,
            }}
          />
        </div>
        <button onClick={run} disabled={running} style={{
          padding: "8px 28px", borderRadius: 6, fontSize: 13, fontWeight: 700,
          background: running ? C.border : "#0d2d0d",
          border: `1px solid ${running ? C.muted : C.call + "66"}`,
          color: running ? C.muted : C.call,
          cursor: running ? "not-allowed" : "pointer",
        }}>
          {running ? `⏳ Scanning… ${elapsed}s` : "▶ Run Scan"}
        </button>

        {scannedAt && !running && (
          <div style={{ fontSize: 11, color: C.muted, alignSelf: "center" }}>
            Last scan: {scannedAt} ET
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

      {/* No results */}
      {results && results.length === 0 && (
        <div style={{
          background: C.surface, border: `1px solid ${C.border}`,
          borderRadius: 8, padding: "24px", textAlign: "center",
          fontSize: 13, color: C.muted,
        }}>
          No stocks met the gap/volume criteria — market may be quiet today.
          <br />Try lowering Min Gap % or Min Vol Ratio.
        </div>
      )}

      {/* Results table */}
      {results && results.length > 0 && (
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8, overflow: "hidden" }}>

          {/* Summary */}
          <div style={{
            padding: "10px 16px", borderBottom: `1px solid ${C.border}`,
            display: "flex", gap: 16, alignItems: "center",
          }}>
            <span style={{ fontSize: 12, color: C.text, fontWeight: 600 }}>
              {results.length} stocks in play today
            </span>
            <span style={{ fontSize: 11, color: C.muted }}>
              Click any row to load the chart
            </span>
          </div>

          {/* Column headers */}
          <div style={{
            display: "grid",
            gridTemplateColumns: "44px 80px 160px 130px 80px 100px",
            padding: "8px 16px",
            borderBottom: `1px solid ${C.border}`,
            fontSize: 10, color: C.muted, fontWeight: 600, letterSpacing: 0.5,
          }}>
            <span>#</span>
            <span>SYMBOL</span>
            <span>GAP</span>
            <span>VOL RATIO</span>
            <span>PM PRICE</span>
            <span>PRIORITY</span>
          </div>

          {/* Rows */}
          {results.map((r, i) => (
            <div
              key={r.symbol}
              onClick={() => onViewInChart(r.symbol)}
              style={{
                display: "grid",
                gridTemplateColumns: "44px 80px 160px 130px 80px 100px",
                padding: "11px 16px",
                borderBottom: `1px solid ${C.border}33`,
                cursor: "pointer", alignItems: "center",
                background: i < 3 ? "#0a0f0a" : "transparent",
                transition: "background 0.1s",
              }}
              onMouseEnter={e => e.currentTarget.style.background = "#0f160f"}
              onMouseLeave={e => e.currentTarget.style.background = i < 3 ? "#0a0f0a" : "transparent"}
            >
              <span style={{ fontSize: 11, color: C.muted, fontFamily: "monospace" }}>
                {i + 1}
              </span>
              <span style={{
                fontSize: 14, fontWeight: 700, fontFamily: "monospace",
                color: r.gap_pct >= 0 ? C.call : C.put,
              }}>
                {r.symbol}
              </span>
              <GapBar pct={r.gap_pct} max={maxGap} />
              <VolBar ratio={r.vol_ratio} max={maxVol} />
              <span style={{ fontSize: 12, fontFamily: "monospace", color: "#777" }}>
                {r.pm_price ? `$${r.pm_price.toFixed(2)}` : "—"}
              </span>
              <Stars n={priority(i)} />
            </div>
          ))}
        </div>
      )}

      {/* Instructions — shown before first scan */}
      {!results && !running && !error && (
        <div style={{
          background: C.surface, border: `1px solid ${C.border}`,
          borderRadius: 8, padding: "20px 24px",
          fontSize: 12, color: C.muted, lineHeight: 1.8,
        }}>
          <div style={{ color: C.text, fontWeight: 600, marginBottom: 10, fontSize: 13 }}>
            How to use this
          </div>
          <div>1. Run at <b style={{ color: C.text }}>9:00–9:20 AM ET</b> before market opens</div>
          <div>2. Top 3 (★★★) are your highest priority watchlist for the day</div>
          <div>3. Click any symbol → chart loads automatically</div>
          <div>4. When the algo fires a signal on a stock from this list → highest conviction trade</div>
          <div style={{ marginTop: 12, color: "#333" }}>
            Gap UP + high vol = earnings beat / upgrade — watch for CALL after 9:45 AM holding above PDH<br />
            Gap DOWN + high vol = earnings miss / shock — more dangerous, skip unless very clean setup
          </div>
        </div>
      )}
    </div>
  );
}
