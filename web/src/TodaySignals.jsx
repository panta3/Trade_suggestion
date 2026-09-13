import { useState, useEffect } from "react";

const API = "http://localhost:5000";

const C = {
  bg:      "#0d0d0d",
  surface: "#111",
  border:  "#222",
  text:    "#e8e8e8",
  muted:   "#555",
  accent:  "#3b82f6",
  call:    "#22c55e",
  put:     "#ef4444",
  gold:    "#f59e0b",
  purple:  "#a855f7",
};

const GRADE_COLOR = { S: C.gold, A: C.call, B: C.accent, C: C.muted };

function GradeBadge({ grade }) {
  const col = GRADE_COLOR[grade] || C.muted;
  return (
    <span style={{
      background: col + "22", color: col, border: `1px solid ${col}55`,
      borderRadius: 4, padding: "1px 7px", fontSize: 11, fontWeight: 700,
      letterSpacing: "0.05em", fontFamily: "monospace",
    }}>{grade}</span>
  );
}

function DirBadge({ dir }) {
  const call = dir === "CALL";
  return (
    <span style={{
      background: (call ? C.call : C.put) + "22",
      color: call ? C.call : C.put,
      border: `1px solid ${call ? C.call : C.put}55`,
      borderRadius: 4, padding: "1px 8px", fontSize: 11, fontWeight: 700,
      fontFamily: "monospace",
    }}>{call ? "▲ CALL" : "▼ PUT"}</span>
  );
}

function VolBadge({ rv }) {
  if (rv == null) return <span style={{ color: C.muted, fontSize: 11 }}>—</span>;
  const v = +rv;
  const color = v >= 2.0 ? C.gold : v >= 1.3 ? C.call : v >= 0.8 ? C.muted : C.put;
  const label = v >= 2.0 ? "HIGH" : v >= 1.3 ? "↑" : v < 0.8 ? "LOW" : null;
  return (
    <span style={{ color, fontFamily: "monospace", fontSize: 11, fontWeight: v >= 1.3 || v < 0.8 ? 700 : 400 }}>
      {v.toFixed(1)}x{label ? <span style={{ fontSize: 9, marginLeft: 3 }}>{label}</span> : null}
    </span>
  );
}

function LevelBadge({ levelBreak }) {
  if (!levelBreak) return <span style={{ color: C.muted, fontSize: 11 }}>—</span>;
  const cfg = {
    PDH:      { label: "PDH ↑",    color: C.call,   tip: "Broke above previous day high — strong CALL signal" },
    PMH:      { label: "PMH ↑",    color: C.call,   tip: "Broke above pre-market high" },
    PDL:      { label: "PDL ↓",    color: C.put,    tip: "Broke below previous day low — strong PUT signal" },
    PML:      { label: "PML ↓",    color: C.put,    tip: "Broke below pre-market low" },
    PDH_test: { label: "PDH test", color: C.gold,   tip: "Approaching previous day high from below — watch for breakout or rejection" },
    PDL_hold: { label: "PDL hold", color: C.accent, tip: "Bouncing off previous day low support" },
  };
  const c = cfg[levelBreak] || { label: levelBreak, color: C.muted };
  return (
    <span title={c.tip} style={{
      background: c.color + "22", color: c.color,
      border: `1px solid ${c.color}55`, borderRadius: 4,
      padding: "1px 7px", fontSize: 10, fontWeight: 700,
      fontFamily: "monospace", cursor: "default",
    }}>{c.label}</span>
  );
}

