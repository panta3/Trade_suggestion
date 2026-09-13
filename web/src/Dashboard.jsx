import { useState, useEffect, useRef, useCallback } from "react";
import ChartPanel from "./ChartPanel";
import "./dashboard.css";

const API = "http://localhost:5000";
const REFRESH_SEC = 60;

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtCountdown(s) {
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `Next: ${m}:${String(sec).padStart(2, "0")}`;
}

// ── Sub-components ────────────────────────────────────────────────────────────

function ScoreBar({ score, direction }) {
  return (
    <div className="score-bar-wrap">
      <div className="score-bar">
        <div
          className={`score-bar-fill${direction === "PUT" ? " put" : ""}`}
          style={{ width: `${score}%` }}
        />
      </div>
      <span>{score.toFixed(0)}%</span>
    </div>
  );
}

function StatusBar({ status, marketOpen }) {
  if (status.type === "scanning") {
    return (
      <div className="status-bar">
        <span className="spinner" /> Fetching latest signals…
      </div>
    );
  }
  if (status.type === "error") {
    return (
      <div className="status-bar" style={{ color: "var(--red)" }}>
        ⚠ {status.msg}
      </div>
    );
  }
  const closed = marketOpen === false;
  return (
    <div className="status-bar">
      {status.count > 0
        ? `${status.count} signal${status.count !== 1 ? "s" : ""} above threshold`
        : closed
          ? <span>Market closed — use <strong>Force Scan</strong> to test, or wait for 9:35 ET</span>
          : "No signals this scan — scanner running in background"}
    </div>
  );
}

function SignalRow({ sig, isSelected, onClick }) {
  const isCall = sig.direction === "CALL";
  return (
    <tr className={isSelected ? "selected" : ""} onClick={onClick}>
      <td><span className="sym">{sig.symbol}</span></td>
      <td>
        <span className={isCall ? "dir-call" : "dir-put"}>
          {isCall ? "▲" : "▼"} {sig.direction}
        </span>
      </td>
      <td><span className={`grade-${sig.grade}`}>{sig.grade}</span></td>
      <td><ScoreBar score={sig.score} direction={sig.direction} /></td>
      <td className="price">${sig.entry.toFixed(2)}</td>
      <td className="stop-val">${sig.stop.toFixed(2)}</td>
      <td className="target-val">${sig.target.toFixed(2)}</td>
      <td className="rr-val">{sig.rr.toFixed(1)}R</td>
      <td>{sig.rsi    != null ? sig.rsi.toFixed(0)           : "—"}</td>
      <td>{sig.rel_vol != null ? sig.rel_vol.toFixed(1) + "x" : "—"}</td>
      <td>{sig.adx    != null ? sig.adx.toFixed(0)           : "—"}</td>
      <td className="reason-cell">{(sig.reasons || []).slice(0, 2).join(" · ")}</td>
      <td className="time-cell">{sig.time || ""}</td>
    </tr>
  );
}

