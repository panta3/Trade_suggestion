import { useState, useEffect } from "react";

const API = "http://localhost:5000";

const C = {
  bg: "#0d0d0d", surface: "#111", border: "#222",
  text: "#e8e8e8", muted: "#555", accent: "#3b82f6",
  call: "#22c55e", put: "#ef4444", gold: "#f59e0b",
  warn: "#f97316",
};

// Compute % a stock is above its MA20
function extPct(entry, ma20) {
  if (!ma20 || ma20 <= 0) return 0;
  return ((entry - ma20) / ma20) * 100;
}

function buildSummary(setups) {
  if (!setups.length) return null;

  const ext     = setups.map(s => ({ ...s, _ext: extPct(s.entry, s.ma20) }));
  const clean   = ext.filter(s => s._ext <= 10);   // ≤10% above MA20
  const caution = ext.filter(s => s._ext > 10 && s._ext <= 30);
  const skip    = ext.filter(s => s._ext > 30);
  const sorted  = [...ext].sort((a, b) => a._ext - b._ext);
  const best    = sorted.slice(0, 3);

  // Build the plain-English verdict
  let verdict = "";
  let verdictColor = C.call;

  if (clean.length === 0 && caution.length === 0) {
    verdict = `All ${setups.length} setups ran too hard today — every stock is already 30%+ above its 20-day average. This happens when IBKR gainer stocks spike. Don't buy any of these tomorrow at open. Wait and check again next session.`;
    verdictColor = C.put;
  } else if (clean.length === 0) {
    verdict = `No setup is in a clean buy zone yet. The ${caution.length} caution setup${caution.length > 1 ? "s" : ""} (${caution.map(s => s.symbol).join(", ")}) could work IF the stock pulls back 5-10% before you enter. Watch them pre-market — if they open flat or slightly down, that's your window.`;
    verdictColor = C.gold;
  } else {
    verdict = `${clean.length} setup${clean.length > 1 ? "s are" : " is"} in a good buy zone (${clean.map(s => s.symbol).join(", ")}). These stocks haven't run too far and have a realistic chance of continuing. Enter at tomorrow's open or wait for a small dip. The other ${setups.length - clean.length} setup${setups.length - clean.length !== 1 ? "s are" : " is"} extended — skip or wait for a pullback first.`;
    verdictColor = C.call;
  }

  return { clean, caution, skip, best, verdict, verdictColor };
}