function ChloeBadge({ t }) {
  const isChloe   = t.chloe_setup;
  const hasPb     = t.level_pullback;
  const conf      = t.confluence?.length > 0;
  const dirBias   = t.direction === "CALL" ? "bull" : "bear";
  const biasOk    = t.weekly_bias === dirBias;
  const postEarn  = t.post_earnings_gap;

  return (
    <div style={{ display: "flex", gap: 3, marginTop: 3, flexWrap: "wrap" }}>

      {/* Full Chloe setup — most prominent */}
      {isChloe && (
        <span title={`Chloe entry: ${t.level_pullback_tag} · HTF agrees`} style={{
          background: "#eab30822", color: "#eab308",
          border: "1px solid #eab30866", borderRadius: 3,
          padding: "2px 8px", fontSize: 10, fontWeight: 800, letterSpacing: 0.5,
        }}>★ CHLOE</span>
      )}

      {/* Pullback present but HTF doesn't agree — partial setup */}
      {hasPb && !isChloe && (
        <span title={t.level_pullback_tag} style={{
          background: "#22c55e11", color: "#22c55e",
          border: "1px solid #22c55e33", borderRadius: 3,
          padding: "1px 6px", fontSize: 9, fontWeight: 700,
        }}>↩ PULLBACK</span>
      )}

      {conf && (
        <span title={(t.confluence || []).join(" · ")} style={{
          background: "#f97316" + "18", color: "#f97316",
          border: "1px solid #f9731633", borderRadius: 3,
          padding: "1px 6px", fontSize: 9, fontWeight: 700,
        }}>⚡ CONFLUENCE</span>
      )}

      {biasOk && !isChloe && (
        <span style={{
          background: "#3b82f611", color: "#3b82f6",
          border: "1px solid #3b82f633", borderRadius: 3,
          padding: "1px 6px", fontSize: 9, fontWeight: 700,
        }}>↑ HTF OK</span>
      )}

      {postEarn && (
        <span title="Stock gapped on earnings — this is a post-earnings gap pullback entry"
              style={{
          background: "#a855f711", color: "#a855f7",
          border: "1px solid #a855f733", borderRadius: 3,
          padding: "1px 6px", fontSize: 9, fontWeight: 700,
        }}>📰 EARNINGS GAP</span>
      )}
    </div>
  );
}

function StatusBadge({ status, pnl }) {
  if (status === "open") return (
    <span style={{ color: C.accent, fontSize: 11, fontWeight: 600 }}>● OPEN</span>
  );
  const win = pnl > 0;
  return (
    <span style={{ color: win ? C.call : C.put, fontSize: 11, fontWeight: 600 }}>
      {win ? "✓" : "✗"} {pnl != null ? `${pnl > 0 ? "+" : ""}${pnl.toFixed(2)}%` : "CLOSED"}
    </span>
  );
}