// ── Search bar ────────────────────────────────────────────────────────────────
function SearchBar({ onSearch }) {
  const [query, setQuery] = useState("");
  const inputRef = useRef(null);

  const submit = () => {
    const sym = query.trim().toUpperCase();
    if (sym) { onSearch(sym); setQuery(""); }
  };

  return (
    <div className="search-bar">
      <input
        ref={inputRef}
        className="search-input"
        value={query}
        onChange={e => setQuery(e.target.value)}
        onKeyDown={e => e.key === "Enter" && submit()}
        placeholder="Search symbol…"
        spellCheck={false}
      />
      <button className="search-btn" onClick={submit} title="Open chart">
        🔍
      </button>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function Dashboard() {
  const [signals,    setSignals]    = useState([]);
  const [regime,     setRegime]     = useState("Loading…");
  const [scanTime,   setScanTime]   = useState(null);
  const [scanning,   setScanning]   = useState(false);
  const [marketOpen, setMarketOpen] = useState(null);
  const [status,     setStatus]     = useState({ type: "scanning" });
  const [countdown,  setCountdown]  = useState(null);
  const [forceBusy,  setForceBusy]  = useState(false);
  const [paused,     setPaused]     = useState(false);

  // Chart state: which symbol is shown + which signal (if any) drives price lines
  const [chartSymbol, setChartSymbol] = useState(null);
  const [chartSig,    setChartSig]    = useState(null);
  const [selectedIdx, setSelectedIdx] = useState(null);

  const timerRef       = useRef(null);
  const fetchRef       = useRef(null);
  const pausedRef      = useRef(false);
  const prevKeysRef    = useRef(new Set());
  const isFirstLoadRef = useRef(true);

  // Request notification permission once on mount
  useEffect(() => {
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission();
    }
  }, []);

  // ── Data fetching ─────────────────────────────────────────────────────────
  const fetchSignals = useCallback(async () => {
    setStatus({ type: "scanning" });
    try {
      const r = await fetch(`${API}/api/intraday_scan`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      if (data.error) { setStatus({ type: "error", msg: data.error }); return; }

      const newSigs = data.signals || [];
      const newKeys = new Set(newSigs.map(s => `${s.symbol}:${s.direction}:${s.time}`));

      // Fire browser notifications for genuinely new signals (skip first load)
      if (!isFirstLoadRef.current && "Notification" in window && Notification.permission === "granted") {
        for (const sig of newSigs) {
          const key = `${sig.symbol}:${sig.direction}:${sig.time}`;
          if (!prevKeysRef.current.has(key)) {
            new Notification(
              `${sig.direction === "CALL" ? "▲" : "▼"} ${sig.symbol} — ${sig.direction}`,
              {
                body: `Grade ${sig.grade} · ${sig.score.toFixed(0)}% · Entry $${sig.entry.toFixed(2)}`,
                tag:  key,
              }
            );
          }
        }
      }
      prevKeysRef.current = newKeys;
      isFirstLoadRef.current = false;

      setRegime(data.regime || "unknown");
      if (data.scanned_at) setScanTime(new Date(data.scanned_at));
      setSignals(newSigs);
      setMarketOpen(data.market_open ?? null);
      setScanning(!!data.scanning);
      setStatus({ type: "ok", count: newSigs.length, marketOpen: data.market_open });
    } catch (e) {
      setStatus({ type: "error", msg: e.message + " — is api.py running? (python3 api.py)" });
    }
  }, []);

  useEffect(() => { fetchRef.current = fetchSignals; }, [fetchSignals]);

  const startCountdown = useCallback(() => {
    if (pausedRef.current) return;
    clearInterval(timerRef.current);
    let remaining = REFRESH_SEC;
    setCountdown(remaining);
    timerRef.current = setInterval(() => {
      if (pausedRef.current) { clearInterval(timerRef.current); setCountdown(null); return; }
      remaining -= 1;
      setCountdown(remaining);
      if (remaining <= 0) {
        clearInterval(timerRef.current);
        setCountdown(null);
        fetchRef.current?.().then(() => startCountdown());
      }
    }, 1000);
  }, []);

  const triggerRefresh = useCallback(() => {
    clearInterval(timerRef.current);
    setCountdown(null);
    fetchSignals().then(() => startCountdown());
  }, [fetchSignals, startCountdown]);

  useEffect(() => {
    fetchSignals().then(() => startCountdown());
    return () => clearInterval(timerRef.current);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // SSE — instant refresh the moment a new signal fires, no waiting for the poll cycle
  useEffect(() => {
    const es = new EventSource(`${API}/api/events`);
    es.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === "new_signal" && !pausedRef.current) {
          clearInterval(timerRef.current);
          fetchRef.current?.().then(() => startCountdown());
        }
      } catch {}
    };
    return () => es.close();
  }, [startCountdown]);

  const togglePause = () => {
    const next = !pausedRef.current;
    pausedRef.current = next;
    setPaused(next);
    if (next) {
      clearInterval(timerRef.current);
      setCountdown(null);
    } else {
      fetchSignals().then(() => startCountdown());
    }
  };

  const forceScan = async () => {
    setForceBusy(true);
    try {
      await fetch(`${API}/api/intraday_force_scan`, { method: "POST" });
      setTimeout(() => { setForceBusy(false); triggerRefresh(); }, 8000);
    } catch (e) {
      setForceBusy(false);
      setStatus({ type: "error", msg: "Force scan failed: " + e.message });
    }
  };

  // ── Row click — opens chart with signal price lines ───────────────────────
  const handleRowClick = (i) => {
    if (i === selectedIdx) {
      // Second click on same row closes panel
      setSelectedIdx(null);
      setChartSymbol(null);
      setChartSig(null);
    } else {
      const sig = signals[i];
      setSelectedIdx(i);
      setChartSymbol(sig.symbol);
      setChartSig(sig);
    }
  };

  // ── Search — opens chart without signal lines ─────────────────────────────
  const handleSearch = (sym) => {
    // If there's a matching signal in the table, use its levels
    const match = signals.find(s => s.symbol === sym);
    setSelectedIdx(match ? signals.indexOf(match) : null);
    setChartSymbol(sym);
    setChartSig(match || null);
  };

  const closeChart = () => {
    setChartSymbol(null);
    setChartSig(null);
    setSelectedIdx(null);
  };

  const regimeClass = regime.toLowerCase().includes("bull") ? "bull"
    : regime.toLowerCase().includes("bear") ? "bear"
    : regime.toLowerCase().includes("neutral") ? "neutral" : "neutral";

  return (
    <div className="dashboard">
      {/* ── Header ── */}
      <div className="dash-header">
        <h1>Day Trading Scanner</h1>
        <span className={`regime-badge ${regimeClass}`}>{regime}</span>
        <div className="scan-info">
          {scanning && <span><span className="spinner" />Scanning…</span>}
          {scanTime && (
            <span>
              Last:{" "}
              {scanTime.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })}
            </span>
          )}
          {countdown != null && !paused && <span className="countdown">{fmtCountdown(countdown)}</span>}
          {paused && <span className="countdown" style={{ color: "var(--red)" }}>⏹ Paused</span>}
          {marketOpen === false && <span className="market-badge closed">Market Closed</span>}
          {marketOpen === true  && <span className="market-badge open">● Market Open</span>}
          <button className="refresh-btn" onClick={triggerRefresh}>Refresh</button>
          <button
            className={`force-btn${paused ? " paused" : ""}`}
            onClick={togglePause}
            title={paused ? "Resume auto-refresh" : "Stop auto-refresh"}
          >
            {paused ? "▶ Resume" : "⏹ Stop"}
          </button>
          <button
            className="force-btn"
            disabled={forceBusy}
            onClick={forceScan}
            title="Force a scan now (bypasses market-hours guard)"
          >
            {forceBusy ? "Scanning…" : "Force Scan"}
          </button>
        </div>
        <SearchBar onSearch={handleSearch} />
      </div>

      {/* ── Body ── */}
      <div className="dash-body">

        {/* Signal table */}
        <div className="table-panel">
          <StatusBar status={status} marketOpen={marketOpen} />

          {signals.length === 0 && status.type !== "scanning" ? (
            <div className="empty-state">
              <div className="empty-icon">🔍</div>
              <h3>No signals above threshold</h3>
              <p>
                Scanner runs every 5 minutes during active hours
                (9:35–11:00 ET, 1:00–3:45 ET).
                Use <strong>Search</strong> to pull up any chart.
              </p>
            </div>
          ) : (
            <table className="signal-table">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Direction</th>
                  <th>Grade</th>
                  <th>Score</th>
                  <th>Entry</th>
                  <th>Stop</th>
                  <th>Target</th>
                  <th>R:R</th>
                  <th>RSI</th>
                  <th>R.Vol</th>
                  <th>ADX</th>
                  <th>Reasons</th>
                  <th>Time</th>
                </tr>
              </thead>
              <tbody>
                {signals.map((sig, i) => (
                  <SignalRow
                    key={`${sig.symbol}-${sig.time || i}`}
                    sig={sig}
                    isSelected={i === selectedIdx}
                    onClick={() => handleRowClick(i)}
                  />
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Chart panel — replaces old side panel */}
        {chartSymbol && (
          <ChartPanel
            key={chartSymbol}
            symbol={chartSymbol}
            sig={chartSig}
            onClose={closeChart}
          />
        )}
      </div>
    </div>
  );
}