export default function SwingWatchlist() {
  const [data,    setData]    = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API}/api/swing_watchlist`)
      .then(r => r.json())
      .then(d => { setData(d); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  const cell = { padding: "10px 14px", borderBottom: `1px solid ${C.border}55`, fontSize: 12, color: C.text, whiteSpace: "nowrap" };
  const col  = { ...cell, color: C.muted, fontWeight: 600, background: C.surface };

  if (loading) return (
    <div style={{ background: C.bg, padding: 40, color: C.muted, textAlign: "center", fontFamily: "system-ui" }}>
      Loading swing watchlist…
    </div>
  );

  const setups  = data?.setups || [];
  const genAt   = data?.generated_at ? new Date(data.generated_at).toLocaleTimeString() : null;
  const summary = buildSummary(setups);

  return (
    <div style={{ background: C.bg, minHeight: "100%", padding: 20, fontFamily: "system-ui, sans-serif" }}>

      {/* Header */}
      <div style={{ marginBottom: 16 }}>
        <h2 style={{ margin: 0, color: C.text, fontSize: 16, fontWeight: 700 }}>
          🌙 Swing Watchlist
        </h2>
        <div style={{ fontSize: 11, color: C.muted, marginTop: 4 }}>
          {genAt
            ? `Generated at ${genAt} — intraday Grade S signals that also pass daily chart check`
            : "Generated at 3:45 PM ET each session — shows today's Grade S signals with clean daily setups"}
        </div>
      </div>

      {setups.length === 0 ? (
        <div style={{ color: C.muted, textAlign: "center", padding: 60 }}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>🌙</div>
          <div style={{ fontSize: 14, marginBottom: 6 }}>No swing setups today</div>
          <div style={{ fontSize: 12 }}>
            {data?.message || "Watchlist generates at 3:45 PM when Grade S signals pass the daily chart check (MA50, trend, candle pattern)."}
          </div>
        </div>
      ) : (
        <>
          {/* ── Plain-English Summary ── */}
          {summary && (
            <div style={{
              background: C.surface, border: `1px solid ${C.border}`,
              borderRadius: 10, padding: "16px 20px", marginBottom: 16,
            }}>
              <div style={{ fontSize: 11, color: C.muted, fontWeight: 700, letterSpacing: "0.08em", marginBottom: 10 }}>
                TOMORROW'S PLAN
              </div>

              {/* Traffic-light row */}
              <div style={{ display: "flex", gap: 10, marginBottom: 14, flexWrap: "wrap" }}>
                <PlanBadge color={C.call} label="Ready to enter" count={summary.clean.length}
                  tip="≤10% above MA20 — stock hasn't run too far"
                  symbols={summary.clean.map(s => s.symbol)} />
                <PlanBadge color={C.gold} label="Wait for pullback" count={summary.caution.length}
                  tip="10–30% above MA20 — could work if it dips first"
                  symbols={summary.caution.map(s => s.symbol)} />
                <PlanBadge color={C.put} label="Skip / too extended" count={summary.skip.length}
                  tip=">30% above MA20 — already ran too hard, high reversal risk"
                  symbols={summary.skip.map(s => s.symbol)} />
              </div>

              {/* Verdict paragraph */}
              <div style={{
                fontSize: 13, color: summary.verdictColor, lineHeight: 1.65,
                borderLeft: `3px solid ${summary.verdictColor}44`, paddingLeft: 12,
              }}>
                {summary.verdict}
              </div>

              {/* Best picks detail */}
              {summary.best.length > 0 && (
                <div style={{ marginTop: 14 }}>
                  <div style={{ fontSize: 11, color: C.muted, fontWeight: 600, marginBottom: 8 }}>
                    CLEANEST SETUPS (least extended)
                  </div>
                  <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                    {summary.best.map(s => {
                      const opt = s.option || {};
                      const isExtended = s._ext > 10;
                      const extColor = s._ext <= 10 ? C.call : s._ext <= 30 ? C.gold : C.put;
                      return (
                        <div key={s.symbol} style={{
                          background: C.bg, border: `1px solid ${C.border}`,
                          borderRadius: 8, padding: "10px 14px", minWidth: 180,
                        }}>
                          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                            <span style={{ fontWeight: 700, fontFamily: "monospace", color: C.text, fontSize: 14 }}>
                              {s.symbol}
                            </span>
                            <span style={{
                              fontSize: 10, fontWeight: 700, color: extColor,
                              background: extColor + "22", border: `1px solid ${extColor}44`,
                              borderRadius: 3, padding: "1px 5px",
                            }}>
                              {s._ext > 0 ? `+${s._ext.toFixed(0)}% above MA20` : "at MA20"}
                            </span>
                          </div>
                          <div style={{ fontSize: 11, color: C.muted, lineHeight: 1.6 }}>
                            <div>Entry <span style={{ color: C.text, fontFamily: "monospace" }}>${s.entry?.toFixed(2)}</span>
                              {" "}· Target <span style={{ color: C.call, fontFamily: "monospace" }}>${s.target?.toFixed(2)}</span></div>
                            <div>Stop <span style={{ color: C.put, fontFamily: "monospace" }}>${s.stop?.toFixed(2)}</span></div>
                            {opt.strike && (
                              <div style={{ marginTop: 4, color: C.gold, fontFamily: "monospace" }}>
                                ${opt.strike} {s.direction} · {opt.expiry} · ~${opt.cost?.toFixed(0)}
                              </div>
                            )}
                            {isExtended && (
                              <div style={{ marginTop: 4, color: C.gold, fontSize: 10 }}>
                                Wait for dip toward ${s.ma20?.toFixed(2)} before entering
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* Rules reminder */}
              <div style={{
                marginTop: 14, fontSize: 11, color: C.muted, lineHeight: 1.7,
                borderTop: `1px solid ${C.border}`, paddingTop: 12,
              }}>
                <span style={{ color: C.accent, fontWeight: 700 }}>Rules: </span>
                If a stock gaps up 3%+ at open tomorrow — skip it, the move is already done.
                Only enter "Ready" setups at open or any setup that pulls back to near MA20.
                Buy 1 contract of the suggested option. Exit if option drops 40%, or take profit at +50%.
              </div>
            </div>
          )}

          {/* ── Full Table ── */}
          <div style={{ overflowX: "auto", borderRadius: 8, border: `1px solid ${C.border}` }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th style={col}>Symbol</th>
                  <th style={col}>Dir</th>
                  <th style={col}>Intraday score</th>
                  <th style={col}>Daily score</th>
                  <th style={col}>Entry ~</th>
                  <th style={col}>Stop</th>
                  <th style={col}>Target</th>
                  <th style={col}>MA20</th>
                  <th style={col}>MA50</th>
                  <th style={col}>Option to buy</th>
                  <th style={col}>Est. cost</th>
                  <th style={col}>Daily reasons</th>
                </tr>
              </thead>
              <tbody>
                {setups.map((s, i) => {
                  const isCall   = s.direction === "CALL";
                  const dirColor = isCall ? C.call : C.put;
                  const opt      = s.option || {};
                  const ext      = extPct(s.entry, s.ma20);
                  const rowBg    = ext > 30
                    ? C.put + "08"
                    : ext > 10
                    ? C.gold + "08"
                    : i % 2 === 0 ? "transparent" : C.surface + "88";
                  return (
                    <tr key={i} style={{ background: rowBg }}>
                      <td style={{ ...cell, fontWeight: 700, fontFamily: "monospace", color: C.text }}>{s.symbol}</td>
                      <td style={cell}>
                        <span style={{
                          background: dirColor + "22", color: dirColor,
                          border: `1px solid ${dirColor}55`, borderRadius: 4,
                          padding: "1px 8px", fontSize: 11, fontWeight: 700, fontFamily: "monospace",
                        }}>{isCall ? "▲ CALL" : "▼ PUT"}</span>
                      </td>
                      <td style={{ ...cell, fontFamily: "monospace", color: C.accent }}>{s.intraday_score?.toFixed(0)}%</td>
                      <td style={{ ...cell, fontFamily: "monospace", color: s.daily_score >= 35 ? C.call : C.gold }}>
                        {s.daily_score}
                      </td>
                      <td style={{ ...cell, fontFamily: "monospace" }}>${s.entry?.toFixed(2)}</td>
                      <td style={{ ...cell, fontFamily: "monospace", color: C.put }}>${s.stop?.toFixed(2)}</td>
                      <td style={{ ...cell, fontFamily: "monospace", color: C.call }}>${s.target?.toFixed(2)}</td>
                      <td style={{ ...cell, fontFamily: "monospace", color: C.muted }}>${s.ma20?.toFixed(2)}</td>
                      <td style={{ ...cell, fontFamily: "monospace", color: C.muted }}>${s.ma50?.toFixed(2)}</td>
                      <td style={{ ...cell, fontFamily: "monospace", fontSize: 11 }}>
                        {opt.strike
                          ? <span style={{ color: dirColor }}>${opt.strike} {s.direction} · {opt.expiry} · {opt.dte}d</span>
                          : <span style={{ color: C.muted }}>—</span>}
                      </td>
                      <td style={{ ...cell, fontFamily: "monospace", fontSize: 11, color: C.gold }}>
                        {opt.cost ? `~$${opt.cost?.toFixed(0)}` : "—"}
                      </td>
                      <td style={{ ...cell, fontSize: 11, color: C.muted, maxWidth: 260, whiteSpace: "normal" }}>
                        {(s.daily_reasons || []).join(" · ")}
                        {(s.daily_notes || []).length > 0 && (
                          <span style={{ color: C.gold }}> ⚠ {s.daily_notes.join(", ")}</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

function PlanBadge({ color, label, count, tip, symbols }) {
  return (
    <div style={{
      background: color + "11", border: `1px solid ${color}33`,
      borderRadius: 8, padding: "8px 14px", minWidth: 120,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
        <span style={{
          background: color, borderRadius: "50%",
          width: 18, height: 18, display: "inline-flex", alignItems: "center",
          justifyContent: "center", fontSize: 11, fontWeight: 700, color: "#000",
          flexShrink: 0,
        }}>{count}</span>
        <span style={{ fontSize: 12, fontWeight: 700, color }}>{label}</span>
      </div>
      <div style={{ fontSize: 10, color: C.muted, marginBottom: 4 }}>{tip}</div>
      {symbols.length > 0 && (
        <div style={{ fontSize: 11, color: color, fontFamily: "monospace" }}>
          {symbols.join(", ")}
        </div>
      )}
      {symbols.length === 0 && (
        <div style={{ fontSize: 11, color: C.muted, fontStyle: "italic" }}>none</div>
      )}
    </div>
  );
}