export default function TodaySignals({ onViewInGapFade, onViewInChart }) {
  const [trades, setTrades]       = useState([]);
  const [bestTrade, setBestTrade] = useState(null);
  const [loading, setLoading]     = useState(true);
  const [lastFetch, setLastFetch] = useState(null);
  const [filter, setFilter]       = useState("ALL"); // ALL | CALL | PUT | S | A
  const [optMode, setOptMode]     = useState("day"); // day | swing

  const fetchSignals = async () => {
    try {
      const [r1, r2] = await Promise.all([
        fetch(`${API}/api/today_signals`),
        fetch(`${API}/api/best_trade`),
      ]);
      const d1 = await r1.json();
      const d2 = await r2.json();
      setTrades(d1.trades || []);
      setBestTrade(d2.trade || null);
      setLastFetch(new Date());
    } catch {
      // api.py not running — keep old data
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSignals();
    const id = setInterval(fetchSignals, 30_000);
    return () => clearInterval(id);
  }, []);

  const filtered = trades.filter(t => {
    if (filter === "CALL") return t.direction === "CALL";
    if (filter === "PUT")  return t.direction === "PUT";
    if (filter === "S")    return t.grade === "S";
    if (filter === "A")    return ["S","A"].includes(t.grade);
    return true;
  });

  const open     = trades.filter(t => t.status === "open").length;
  const closed   = trades.filter(t => t.status === "closed");
  const wins     = closed.filter(t => (t.pnl_pct || 0) > 0).length;
  const totalPnl = closed.reduce((s, t) => s + (t.pnl_pct || 0), 0);

  const col = {
    padding: "10px 12px", textAlign: "left", borderBottom: `1px solid ${C.border}`,
    fontSize: 12, color: C.muted, fontWeight: 600, whiteSpace: "nowrap",
  };
  const cell = {
    padding: "9px 12px", borderBottom: `1px solid ${C.border}55`,
    fontSize: 12, color: C.text, whiteSpace: "nowrap",
  };

  return (
    <div style={{ background: C.bg, minHeight: "100%", padding: 20, fontFamily: "system-ui, sans-serif" }}>

      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 16, flexWrap: "wrap" }}>
        <div>
          <h2 style={{ margin: 0, color: C.text, fontSize: 16, fontWeight: 700 }}>Today's Signals</h2>
          {lastFetch && (
            <div style={{ fontSize: 11, color: C.muted, marginTop: 2 }}>
              Updated {lastFetch.toLocaleTimeString()} · auto-refreshes every 30s
            </div>
          )}
        </div>

        {trades.length > 0 && (
          <div style={{ display: "flex", gap: 12, marginLeft: "auto", flexWrap: "wrap" }}>
            <Stat label="Total" value={trades.length} color={C.accent} />
            <Stat label="Open"  value={open}           color={C.accent} />
            {closed.length > 0 && <>
              <Stat label="Closed" value={closed.length}                              color={C.muted} />
              <Stat label="WR"     value={`${Math.round(wins/closed.length*100)}%`}   color={wins/closed.length >= 0.5 ? C.call : C.put} />
              <Stat label="P&L"    value={`${totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(2)}%`} color={totalPnl >= 0 ? C.call : C.put} />
            </>}
          </div>
        )}
      </div>

      {/* Filters + option mode toggle */}
      <div style={{ display: "flex", gap: 6, marginBottom: 14, flexWrap: "wrap", alignItems: "center" }}>
        {["ALL","CALL","PUT","S","A"].map(f => (
          <button key={f} onClick={() => setFilter(f)} style={{
            padding: "4px 12px", borderRadius: 4, fontSize: 11, fontWeight: 600,
            border: `1px solid ${filter === f ? C.accent : C.border}`,
            background: filter === f ? C.accent + "22" : "transparent",
            color: filter === f ? C.accent : C.muted, cursor: "pointer",
          }}>{f}</button>
        ))}

        {/* Option mode toggle */}
        <div style={{ display: "flex", marginLeft: 12, border: `1px solid ${C.border}`, borderRadius: 4, overflow: "hidden" }}>
          {[["day","📅 Day (ATM)"],["swing","🌙 Swing (OTM 7-14d)"]].map(([mode, label]) => (
            <button key={mode} onClick={() => setOptMode(mode)} style={{
              padding: "4px 12px", fontSize: 11, fontWeight: 600, cursor: "pointer",
              border: "none", borderRight: mode === "day" ? `1px solid ${C.border}` : "none",
              background: optMode === mode ? C.accent + "22" : "transparent",
              color: optMode === mode ? C.accent : C.muted,
            }}>{label}</button>
          ))}
        </div>

        <button onClick={fetchSignals} style={{
          marginLeft: "auto", padding: "4px 12px", borderRadius: 4, fontSize: 11,
          border: `1px solid ${C.border}`, background: "transparent", color: C.muted, cursor: "pointer",
        }}>↻ Refresh</button>
      </div>

      {/* Best Trade Banner */}
      {bestTrade?.option && (
        <div style={{
          background: "linear-gradient(135deg, #f59e0b18, #22c55e18)",
          border: `1px solid ${C.gold}55`, borderRadius: 10,
          padding: "16px 20px", marginBottom: 16,
          display: "flex", alignItems: "center", gap: 20, flexWrap: "wrap",
        }}>
          <div>
            <div style={{ fontSize: 10, color: C.gold, fontWeight: 700, letterSpacing: "0.1em", marginBottom: 4 }}>
              ★ TODAY'S BEST TRADE
            </div>
            <div style={{ fontSize: 20, fontWeight: 800, color: C.text, fontFamily: "monospace" }}>
              {bestTrade.symbol}
              <span style={{ fontSize: 12, color: C.call, marginLeft: 10, fontWeight: 600 }}>
                ▲ CALL · Score {bestTrade.score?.toFixed(0)}%
              </span>
            </div>
            <div style={{ fontSize: 11, color: C.muted, marginTop: 2 }}>
              Signal fired at {bestTrade.time}
            </div>
          </div>

          <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
            <OptionDetail label="Strike"    value={`$${bestTrade.option.strike}`} color={C.text} />
            <OptionDetail label="Expiry"    value={`${bestTrade.option.expiry} (${bestTrade.option.dte}d)`} color={C.text} />
            <OptionDetail label="Est. Cost" value={`~$${bestTrade.option.cost?.toFixed(0)}`} color={C.gold} />
            <OptionDetail label="Contracts" value="1" color={C.text} />
            <OptionDetail label="Exit TP"   value="+50% option" color={C.call} />
            <OptionDetail label="Exit SL"   value="−40% option" color={C.put} />
          </div>

          <div style={{ marginLeft: "auto", fontSize: 11, color: C.muted, maxWidth: 180, lineHeight: 1.5 }}>
            Stock stop ${bestTrade.stop?.toFixed(2)} · target ${bestTrade.target?.toFixed(2)}<br />
            ATM · next expiry · 1 contract
          </div>
        </div>
      )}

      {/* Table */}
      {loading ? (
        <div style={{ color: C.muted, fontSize: 13, padding: 40, textAlign: "center" }}>Loading…</div>
      ) : filtered.length === 0 ? (
        <div style={{ color: C.muted, fontSize: 13, padding: 60, textAlign: "center" }}>
          <div style={{ fontSize: 32, marginBottom: 10 }}>📭</div>
          No signals yet today — scanner fires every 5 min from 9:35 AM ET
        </div>
      ) : (
        <div style={{ overflowX: "auto", borderRadius: 8, border: `1px solid ${C.border}` }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ background: C.surface }}>
                <th style={col}>Time</th>
                <th style={col}>Symbol</th>
                <th style={col}>Dir</th>
                <th style={col}>Grade</th>
                <th style={col}>Score</th>
                <th style={col}>Entry</th>
                <th style={col}>Stop</th>
                <th style={col}>Target / Swing ★</th>
                <th style={col}>R:R</th>
                <th style={col}>Volume</th>
                <th style={col}>Level</th>
                <th style={col}>{optMode === "swing" ? "Swing Option (OTM 7-14d)" : "Option to Buy"}</th>
                <th style={col}>Est. Cost</th>
                <th style={col}>Status</th>
                <th style={{ ...col, textAlign: "center" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((t, i) => (
                <tr key={i} style={{ background: i % 2 === 0 ? "transparent" : C.surface + "88" }}>
                  <td style={{ ...cell, color: C.muted, fontFamily: "monospace" }}>{t.time}</td>
                  <td style={{ ...cell, fontWeight: 700, fontFamily: "monospace", color: C.text }}>{t.symbol}</td>
                  <td style={cell}><DirBadge dir={t.direction} /></td>
                  <td style={cell}><GradeBadge grade={t.grade} /></td>
                  <td style={{ ...cell, fontFamily: "monospace" }}>
                    <div style={{ color: C.accent, fontWeight: 700 }}>{t.score?.toFixed(0)}%</div>
                    {t.base_score != null && t.bonuses?.length > 0 && (
                      <div style={{ fontSize: 9, color: C.muted, marginTop: 2, lineHeight: 1.5 }}>
                        <span style={{ color: "#444" }}>base {t.base_score?.toFixed(0)}</span>
                        {t.bonuses.map((b, i) => (
                          <span key={i} style={{ color: b.pts > 0 ? C.call : C.put }}>
                            {" "}{b.pts > 0 ? "+" : ""}{b.pts} {b.label}
                          </span>
                        ))}
                      </div>
                    )}
                  </td>
                  <td style={{ ...cell, fontFamily: "monospace" }}>${t.entry?.toFixed(2)}</td>
                  <td style={{ ...cell, fontFamily: "monospace", color: C.put }}>${t.stop?.toFixed(2)}</td>
                  <td style={{ ...cell, fontFamily: "monospace" }}>
                    <div style={{ color: C.call }}>${t.target?.toFixed(2)}</div>
                    {t.chloe_setup && t.swing_target != null && (
                      <div style={{ fontSize: 10, color: "#eab308", marginTop: 2, fontWeight: 700 }}
                           title="Chloe swing target — next major level if held past day target">
                        ★ ${t.swing_target.toFixed(2)}
                      </div>
                    )}
                  </td>
                  <td style={{ ...cell, fontFamily: "monospace" }}>{t.rr?.toFixed(1)}R</td>
                  <td style={cell}><VolBadge rv={t.rel_vol} /></td>
                  <td style={cell}>
                    <LevelBadge levelBreak={t.level_break} />
                    <ChloeBadge t={t} />
                  </td>
                  <td style={{ ...cell, fontFamily: "monospace", fontSize: 11 }}>
                    {(() => {
                      const opt = optMode === "swing" ? t.swing_option : t.option;
                      const dirCol = t.direction === "CALL" ? C.call : C.put;
                      if (!opt) return <span style={{ color: C.muted }}>—</span>;
                      return (
                        <span style={{ color: dirCol }}>
                          ${opt.strike} {t.direction} · {opt.expiry}
                          {optMode === "swing" && <span style={{ color: C.muted, fontSize: 10 }}> · {opt.dte}d</span>}
                        </span>
                      );
                    })()}
                  </td>
                  <td style={{ ...cell, fontFamily: "monospace", fontSize: 11 }}>
                    {(() => {
                      const opt = optMode === "swing" ? t.swing_option : t.option;
                      return opt
                        ? <span style={{ color: C.gold }}>~${opt.cost?.toFixed(0)}</span>
                        : <span style={{ color: C.muted }}>—</span>;
                    })()}
                  </td>
                  <td style={cell}><StatusBadge status={t.status} pnl={t.pnl_pct} /></td>
                  <td style={{ ...cell, textAlign: "center" }}>
                    <div style={{ display: "flex", gap: 4, justifyContent: "center" }}>
                      <button
                        onClick={() => onViewInChart && onViewInChart(t.symbol, t)}
                        style={{
                          padding: "3px 9px", borderRadius: 4, fontSize: 11, fontWeight: 600,
                          border: `1px solid ${C.accent}55`,
                          background: C.accent + "11",
                          color: C.accent,
                          cursor: "pointer", whiteSpace: "nowrap",
                        }}
                      >
                        🕯️ Chart
                      </button>
                      <button
                        onClick={() => onViewInGapFade(t.symbol, t.direction, t)}
                        style={{
                          padding: "3px 9px", borderRadius: 4, fontSize: 11, fontWeight: 600,
                          border: `1px solid ${C.border}`,
                          background: "transparent",
                          color: C.muted,
                          cursor: "pointer", whiteSpace: "nowrap",
                        }}
                      >
                        ⚡ Options
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, color }) {
  return (
    <div style={{
      background: C.surface, border: `1px solid ${C.border}`,
      borderRadius: 6, padding: "4px 12px", textAlign: "center",
    }}>
      <div style={{ fontSize: 10, color: C.muted, marginBottom: 1 }}>{label}</div>
      <div style={{ fontSize: 14, fontWeight: 700, color: color || C.text, fontFamily: "monospace" }}>{value}</div>
    </div>
  );
}

function OptionDetail({ label, value, color }) {
  return (
    <div style={{ textAlign: "center" }}>
      <div style={{ fontSize: 10, color: C.muted, marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 14, fontWeight: 700, color: color || C.text, fontFamily: "monospace" }}>{value}</div>
    </div>
  );
}
