import { useState, useRef, useMemo, useEffect } from "react";

// ─── Python backend (Yahoo Finance via api.py) ────────────────────────────────
const API_BASE = "http://localhost:5000";
const RF_RATE  = 0.045;

async function fetchQuoteYahoo(ticker) {
  const res = await fetch(`${API_BASE}/api/quote?ticker=${ticker}`);
  if (!res.ok) throw new Error("Quote fetch failed");
  const d = await res.json();
  if (d.error) throw new Error(d.error);
  return d; // { price, prevClose, closes, rsi }
}

// ─── HV / RSI helpers ─────────────────────────────────────────────────────────
function calcHV(closes) {
  if (closes.length < 2) return 0.65;
  const returns = [];
  for (let i = 1; i < closes.length; i++) {
    const r = Math.log(closes[i] / closes[i - 1]);
    if (isFinite(r)) returns.push(r);
  }
  if (returns.length < 2) return 0.65;
  const mean = returns.reduce((a, b) => a + b, 0) / returns.length;
  const variance = returns.reduce((a, b) => a + (b - mean) ** 2, 0) / (returns.length - 1);
  const hv = Math.sqrt(variance * 252) * 100;
  return isFinite(hv) && hv > 0 ? hv : 0.65;
}
function calcRSI(closes, period = 14) {
  if (closes.length < period + 1) return null;
  let gains = 0, losses = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d > 0) gains += d; else losses += Math.abs(d);
  }
  let ag = gains / period, al = losses / period;
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    ag = (ag * (period - 1) + (d > 0 ? d : 0)) / period;
    al = (al * (period - 1) + (d < 0 ? Math.abs(d) : 0)) / period;
  }
  if (al === 0) return 100;
  return 100 - 100 / (1 + ag / al);
}

// ─── Black-Scholes Math ───────────────────────────────────────────────────────
function normalCDF(x) {
  const a1=0.254829592,a2=-0.284496736,a3=1.421413741,a4=-1.453152027,a5=1.061405429,p=0.3275911;
  const sign=x<0?-1:1; x=Math.abs(x)/Math.sqrt(2);
  const t=1/(1+p*x);
  const y=1-((((a5*t+a4)*t+a3)*t+a2)*t+a1)*t*Math.exp(-x*x);
  return 0.5*(1+sign*y);
}
function bs(S,K,T,r,sigma,type){
  if(!S||!K||!sigma||S<=0||K<=0||sigma<=0)return type==="put"?Math.max(K-S,0):Math.max(S-K,0);
  if(T<=0)return type==="put"?Math.max(K-S,0):Math.max(S-K,0);
  const d1=(Math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*Math.sqrt(T));
  const d2=d1-sigma*Math.sqrt(T);
  if(type==="call")return S*normalCDF(d1)-K*Math.exp(-r*T)*normalCDF(d2);
  return K*Math.exp(-r*T)*normalCDF(-d2)-S*normalCDF(-d1);
}
function getDelta(S,K,T,r,sigma,type){
  if(T<=0)return type==="put"?(S<K?-1:0):(S>K?1:0);
  const d1=(Math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*Math.sqrt(T));
  return type==="put"?normalCDF(d1)-1:normalCDF(d1);
}
function getGamma(S,K,T,r,sigma){
  if(T<=0)return 0;
  const d1=(Math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*Math.sqrt(T));
  return Math.exp(-d1*d1/2)/(S*sigma*Math.sqrt(T)*Math.sqrt(2*Math.PI));
}
function getTheta(S,K,T,r,sigma,type){
  if(T<=0)return 0;
  const d1=(Math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*Math.sqrt(T));
  const d2=d1-sigma*Math.sqrt(T);
  const nd1=Math.exp(-d1*d1/2)/Math.sqrt(2*Math.PI);
  if(type==="call")return(-(S*nd1*sigma)/(2*Math.sqrt(T))-r*K*Math.exp(-r*T)*normalCDF(d2))/365;
  return(-(S*nd1*sigma)/(2*Math.sqrt(T))+r*K*Math.exp(-r*T)*normalCDF(-d2))/365;
}
function getVega(S,K,T,r,sigma){
  if(T<=0)return 0;
  const d1=(Math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*Math.sqrt(T));
  return S*Math.exp(-d1*d1/2)/Math.sqrt(2*Math.PI)*Math.sqrt(T)/100;
}

// ─── Compute analysis ─────────────────────────────────────────────────────────
function compute(S0,S1,K,dte,iv0,ivBump,optPrice,type,nC=1){
  const T1=Math.max((dte-1)/365,0.001);
  const iv1=(iv0/100)*(1+ivBump/100);  // iv0 is %, convert to decimal before BS
  const gapPct=((S1-S0)/S0)*100;
  const priceAtOpen=bs(S1,K,T1,RF_RATE,iv1,type);
  const d=getDelta(S1,K,T1,RF_RATE,iv1,type);
  const g=getGamma(S1,K,T1,RF_RATE,iv1);
  const th=getTheta(S1,K,T1,RF_RATE,iv1,type);
  const v=getVega(S1,K,T1,RF_RATE,iv1);
  const suggestedBid=priceAtOpen*0.95;
  const ivCrushPrice=bs(S1,K,T1,RF_RATE,iv1*0.80,type);
  const absDelta=Math.abs(d);
  const targetStock=(mult)=>absDelta>=0.01?(type==="put"?S1-(priceAtOpen*mult/absDelta):S1+(priceAtOpen*mult/absDelta)):null;
  const status=priceAtOpen>optPrice*1.05?"FAVORABLE":priceAtOpen<optPrice*0.95?"UNFAVORABLE":"NEUTRAL";
  const scenarios=type==="put"
    ?[S1*0.97,S1*0.95,S1*0.92,S1*0.90,S1*0.85]
    :[S1*1.03,S1*1.05,S1*1.08,S1*1.10,S1*1.15];
  const scenarioTable=scenarios.map(price=>({
    stockPrice:price,
    optPrice:bs(price,K,T1,RF_RATE,iv1,type),
    pct:((bs(price,K,T1,RF_RATE,iv1,type)-priceAtOpen)/priceAtOpen)*100,
    dollar:(bs(price,K,T1,RF_RATE,iv1,type)-priceAtOpen)*nC*100,
  }));
  return{
    gapPct,priceAtOpen,suggestedBid,delta:d,gamma:g,theta:th,vega:v,
    target15:priceAtOpen*1.15,target20:priceAtOpen*1.20,target25:priceAtOpen*1.25,
    targetStock15:targetStock(0.15),targetStock20:targetStock(0.20),targetStock25:targetStock(0.25),
    ivCrushPrice,ivAtOpen:iv1*100,status,crushSurvives:ivCrushPrice>suggestedBid,
    scenarioTable,type,
    K,T1,iv1,S1,nC,
  };
}

function snapToHalf(x){ return Math.round(x * 2) / 2; }

// ─── Design ───────────────────────────────────────────────────────────────────
const C={bg:"#070707",panel:"#0d0d0d",border:"#1e1e1e",put:"#ff4455",call:"#00d4ff",accent:"#c8ff00",muted:"#555",text:"#f0f0f0",orange:"#ff9944"};
const mono="'DM Mono', monospace";
const disp="'Bebas Neue', sans-serif";
const baseInp={background:C.panel,border:`1px solid ${C.border}`,borderRadius:4,padding:"8px 10px",color:C.text,fontFamily:mono,fontSize:12,outline:"none",width:"100%",boxSizing:"border-box"};

const Label=({c})=><div style={{fontSize:9,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase",fontFamily:mono,marginBottom:4}}>{c}</div>;
const Field=({label,hint,children})=>(
  <div><Label c={label}/>{children}
  {hint&&<div style={{fontSize:8,color:"#333",fontFamily:mono,marginTop:2}}>{hint}</div>}</div>
);
const Inp=({value,onChange,placeholder,step="any",col})=>(
  <input type="number" value={value} onChange={e=>onChange(e.target.value)} placeholder={placeholder} step={step}
    style={{...baseInp,color:col||C.text}}
    onFocus={e=>e.target.style.borderColor=C.accent}
    onBlur={e=>e.target.style.borderColor=C.border}/>
);
const Stat=({label,value,accent,sub,col})=>(
  <div style={{background:C.panel,border:`1px solid ${accent?(col||C.accent)+"33":C.border}`,borderRadius:5,padding:"10px 12px",position:"relative",overflow:"hidden"}}>
    {accent&&<div style={{position:"absolute",top:0,left:0,right:0,height:2,background:col||C.accent}}/>}
    <div style={{fontSize:8,color:C.muted,letterSpacing:"0.1em",textTransform:"uppercase",fontFamily:mono}}>{label}</div>
    <div style={{fontSize:accent?20:14,color:accent?(col||C.accent):C.text,fontFamily:mono,fontWeight:600,lineHeight:1.2}}>{value}</div>
    {sub&&<div style={{fontSize:8,color:"#444",fontFamily:mono,marginTop:2}}>{sub}</div>}
  </div>
);
const Btn=({onClick,children,secondary,loading,small,col,disabled})=>(
  <button onClick={onClick} disabled={loading||disabled} style={{
    background:secondary?"transparent":(col||C.accent),color:secondary?C.muted:C.bg,
    border:secondary?`1px solid ${C.border}`:"none",borderRadius:4,
    padding:small?"5px 12px":"10px 24px",fontSize:small?8:10,
    fontFamily:mono,letterSpacing:"0.1em",cursor:(loading||disabled)?"not-allowed":"pointer",
    textTransform:"uppercase",opacity:(loading||disabled)?0.5:1,whiteSpace:"nowrap"
  }}>{loading?"...":children}</button>
);
const Tag=({text,col})=>(
  <span style={{background:col+"22",border:`1px solid ${col}44`,color:col,fontSize:8,padding:"2px 7px",borderRadius:3,fontFamily:mono,letterSpacing:"0.08em",textTransform:"uppercase"}}>{text}</span>
);
const _ok=n=>n!=null&&isFinite(+n)&&!isNaN(+n);
const fmtN=(n,d=2)=>_ok(n)?(+n).toFixed(d):"—";
const fmtC=n=>_ok(n)?"$"+(+n).toFixed(2):"—";
const fmtD=n=>_ok(n)?((+n>=0?"+$":"-$")+Math.abs(+n).toFixed(0)):"—";

// ─── Slider ───────────────────────────────────────────────────────────────────
const Slider=({label,min,max,value,onChange,fmt:fmtFn,col,hint})=>(
  <div style={{display:"flex",flexDirection:"column",gap:4}}>
    <div style={{display:"flex",justifyContent:"space-between",alignItems:"baseline"}}>
      <Label c={label}/>
      <span style={{fontSize:11,color:col||C.accent,fontFamily:mono,fontWeight:600}}>{fmtFn?fmtFn(value):value}</span>
    </div>
    <div style={{position:"relative",height:20,display:"flex",alignItems:"center"}}>
      <div style={{position:"absolute",left:0,right:0,height:3,background:C.border,borderRadius:2}}/>
      <div style={{position:"absolute",left:0,height:3,background:col||C.accent,borderRadius:2,width:`${((value-min)/(max-min))*100}%`}}/>
      <input type="range" min={min} max={max} step={(max-min)/200} value={value} onChange={e=>onChange(+e.target.value)}
        style={{position:"absolute",width:"100%",margin:0,padding:0,height:20,
          WebkitAppearance:"none",background:"transparent",cursor:"pointer",outline:"none"}}/>
    </div>
    {hint&&<div style={{fontSize:8,color:"#333",fontFamily:mono}}>{hint}</div>}
    <style>{`input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:${col||C.accent};cursor:pointer;border:2px solid #080808;}`}</style>
  </div>
);

// ─── Payoff Chart ─────────────────────────────────────────────────────────────
function PayoffChart({ K, T1, iv1, ivSim, premium, optType, spotRange, simSpot, simDTE, contracts }) {
  const W=680, H=220, padL=56, padR=20, padT=18, padB=32;
  const chartW=W-padL-padR, chartH=H-padT-padB;
  const [hover,setHover]=useState(null);
  const svgRef=useRef();
  const [sMin,sMax]=spotRange;
  const N=120;
  const prices=Array.from({length:N},(_,i)=>sMin+(sMax-sMin)*(i/(N-1)));

  const expiryPnl=prices.map(S=>(bs(S,K,0,RF_RATE,iv1,optType)-premium)*contracts*100);
  const openPnl  =prices.map(S=>(bs(S,K,T1,RF_RATE,iv1,optType)-premium)*contracts*100);
  const simT=Math.max(simDTE/365,0.001);
  const simPnl   =prices.map(S=>(bs(S,K,simT,RF_RATE,ivSim/100,optType)-premium)*contracts*100);

  const allY=[...expiryPnl,...openPnl,...simPnl];
  const yMin=Math.min(...allY,-premium*contracts*100*1.2);
  const yMax=Math.max(...allY,premium*contracts*100*3);
  const yRange=yMax-yMin||1;

  const toX=S=>padL+(S-sMin)/(sMax-sMin)*chartW;
  const toY=pnl=>padT+chartH-(pnl-yMin)/yRange*chartH;
  const makePath=pnls=>pnls.map((p,i)=>`${i===0?"M":"L"}${toX(prices[i]).toFixed(1)},${toY(p).toFixed(1)}`).join(" ");

  const zeroY=toY(0);
  const makeFilledPath=(pnls,above)=>{
    const pts=pnls.map((p,i)=>({x:toX(prices[i]),y:toY(p)}));
    if(above){
      const seg=pts.map((p,i)=>`${i===0?"M":"L"}${p.x.toFixed(1)},${Math.min(p.y,zeroY).toFixed(1)}`).join(" ");
      return seg+` L${pts[pts.length-1].x.toFixed(1)},${zeroY} L${pts[0].x.toFixed(1)},${zeroY} Z`;
    }else{
      const seg=pts.map((p,i)=>`${i===0?"M":"L"}${p.x.toFixed(1)},${Math.max(p.y,zeroY).toFixed(1)}`).join(" ");
      return seg+` L${pts[pts.length-1].x.toFixed(1)},${zeroY} L${pts[0].x.toFixed(1)},${zeroY} Z`;
    }
  };

  let breakevens=[];
  for(let i=1;i<expiryPnl.length;i++){
    if((expiryPnl[i-1]<0&&expiryPnl[i]>=0)||(expiryPnl[i-1]>=0&&expiryPnl[i]<0)){
      const t=-expiryPnl[i-1]/(expiryPnl[i]-expiryPnl[i-1]);
      breakevens.push(prices[i-1]+(prices[i]-prices[i-1])*t);
    }
  }

  const handleMouseMove=(e)=>{
    const rect=svgRef.current?.getBoundingClientRect();
    if(!rect)return;
    const mx=e.clientX-rect.left;
    const fracX=(mx-padL)/chartW;
    if(fracX<0||fracX>1){setHover(null);return;}
    const S=sMin+(sMax-sMin)*fracX;
    const idx=Math.max(0,Math.min(N-1,Math.round(fracX*(N-1))));
    setHover({S,expiryP:expiryPnl[idx],openP:openPnl[idx],simP:simPnl[idx],x:mx});
  };

  const simX=toX(simSpot);
  const yTicks=[];
  const tickStep=Math.pow(10,Math.floor(Math.log10(yRange/4)));
  const tickMin=Math.ceil(yMin/tickStep)*tickStep;
  for(let v=tickMin;v<=yMax;v+=tickStep) yTicks.push(v);
  const fmtTick=v=>v>=0?`+$${Math.abs(v).toFixed(0)}`:`-$${Math.abs(v).toFixed(0)}`;

  return(
    <div style={{position:"relative",userSelect:"none"}}>
      <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} style={{width:"100%",height:"auto",display:"block",cursor:"crosshair"}}
        onMouseMove={handleMouseMove} onMouseLeave={()=>setHover(null)}>
        {yTicks.map(v=>{
          const y=toY(v);
          if(y<padT||y>padT+chartH)return null;
          return <g key={v}>
            <line x1={padL} y1={y} x2={padL+chartW} y2={y} stroke={v===0?"#444":"#181818"} strokeWidth={v===0?1:0.5}/>
            <text x={padL-5} y={y+4} textAnchor="end" fill={v===0?"#666":"#333"} fontSize={8} fontFamily={mono}>{fmtTick(v)}</text>
          </g>;
        })}
        {[0,0.25,0.5,0.75,1].map(f=>{
          const S=sMin+(sMax-sMin)*f;
          return <text key={f} x={padL+f*chartW} y={H-padB+12} textAnchor="middle" fill="#333" fontSize={8} fontFamily={mono}>${S.toFixed(0)}</text>;
        })}
        <path d={makeFilledPath(simPnl,true)}  fill={`${optType==="put"?C.put:C.call}18`} stroke="none"/>
        <path d={makeFilledPath(simPnl,false)} fill="#ff445518" stroke="none"/>
        <path d={makePath(expiryPnl)} fill="none" stroke="#444"   strokeWidth={1.5} strokeDasharray="4 3"/>
        <path d={makePath(openPnl)}   fill="none" stroke={C.orange} strokeWidth={1.5} strokeDasharray="2 2"/>
        <path d={makePath(simPnl)}    fill="none" stroke={optType==="put"?C.put:C.call} strokeWidth={2.5}/>
        {breakevens.map((be,i)=>(
          <g key={i}>
            <line x1={toX(be)} y1={padT} x2={toX(be)} y2={padT+chartH} stroke={C.accent} strokeWidth={0.8} strokeDasharray="3 2"/>
            <text x={toX(be)} y={padT-4} textAnchor="middle" fill={C.accent} fontSize={8} fontFamily={mono}>BE ${be.toFixed(1)}</text>
          </g>
        ))}
        <line x1={simX} y1={padT} x2={simX} y2={padT+chartH} stroke="#fff" strokeWidth={1} opacity={0.25}/>
        <circle cx={simX} cy={toY((bs(simSpot,K,simT,RF_RATE,ivSim/100,optType)-premium)*contracts*100)} r={4} fill={optType==="put"?C.put:C.call} stroke={C.bg} strokeWidth={1.5}/>
        {hover&&(()=>{
          const ttX=hover.x+12<W-120?hover.x+12:hover.x-130;
          return(
            <g>
              <line x1={hover.x} y1={padT} x2={hover.x} y2={padT+chartH} stroke="#fff" strokeWidth={0.5} opacity={0.3}/>
              <rect x={ttX} y={padT+4} width={118} height={66} rx={4} fill="#111" stroke="#333" strokeWidth={0.8}/>
              <text x={ttX+8} y={padT+16} fill={C.accent}   fontSize={8} fontFamily={mono} fontWeight={600}>${hover.S.toFixed(2)}</text>
              <text x={ttX+8} y={padT+28} fill={C.muted}    fontSize={7} fontFamily={mono}>Sim:    <tspan fill={hover.simP>=0?(optType==="put"?C.put:C.call):"#ff4455"}>{fmtD(hover.simP)}</tspan></text>
              <text x={ttX+8} y={padT+40} fill={C.muted}    fontSize={7} fontFamily={mono}>At Open:<tspan fill={C.orange}>{fmtD(hover.openP)}</tspan></text>
              <text x={ttX+8} y={padT+52} fill={C.muted}    fontSize={7} fontFamily={mono}>Expiry: <tspan fill="#888">{fmtD(hover.expiryP)}</tspan></text>
              <text x={ttX+8} y={padT+62} fill={C.muted}    fontSize={7} fontFamily={mono}>per {contracts} contract{contracts>1?"s":""}</text>
            </g>
          );
        })()}
        <g>
          {[[optType==="put"?C.put:C.call,"Simulation",false],[C.orange,"At Open",true],["#555","At Expiry",true]].map(([col,lbl,dash],i)=>(
            <g key={lbl} transform={`translate(${padL+i*150},${H-4})`}>
              <line x1={0} y1={0} x2={20} y2={0} stroke={col} strokeWidth={dash?1.5:2} strokeDasharray={dash?"4 3":"none"}/>
              <text x={25} y={3} fill="#555" fontSize={8} fontFamily={mono}>{lbl}</text>
            </g>
          ))}
        </g>
      </svg>
    </div>
  );
}

// ─── RSI Bar ──────────────────────────────────────────────────────────────────
const RSIBar=({rsi})=>{
  if(rsi===null)return null;
  const r=+rsi;
  const color=r<30?C.call:r>70?C.put:C.accent;
  const label=r<30?"OVERSOLD":r>70?"OVERBOUGHT":"NEUTRAL";
  return(
    <div style={{background:C.panel,border:`1px solid ${color}33`,borderRadius:5,padding:"10px 12px",position:"relative",overflow:"hidden"}}>
      <div style={{position:"absolute",top:0,left:0,right:0,height:2,background:color}}/>
      <div style={{fontSize:8,color:C.muted,letterSpacing:"0.1em",textTransform:"uppercase",fontFamily:mono}}>RSI-14</div>
      <div style={{display:"flex",alignItems:"baseline",gap:8}}>
        <div style={{fontSize:20,color,fontFamily:mono,fontWeight:600}}>{fmtN(r,1)}</div>
        <Tag text={label} col={color}/>
      </div>
      <div style={{marginTop:6,height:4,background:C.border,borderRadius:2,overflow:"hidden"}}>
        <div style={{height:"100%",width:`${r}%`,background:color,borderRadius:2,transition:"width 0.5s"}}/>
      </div>
    </div>
  );
};

const TABS=["PRICER","SCREENER","SIGNALS","TRADE LOG"];

export default function App({ externalSymbol, externalDirection, externalTrade }){
  const [tab,setTab]=useState(0);
  const [ticker,setTicker]=useState("");
  const [close,setClose]=useState("");
  const [preMkt,setPreMkt]=useState("");
  const [strike,setStrike]=useState("");
  const [dte,setDte]=useState("1");
  const [iv,setIv]=useState("");
  const [ivBump,setIvBump]=useState("10");
  const [optPrice,setOptPrice]=useState("");
  const [contracts,setContracts]=useState("1");
  const [optType,setOptType]=useState("put");
  const [result,setResult]=useState(null);
  const [fetching,setFetching]=useState(false);
  const [fetchNote,setFetchNote]=useState("");
  const [fetchErr,setFetchErr]=useState(false);
  const [calcError,setCalcError]=useState("");
  const [rsi,setRsi]=useState(null);
  const [livePrice,setLivePrice]=useState(null);

  // Simulation sliders
  const [simSpot,setSimSpot]=useState(0);
  const [simDTE,setSimDTE]=useState(0);
  const [simIV,setSimIV]=useState(0);

  // Screener
  const [scanTicker,setScanTicker]=useState("");
  const [scanning,setScanning]=useState(false);
  const [scanData,setScanData]=useState(null);
  const [scanErr,setScanErr]=useState("");

  // Trade log
  const [trades,setTrades]=useState([]);
  const [logForm,setLogForm]=useState({ticker:"",type:"call",strike:"",entry:"",exit:"",outcome:"WIN",notes:""});

  // Signals tab
  const [signals,setSignals]=useState([]);
  const [sigScanning,setSigScanning]=useState(false);
  const [sigRegime,setSigRegime]=useState("");
  const [sigFilter,setSigFilter]=useState("ALL");

  // Scan log (save open / log 3:45 / validate)
  const [scanSaving,setScanSaving]=useState(false);
  const [scanClosing,setScanClosing]=useState(false);
  const [scanValidating,setScanValidating]=useState(false);
  const [scanSaveMsg,setScanSaveMsg]=useState(null);
  const [scanValidation,setScanValidation]=useState(null);
  const [sigError,setSigError]=useState("");
  const [sigLastScanned,setSigLastScanned]=useState(null);
  const [sigAlgo,setSigAlgo]=useState("largecap");
  const [autoRec,setAutoRec]=useState(null);
  const [autoRecSym,setAutoRecSym]=useState(null);
  const [volCheck,setVolCheck]=useState(null);
  const [volChecking,setVolChecking]=useState(false);
  const [intradayRec,setIntradayRec]=useState(null);

  const tc=optType==="call"?C.call:C.put;

  // Live simulation P&L (updates as sliders move)
  const simResult=useMemo(()=>{
    if(!result)return null;
    const{K,premium:prem,suggestedBid,nC,S1}=result;
    const prem2=result.priceAtOpen;
    const T=Math.max(simDTE/365,0.001);
    const sigma=simIV/100;
    const optP=bs(simSpot,K,T,RF_RATE,sigma,optType);
    const d=getDelta(simSpot,K,T,RF_RATE,sigma,optType);
    const g=getGamma(simSpot,K,T,RF_RATE,sigma);
    const th=getTheta(simSpot,K,T,RF_RATE,sigma,optType);
    const pnlPct=((optP-prem2)/prem2)*100;
    const pnlDollar=(optP-suggestedBid)*nC*100;
    return{optP,d,g,th,pnlPct,pnlDollar};
  },[result,simSpot,simDTE,simIV,optType]);

  // ── External symbol injection (from Today's Signals tab) ─────────────────
  useEffect(()=>{
    if(!externalSymbol)return;
    setTab(0);  // always land on PRICER when jumping from Today's Signals
    setTicker(externalSymbol);
    setIntradayRec(null);
    if(externalDirection) setOptType(externalDirection.toLowerCase()==="call"?"call":"put");
    fetchLive(externalSymbol).then(quote=>{
      if(quote&&externalTrade) runAutoRecommendFromTrade(externalTrade,quote);
    });
  },[externalSymbol,externalDirection,externalTrade]); // eslint-disable-line

  // ── Live fetch via Yahoo (api.py) ──────────────────────────────────────────
  const fetchLive=async(symOverride)=>{
    const sym=(symOverride||ticker||"").toUpperCase();
    if(!sym)return;
    setFetching(true);setFetchNote("");setFetchErr(false);setRsi(null);setLivePrice(null);
    try{
      const d=await fetchQuoteYahoo(sym);
      setClose(String(d.prevClose));
      setPreMkt(String(d.price));
      setLivePrice(d.price);
      const gapPct=((d.price-d.prevClose)/d.prevClose*100).toFixed(2);
      const direction=d.price>d.prevClose?"UP":"DOWN";
      if(d.closes?.length>=2){
        const hv=calcHV(d.closes);
        setIv(String(Math.round(hv)));
        if(d.rsi!=null) setRsi(d.rsi);
        setFetchNote(`Yahoo: $${d.price} · Prev: $${d.prevClose} · Gap: ${gapPct}% ${direction} · HV-est IV: ${Math.round(hv)}%`);
      }else{
        if(d.rsi!=null) setRsi(d.rsi);
        setFetchNote(`Yahoo: $${d.price} · Prev: $${d.prevClose} · Gap: ${gapPct}% ${direction} · set IV manually`);
      }
      setFetching(false);
      return d;
    }catch(e){
      setFetchErr(true);
      setFetchNote(`Error: ${e.message} — check ticker or ensure api.py is running`);
      setFetching(false);
      return null;
    }
  };

  // ── Intraday signal → options auto-recommend ──────────────────────────────
  const runAutoRecommendFromTrade=async(trade,quote)=>{
    const price=quote?.price||trade.entry;
    if(!price)return;
    const type=trade.direction==="CALL"?"call":"put";
    const hv=quote?.closes?.length>=2?calcHV(quote.closes):65;
    let iv2=Math.round(hv);
    const ivBump2=5;
    const prevClose=quote?.prevClose||price;
    const tradeTarget=trade.target||price;
    const tradeStop=trade.stop||price;

    // Target-informed ideal strike: 45% of the way from price to target (intraday aggression)
    const targetMove=Math.abs(tradeTarget-price);
    const idealStrike=snapToHalf(
      type==="put" ? price - targetMove*0.45 : price + targetMove*0.45
    );

    // Illiquid underlying warning
    const illiquidWarning=price<15
      ?"Stock under $15 — options likely have wide spreads, confirm liquidity on broker before entering"
      :null;

    setVolChecking(true);setIntradayRec(null);setVolCheck(null);

    const sym=trade.symbol.replace(/\.(TO|V|CN)$/,"");
    const MIN_VOL=50,MIN_OI=100,MAX_SPREAD=15;
    const isLiquid=c=>(c.volume>=MIN_VOL||c.openInterest>=MIN_OI)&&(c.spread_pct===null||c.spread_pct<=MAX_SPREAD);
    const TARGET_DELTA=0.40;

    let strike2=idealStrike;
    let chainData=null,volCheckResult=null;

    try{
      // Fetch near-term then weekly sequentially — avoids yfinance rate-limiting on concurrent calls
      const rNear=await fetch(`${API_BASE}/api/options?ticker=${sym}&dte=1`);
      const dNear=await rNear.json();
      const rWeek=await fetch(`${API_BASE}/api/options?ticker=${sym}&dte=7`);
      const dWeek=await rWeek.json();

      // Score an expiry: ATM (OI×0.6 + volume×0.4) × delta bonus (rewards delta 0.30–0.55)
      const expiryScore=(d,t)=>{
        if(d.error||!(d.calls?.length||d.puts?.length))return -1;
        const chain=t==="put"?d.puts:d.calls;
        if(!chain?.length)return -1;
        const atm=[...chain].sort((a,b)=>Math.abs(a.strike-price)-Math.abs(b.strike-price))[0];
        if(!atm)return -1;
        const dte=d.dte??1;
        const T=Math.max(dte/365,0.001);
        const sigma=atm.iv>0?atm.iv/100:0.35;
        const delta=Math.abs(getDelta(price,atm.strike,T,RF_RATE,sigma,t));
        const deltaBonus=(delta>=0.30&&delta<=0.55)?1.2:1.0;
        return((atm.openInterest||0)*0.6+(atm.volume||0)*0.4)*deltaBonus;
      };

      const scoreNear=expiryScore(dNear,type);
      const scoreWeek=expiryScore(dWeek,type);

      // Prefer near-term if it has ≥70% the liquidity score of the weekly (more gamma leverage for intraday)
      const data=(()=>{
        if(scoreNear<0&&scoreWeek<0)return dNear;
        if(scoreNear<0)return dWeek;
        if(scoreWeek<0)return dNear;
        return scoreNear>=scoreWeek*0.70?dNear:dWeek;
      })();

      if(!data.error&&(data.calls?.length||data.puts?.length)){
        chainData=data;
        const atmCall=[...(data.calls||[])].sort((a,b)=>Math.abs(a.strike-price)-Math.abs(b.strike-price))[0];
        if(atmCall?.iv>0) iv2=atmCall.iv;
        const chain=type==="put"?data.puts:data.calls;
        const T0=Math.max(((data.dte??1)-1)/365,0.001);
        const iv1base=(iv2*(1+ivBump2/100))/100;
        const otm=(chain||[]).filter(c=>type==="put"?c.strike<price:c.strike>price);
        if(otm.length>0){
          // Blend: 40% delta quality + 60% proximity to target-informed ideal strike
          const withScore=otm.map(c=>{
            const cIv=c.iv>0?(c.iv*(1+ivBump2/100))/100:iv1base;
            const cDelta=Math.abs(getDelta(price,c.strike,T0,RF_RATE,cIv,type));
            const deltaScore=Math.abs(cDelta-TARGET_DELTA);
            const targetScore=Math.abs(c.strike-idealStrike)/price;
            return{...c,calcDelta:cDelta,pickScore:deltaScore*0.40+targetScore*0.60};
          });
          withScore.sort((a,b)=>a.pickScore-b.pickScore);
          const top5=withScore.slice(0,5);
          const liquidTop=top5.filter(isLiquid);
          const chosen=liquidTop[0]??top5[0];
          strike2=chosen.strike;
          const suggest=liquidTop[0]&&liquidTop[0].strike!==strike2?liquidTop[0]:null;
          const nearby=withScore.slice(0,6);
          const exact=nearby.find(c=>c.strike===strike2);
          const spreadWide=exact?.spread_pct>18;
          volCheckResult={exact,nearby,suggest,targetOk:!!(exact&&isLiquid(exact)),
                          spreadWide,expiry:data.expiry,MIN_VOL,MIN_OI,MAX_SPREAD,
                          expiryScores:{near:{dte:dNear.dte,score:Math.round(scoreNear)},week:{dte:dWeek.dte,score:Math.round(scoreWeek)}}};
        }else{
          const byDist=[...(chain||[])].sort((a,b)=>Math.abs(a.strike-price)-Math.abs(b.strike-price));
          strike2=byDist[0]?.strike??snapToHalf(price);
          volCheckResult={error:"No OTM contracts — nearest strike used"};
        }
      }else{
        volCheckResult={error:data.error??"No options chain found — prices are BS estimates only"};
      }
    }catch(e){
      volCheckResult={error:e.message};
    }

    const actualDte=chainData?.dte??1;
    const dteLabelStr=actualDte===0?"0DTE (today)":actualDte===1?"1DTE (tomorrow)":`${actualDte}DTE`;
    const T=Math.max((actualDte-1)/365,0.001);
    const iv1=(iv2*(1+ivBump2/100))/100;
    const matched=chainData?(type==="put"?chainData.puts:chainData.calls)?.find(c=>c.strike===strike2):null;
    const optP=(matched?.mid>0)?matched.mid:bs(price,strike2,T,RF_RATE,iv1,type);
    const sugBid=(matched?.bid>0)?matched.bid:optP*0.95;
    const dVal=getDelta(price,strike2,T,RF_RATE,iv1,type);
    const thVal=getTheta(price,strike2,T,RF_RATE,iv1,type);
    const vVal=getVega(price,strike2,T,RF_RATE,iv1);
    const t15=optP*1.15,t20=optP*1.20,t25=optP*1.25;
    const absDelta=Math.abs(dVal||0.001);
    const reqMove=(optP*0.20)/absDelta;
    const reqMovePct=(reqMove/price)*100;
    const ivCrushPrice=bs(price,strike2,T,RF_RATE,iv1*0.80,type);

    // Expected value when stock hits signal target (assume ~40% of DTE used, IV unchanged for intraday)
    const T_target=Math.max((actualDte*0.40)/365,0.001);
    const valueAtTarget=bs(tradeTarget,strike2,T_target,RF_RATE,iv1,type);
    const pnlAtTarget=optP>0?((valueAtTarget-optP)/optP)*100:0;
    const targetMovePct=((tradeTarget-price)/price)*100;

    const why=[
      `Intraday signal · Grade ${trade.grade} · Score ${typeof trade.score==="number"?trade.score.toFixed(0):trade.score}%`,
      `Entry $${trade.entry?.toFixed(2)} · Stop $${tradeStop.toFixed(2)} · Target $${tradeTarget.toFixed(2)} · R:R ${trade.rr?.toFixed(1)}R`,
      `$${strike2} strike = ideal ${type==="put"?"put":"call"} for ${Math.abs(targetMovePct).toFixed(1)}% ${type==="put"?"drop":"rally"} to $${tradeTarget.toFixed(2)} · ${Math.abs(dVal).toFixed(2)}Δ`,
      `Ideal strike (45% of move): $${idealStrike.toFixed(2)} → snapped to nearest chain contract`,
    ];
    if(trade.reasons?.length) why.push(`Signal triggers: ${trade.reasons.slice(0,3).join(" · ")}`);
    if(chainData&&matched?.iv>0) why.push(`IV ${iv2.toFixed(0)}% from live chain (ATM)`);
    else why.push(`IV ${iv2}% estimated from historical volatility — set manually if off`);
    if(chainData) why.push(`Exp. ${chainData.expiry} (${dteLabelStr}) · targeted 0.40Δ blended with signal target`);
    else why.push(`No live chain — strike/price are Black-Scholes estimates, confirm on broker`);
    if(matched?.mid>0) why.push(`Live: bid $${matched.bid.toFixed(2)} / ask $${matched.ask.toFixed(2)} · mid $${matched.mid.toFixed(2)}`);
    if(actualDte<=1) why.push(`${dteLabelStr} — high gamma, rapid moves, plan exit before close`);

    setIntradayRec({trade,type,strike:strike2,idealStrike,dte:actualDte,dteLabelStr,
                    iv:iv2,ivBump:ivBump2,optPrice:optP,sugBid,delta:dVal,theta:thVal,vega:vVal,
                    t15,t20,t25,reqMove,reqMovePct,ivCrushPrice,why,price,prevClose,
                    tradeTarget,tradeStop,valueAtTarget,pnlAtTarget,targetMovePct,illiquidWarning});
    setVolCheck(volCheckResult);
    setVolChecking(false);
  };

  const loadIntradayRecIntoPricer=()=>{
    if(!intradayRec)return;
    const{trade,type,strike:st,dte:dt,iv:iv2,ivBump:ivB,optPrice:op,price,prevClose}=intradayRec;
    setTicker(trade.symbol);
    setClose(String(prevClose||price));
    setPreMkt(String(price));
    setOptType(type);
    setStrike(String(st));
    setDte(String(dt));
    setIv(String(iv2));
    setIvBump(String(ivB));
    setOptPrice(fmtN(op,2));
    setLivePrice(price);
    setFetchNote(`Intraday loaded: ${trade.symbol} ${type.toUpperCase()} $${st} ${dt}DTE — Grade ${trade.grade} · Score ${trade.score?.toFixed?trade.score.toFixed(0):trade.score}%`);
    const nC=Math.max(1,parseInt(contracts)||1);
    const r=compute(prevClose||price,price,st,dt,iv2,ivB,op,type,nC);
    setResult(r);
    setSimSpot(r.S1);setSimDTE(dt-1||0);setSimIV(r.ivAtOpen);
  };

  // ── Screener ───────────────────────────────────────────────────────────────
  const runScan=async()=>{
    if(!scanTicker)return;
    setScanning(true);setScanData(null);setScanErr("");
    try{
      const d=await fetchQuoteYahoo(scanTicker);
      const gapPct=((d.price-d.prevClose)/d.prevClose)*100;
      const hv=d.closes?.length>=2?calcHV(d.closes):65;
      const rsiVal=d.closes?.length>=15?calcRSI([...d.closes,d.price]):d.rsi??null;
      setScanData({ticker:scanTicker.toUpperCase(),quote:{current:d.price,prevClose:d.prevClose},hv,rsi:rsiVal,gapPct});
    }catch(e){
      setScanErr(e.message);
    }
    setScanning(false);
  };

  const loadFromScan=()=>{
    if(!scanData)return;
    setTicker(scanData.ticker);
    setClose(String(scanData.quote.prevClose));
    setPreMkt(String(scanData.quote.current));
    setIv(String(Math.round(scanData.hv)));
    setOptType(scanData.gapPct<0?"call":"put");
    setRsi(scanData.rsi);
    setTab(0);
  };

  const calculate=()=>{
    const nC=Math.max(1,parseInt(contracts)||1);
    const fields=[
      [+close,    "Prev Close"],
      [+preMkt,   "Current / Pre-Mkt"],
      [+strike,   "Strike"],
      [+dte,      "DTE"],
      [+iv,       "IV %"],
      [+optPrice, "Option Price"],
    ];
    const missing=fields.filter(([v])=>isNaN(v)||v<=0).map(([,n])=>n);
    if(missing.length){
      setCalcError(`Missing / zero: ${missing.join(", ")}`);
      return;
    }
    setCalcError("");
    const r=compute(+close,+preMkt,+strike,+dte,+iv,+ivBump,+optPrice,optType,nC);
    setResult(r);
    setSimSpot(r.S1);
    setSimDTE(parseFloat(dte)-1||0);
    setSimIV(r.ivAtOpen);
  };

  const logTrade=()=>{
    if(!logForm.ticker||!logForm.entry||!logForm.exit)return;
    const pnlPct=fmtN(((+logForm.exit-+logForm.entry)/+logForm.entry)*100);
    setTrades(p=>[{...logForm,pnlPct,id:Date.now(),date:new Date().toLocaleDateString()},...p]);
    setLogForm({ticker:"",type:"call",strike:"",entry:"",exit:"",outcome:"WIN",notes:""});
  };

  // ── Scan log helpers ──────────────────────────────────────────────────────
  const saveScanOpen=async()=>{
    const toSave=signals.filter(s=>s.signal!=="SKIP");
    if(!toSave.length){setScanSaveMsg({ok:false,text:"Run a scan first"});return;}
    setScanSaving(true);setScanSaveMsg(null);
    try{
      const r=await fetch(`${API_BASE}/api/save_scan_signals`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({signals:toSave,algo:sigAlgo}),
      });
      const d=await r.json();
      if(d.error)setScanSaveMsg({ok:false,text:d.error});
      else setScanSaveMsg({ok:true,text:`Saved ${d.saved} signals at ${d.time} — open prices locked in`});
    }catch{setScanSaveMsg({ok:false,text:"api.py not reachable"});}
    setScanSaving(false);
  };

  const scanLogClose=async()=>{
    setScanClosing(true);setScanSaveMsg(null);
    try{
      const r=await fetch(`${API_BASE}/api/scan_log_close`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({algo:sigAlgo}),
      });
      const d=await r.json();
      if(d.error)setScanSaveMsg({ok:false,text:d.error});
      else setScanSaveMsg({ok:true,text:`3:45 close logged at ${d.time} — ${d.updated} prices captured`});
    }catch{setScanSaveMsg({ok:false,text:"api.py not reachable"});}
    setScanClosing(false);
  };

  const runScanValidate=async()=>{
    setScanValidating(true);setScanValidation(null);
    try{
      const r=await fetch(`${API_BASE}/api/scan_validate?algo=${sigAlgo}`);
      const d=await r.json();
      setScanValidation(d);
    }catch{setScanValidation({error:"api.py not reachable"});}
    setScanValidating(false);
  };

  // ── Large Cap Signals API ──────────────────────────────────────────────────
  const runSignalScan=async()=>{
    setSigScanning(true);setSigError("");setSignals([]);setSigRegime("");
    try{
      const res=await fetch(`${API_BASE}/api/scan?algo=${sigAlgo}`);
      if(!res.ok)throw new Error(`Server returned ${res.status}`);
      const data=await res.json();
      if(data.error)throw new Error(data.error);
      setSignals(data.signals||[]);
      setSigRegime(data.regime||"");
      setSigLastScanned(new Date().toLocaleTimeString());
    }catch(e){
      setSigError(e.message+" — make sure python api.py is running on port 5000");
    }
    setSigScanning(false);
  };

  const loadFromSignal=(sig)=>{
    setTicker(sig.symbol);
    setClose(String(sig.price??0));
    setPreMkt(String(sig.price??0));
    setStrike("");setResult(null);setFetchErr(false);
    setLivePrice(sig.price??null);
    setRsi(typeof sig.rsi_d==="number"?sig.rsi_d:null);
    setFetchNote(`Signal loaded: ${sig.symbol} · $${sig.price} · ${sig.signal} (score ${sig.score}) · Click ⚡ FETCH LIVE to refresh`);
    setTab(0);
  };

  const runAutoRecommend=async(sig)=>{
    const price=sig.price;
    if(!price)return;
    const gap=sig.change??0;
    setVolChecking(true);setAutoRec(null);setAutoRecSym(sig.symbol);setVolCheck(null);

    const isGapFade=Math.abs(gap)>=5;
    let type;
    if(isGapFade){
      type=gap>0?"put":"call";
    }else{
      const emaDown=sig.ema9&&sig.ema21&&sig.ema9<sig.ema21;
      const rsiHot=sig.rsi_d&&sig.rsi_d>68;
      const rsiLow=sig.rsi_d&&sig.rsi_d<35;
      if(rsiLow&&!emaDown) type="call";
      else if(emaDown||rsiHot) type="put";
      else type=sig.signal==="BUY"?"call":"put";
    }

    const tradeTarget=sig.target||price;
    const tradeStop=sig.stop||price;
    const expectedMovePct=Math.abs((tradeTarget-price)/price)*100;

    // DTE: factor both score and expected move magnitude
    // Bigger expected move → shorter DTE needed (move happens faster or not at all)
    let dte2;
    if(isGapFade){ dte2=1; }
    else if(expectedMovePct>=8||sig.score>=8.0){ dte2=7; }   // big move, capture fast
    else if(expectedMovePct>=5||sig.score>=7.0){ dte2=14; }
    else if(expectedMovePct>=3||sig.score>=6.0){ dte2=21; }
    else{ dte2=30; }
    if(typeof sig.earnings_days==="number"&&sig.earnings_days>=0&&sig.earnings_days<dte2)
      dte2=sig.earnings_days+7;

    // Target-informed ideal strike: 35% of the way from price to target (swing conservative)
    const targetMove=Math.abs(tradeTarget-price);
    const idealStrike=snapToHalf(
      type==="put" ? price - targetMove*0.35 : price + targetMove*0.35
    );

    // Illiquid warning
    const illiquidWarning=price<15
      ?"Stock under $15 — options likely illiquid, wide spreads. Consider trading the stock directly"
      :null;

    const SECTOR_IV={semis:65,megacap:45,cloud_saas:58,tsx_banks:25,tsx_energy:35,
                     tsx_gold:40,tsx_telecom:22,consumer:32};
    const _fallbackIV=()=>{
      let v=SECTOR_IV[sig.sector]??45;
      if(sig.vol_spike>=2) v=Math.round(v*1.1);
      if(isGapFade)        v=Math.round(v*1.15);
      return v;
    };
    const ivBump2=isGapFade?15:5;

    const sym=sig.symbol.replace(/\.(TO|V|CN)$/,"");
    const MIN_VOL=50,MIN_OI=100,MAX_SPREAD=15;
    const isLiquid=c=>(c.volume>=MIN_VOL||c.openInterest>=MIN_OI)&&(c.spread_pct===null||c.spread_pct<=MAX_SPREAD);
    const TARGET_DELTA=0.35;

    let iv2=_fallbackIV();
    let strike2=idealStrike;
    let chainData=null, volCheckResult=null;

    try{
      const res=await fetch(`${API_BASE}/api/options?ticker=${sym}&dte=${dte2}`);
      const data=await res.json();
      if(!data.error&&(data.calls?.length||data.puts?.length)){
        chainData=data;
        dte2=data.dte??dte2;
        const atmCall=[...(data.calls||[])].sort((a,b)=>Math.abs(a.strike-price)-Math.abs(b.strike-price))[0];
        if(atmCall?.iv>0) iv2=atmCall.iv;
        const chain=type==="put"?data.puts:data.calls;
        const T0=Math.max((dte2-1)/365,0.001);
        const iv1base=(iv2*(1+ivBump2/100))/100;
        const otm=(chain||[]).filter(c=>type==="put"?c.strike<price:c.strike>price);
        if(otm.length>0){
          // Blend: 40% delta quality + 60% proximity to target-informed ideal strike
          const withScore=otm.map(c=>{
            const cIv=c.iv>0?(c.iv*(1+ivBump2/100))/100:iv1base;
            const cDelta=Math.abs(getDelta(price,c.strike,T0,RF_RATE,cIv,type));
            const deltaScore=Math.abs(cDelta-TARGET_DELTA);
            const targetScore=Math.abs(c.strike-idealStrike)/price;
            return{...c,calcDelta:cDelta,pickScore:deltaScore*0.40+targetScore*0.60};
          });
          withScore.sort((a,b)=>a.pickScore-b.pickScore);
          const top5=withScore.slice(0,5);
          const liquidTop=top5.filter(isLiquid);
          const chosen=liquidTop[0]??top5[0];
          strike2=chosen.strike;
          const suggest=liquidTop[0]&&liquidTop[0].strike!==strike2?liquidTop[0]:null;
          const nearby=withScore.slice(0,6);
          const exact=nearby.find(c=>c.strike===strike2);
          const spreadWide=exact?.spread_pct>18;
          volCheckResult={exact,nearby,suggest,targetOk:!!(exact&&isLiquid(exact)),
                          spreadWide,expiry:data.expiry,MIN_VOL,MIN_OI,MAX_SPREAD};
        }else{
          const byDist=[...(chain||[])].sort((a,b)=>Math.abs(a.strike-price)-Math.abs(b.strike-price));
          strike2=byDist[0]?.strike??snapToHalf(price);
          volCheckResult={error:"No OTM contracts for this expiry — nearest strike used"};
        }
      }else{
        volCheckResult={error:data.error??"No options chain found — prices are BS estimates only"};
      }
    }catch(e){
      volCheckResult={error:e.message};
    }

    const T=Math.max((dte2-1)/365,0.001);
    const iv1=(iv2*(1+ivBump2/100))/100;
    const matched=chainData?(type==="put"?chainData.puts:chainData.calls)?.find(c=>c.strike===strike2):null;
    const optP=(matched?.mid>0)?matched.mid:bs(price,strike2,T,RF_RATE,iv1,type);
    const sugBid=(matched?.bid>0)?matched.bid:optP*0.95;
    const dVal=getDelta(price,strike2,T,RF_RATE,iv1,type);
    const thVal=getTheta(price,strike2,T,RF_RATE,iv1,type);
    const vVal=getVega(price,strike2,T,RF_RATE,iv1);
    const t15=optP*1.15,t20=optP*1.20,t25=optP*1.25;
    const absDelta=Math.abs(dVal||0.001);
    const reqMove=(optP*0.20)/absDelta;
    const reqMovePct=(reqMove/price)*100;
    const ivCrushPrice=bs(price,strike2,T,RF_RATE,iv1*0.80,type);

    // Expected value when stock hits signal target (assume 50% of DTE used, mild IV compression)
    const T_target=Math.max((dte2*0.50)/365,0.001);
    const valueAtTarget=bs(tradeTarget,strike2,T_target,RF_RATE,iv1*0.88,type);
    const pnlAtTarget=optP>0?((valueAtTarget-optP)/optP)*100:0;

    const why=[];
    if(isGapFade) why.push(`Gap ${gap>0?"up":"down"} ${Math.abs(gap).toFixed(1)}% — ${type==="put"?"fade reversal":"bounce play"}`);
    else why.push(`${sig.signal} signal · score ${sig.score?.toFixed(1)} · target ${expectedMovePct.toFixed(1)}% ${type==="put"?"drop":"rally"} to $${tradeTarget.toFixed(2)}`);
    why.push(`Ideal strike (35% of move): $${idealStrike.toFixed(2)} → snapped to nearest chain contract`);
    if(sig.rsi_d!=null) why.push(`RSI-D ${sig.rsi_d} — ${sig.rsi_d<35?"oversold ▲ call lean":sig.rsi_d>68?"overbought ▼ put lean":"in range"}`);
    if(sig.ema9&&sig.ema21) why.push(`EMA9 ${sig.ema9>sig.ema21?">":"<"} EMA21 (${sig.ema9>sig.ema21?"bullish":"bearish"} short-term trend)`);
    if(sig.vol_spike>=2) why.push(`Vol spike ${sig.vol_spike}x — elevated conviction`);
    if(sig.cmf20!=null) why.push(`CMF ${sig.cmf20>0?"positive":"negative"} (${sig.cmf20.toFixed(2)}) — money flow ${sig.cmf20>0?"in":"out"}`);
    if(chainData&&matched?.iv>0) why.push(`IV ${iv2.toFixed(0)}% from live chain (ATM)`);
    else why.push(`IV ${iv2}% estimated by sector — set manually if off`);
    if(chainData) why.push(`$${strike2} strike = ${Math.abs(dVal).toFixed(2)}Δ · 0.35Δ target blended with signal move · exp. ${chainData.expiry} (${dte2}DTE)`);
    else why.push(`$${strike2} strike snapped to $0.50 grid (no chain) — do not trade without confirming`);
    if(matched?.mid>0) why.push(`Live: bid $${matched.bid.toFixed(2)} / ask $${matched.ask.toFixed(2)} · mid $${matched.mid.toFixed(2)}`);

    setAutoRec({sig,type,strike:strike2,idealStrike,dte:dte2,iv:iv2,ivBump:ivBump2,optPrice:optP,sugBid,
                delta:dVal,theta:thVal,vega:vVal,t15,t20,t25,reqMove,reqMovePct,
                ivCrushPrice,why,isGapFade,tradeTarget,tradeStop,
                valueAtTarget,pnlAtTarget,expectedMovePct,illiquidWarning});
    setVolCheck(volCheckResult);
    setVolChecking(false);
  };

  const loadAutoRecIntoPricer=()=>{
    if(!autoRec)return;
    const{sig,type,strike:st,dte:dt,iv:iv2,ivBump:ivB,optPrice:op}=autoRec;
    setTicker(sig.symbol);
    setClose(String(sig.price));
    setPreMkt(String(sig.price));
    setOptType(type);
    setStrike(String(st));
    setDte(String(dt));
    setIv(String(iv2));
    setIvBump(String(ivB));
    setOptPrice(fmtN(op,2));
    setLivePrice(sig.price);
    setRsi(sig.rsi_d??null);
    setFetchNote(`Auto-loaded: ${sig.symbol} ${type.toUpperCase()} $${st} ${dt}DTE — score ${sig.score} · strike from live chain`);
    const nC=Math.max(1,parseInt(contracts)||1);
    const r=compute(sig.price,sig.price,st,dt,iv2,ivB,op,type,nC);
    setResult(r);
    setSimSpot(r.S1); setSimDTE(dt-1||0); setSimIV(r.ivAtOpen);
    setTab(0);
  };

  const wins=trades.filter(t=>t.outcome==="WIN").length;
  const avgPnl=trades.length?(trades.reduce((a,t)=>a+(+t.pnlPct),0)/trades.length).toFixed(1):"—";

  return(
    <div style={{minHeight:"100vh",background:C.bg,color:C.text,fontFamily:mono}}>
      <link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@300;400;500&family=Bebas+Neue&display=swap" rel="stylesheet"/>

      {/* Header */}
      <div style={{borderBottom:`1px solid ${C.border}`,padding:"14px 24px",display:"flex",alignItems:"center",gap:10,background:C.bg,position:"sticky",top:0,zIndex:20}}>
        <span style={{fontFamily:disp,fontSize:24,color:C.text,letterSpacing:"0.06em"}}>GAP FADE</span>
        <span style={{fontFamily:disp,fontSize:24,color:C.accent,letterSpacing:"0.06em"}}>ALGO</span>
        <Tag text="PUTS" col={C.put}/><Tag text="CALLS" col={C.call}/><Tag text="YAHOO LIVE" col={C.accent}/>
        <div style={{marginLeft:"auto",display:"flex",gap:6}}>
          {TABS.map((t,i)=>(
            <button key={t} onClick={()=>setTab(i)} style={{
              background:tab===i?C.accent:"transparent",color:tab===i?C.bg:C.muted,
              border:`1px solid ${tab===i?C.accent:C.border}`,borderRadius:4,
              padding:"5px 14px",fontSize:9,fontFamily:mono,letterSpacing:"0.1em",cursor:"pointer",textTransform:"uppercase"
            }}>{t}</button>
          ))}
        </div>
      </div>

      <div style={{padding:"20px 24px",maxWidth:980,margin:"0 auto"}}>

        {/* ── PRICER ── */}
        {tab===0&&(
          <div style={{display:"flex",flexDirection:"column",gap:14}}>

            {/* Intraday auto-recommend panel */}
            {(intradayRec||volChecking)&&(()=>{
              if(volChecking&&!intradayRec){
                return(
                  <div style={{background:"#060606",border:`1px solid ${C.accent}44`,borderRadius:8,padding:"14px 18px",display:"flex",alignItems:"center",gap:10}}>
                    <div style={{width:8,height:8,borderRadius:"50%",background:C.accent,animation:"pulse 1s infinite"}}/>
                    <span style={{fontSize:10,color:C.accent,letterSpacing:"0.08em"}}>FETCHING OPTIONS CHAIN &amp; BUILDING RECOMMENDATION…</span>
                  </div>
                );
              }
              if(!intradayRec)return null;
              const rc=intradayRec;
              const tc2=rc.type==="call"?C.call:C.put;
              const gradeColor={S:C.gold,A:C.call,B:C.accent,C:C.muted}[rc.trade?.grade]||C.muted;
              return(
                <div style={{background:"#060606",border:`2px solid ${tc2}`,borderRadius:8,padding:"16px 18px",position:"relative"}}>
                  <button onClick={()=>setIntradayRec(null)} style={{position:"absolute",top:10,right:14,background:"transparent",border:"none",color:C.muted,cursor:"pointer",fontSize:16,lineHeight:1}}>✕</button>

                  {/* Header */}
                  <div style={{marginBottom:12}}>
                    <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:6,flexWrap:"wrap"}}>
                      <span style={{fontSize:8,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase"}}>⚡ INTRADAY AUTO-RECOMMEND</span>
                      <span style={{background:gradeColor+"22",color:gradeColor,border:`1px solid ${gradeColor}55`,borderRadius:3,padding:"1px 7px",fontSize:10,fontWeight:700,fontFamily:"monospace"}}>
                        {rc.trade?.grade}
                      </span>
                      <Tag text={rc.type==="call"?"▲ CALL":"▼ PUT"} col={tc2}/>
                      <Tag text={rc.dteLabelStr||`${rc.dte}DTE`} col={C.orange}/>
                    </div>
                    <div style={{display:"flex",alignItems:"baseline",gap:10,flexWrap:"wrap"}}>
                      <span style={{fontFamily:disp,fontSize:26,color:tc2,letterSpacing:"0.04em"}}>
                        {rc.trade?.symbol} ${rc.strike} {rc.type==="call"?"C":"P"}
                      </span>
                      <span style={{fontSize:11,color:C.muted,fontFamily:mono}}>
                        stock @ ${rc.price?.toFixed(2)} · IV {rc.iv}% · {rc.dteLabelStr||`${rc.dte}DTE`}
                      </span>
                    </div>
                  </div>

                  {/* No options chain — show stock-only banner and skip BS stats */}
                  {volCheck?.error&&/no options chain|no options|not found/i.test(volCheck.error)&&(
                    <div style={{padding:"14px 16px",background:"#ef444411",border:`1px solid #ef444466`,borderRadius:6,marginBottom:10}}>
                      <div style={{fontSize:11,color:"#ef4444",fontWeight:700,marginBottom:6}}>⛔ No Listed Options Found</div>
                      <div style={{fontSize:10,color:"#aaa",lineHeight:1.6}}>
                        {rc.trade?.symbol} has no options chain available on the live feed — this stock is likely not optionable or is too illiquid for options.
                        All prices below are Black-Scholes estimates that do not correspond to a real contract.<br/>
                        <span style={{color:"#22c55e",fontWeight:600}}>Trade the stock signal directly:</span>{" "}
                        Buy at ${rc.trade?.entry?.toFixed(2)} · Stop ${rc.tradeStop?.toFixed(2)} · Target ${rc.tradeTarget?.toFixed(2)} · {rc.trade?.rr?.toFixed(1)}R
                      </div>
                    </div>
                  )}

                  {/* Illiquid / spread warning */}
                  {(rc.illiquidWarning||volCheck?.spreadWide)&&(
                    <div style={{padding:"8px 12px",background:"#ff990011",border:`1px solid ${C.orange}44`,borderRadius:5,marginBottom:10,fontSize:9,color:C.orange}}>
                      ⚠ {rc.illiquidWarning||(volCheck?.spreadWide?"Spread > 18% on this strike — use limit orders, not market orders":"")}
                    </div>
                  )}

                  {/* Key stats */}
                  <div style={{display:"grid",gridTemplateColumns:"repeat(5,1fr)",gap:8,marginBottom:10}}>
                    {[
                      ["Est. Price",`$${fmtN(rc.optPrice,2)}`,true],
                      ["Suggested Bid",`$${fmtN(rc.sugBid,2)}`,true],
                      ["Delta",fmtN(rc.delta,3),false],
                      ["Theta/Day",`$${fmtN(rc.theta,3)}`,false],
                      ["Vega/1%IV",`$${fmtN(rc.vega,3)}`,false],
                    ].map(([l,v,a])=>(
                      <Stat key={l} label={l} value={v} accent={a} col={tc2}/>
                    ))}
                  </div>

                  {/* If target hit — the key new stat */}
                  <div style={{padding:"10px 14px",background:tc2+"11",border:`1px solid ${tc2}44`,borderRadius:6,marginBottom:10}}>
                    <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",flexWrap:"wrap",gap:8}}>
                      <div>
                        <div style={{fontSize:8,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase",marginBottom:3}}>
                          IF SIGNAL TARGET HIT (${rc.tradeTarget?.toFixed(2)} · {Math.abs(rc.targetMovePct||0).toFixed(1)}% {rc.type==="call"?"rally":"drop"})
                        </div>
                        <div style={{display:"flex",gap:16,alignItems:"baseline",flexWrap:"wrap"}}>
                          <span style={{fontSize:20,color:tc2,fontFamily:mono,fontWeight:700}}>${fmtN(rc.valueAtTarget,2)}</span>
                          <span style={{fontSize:14,color:rc.pnlAtTarget>=0?tc2:C.put,fontFamily:mono,fontWeight:600}}>
                            {rc.pnlAtTarget>=0?"+":""}{fmtN(rc.pnlAtTarget,0)}%
                          </span>
                          <span style={{fontSize:9,color:C.muted}}>per contract: {rc.pnlAtTarget>=0?"+$":"-$"}{Math.abs((rc.valueAtTarget-rc.optPrice)*100).toFixed(0)}</span>
                        </div>
                      </div>
                      <div style={{fontSize:8,color:"#444",textAlign:"right"}}>
                        <div>Stop hit → ~$0 (full loss)</div>
                        <div style={{marginTop:2}}>Ideal strike: ${rc.idealStrike?.toFixed(2)}</div>
                      </div>
                    </div>
                  </div>

                  {/* Signal levels */}
                  <div style={{display:"grid",gridTemplateColumns:"repeat(3,1fr)",gap:8,marginBottom:10}}>
                    {[
                      ["Signal Entry",`$${rc.trade?.entry?.toFixed(2)}`,"#666"],
                      ["Signal Stop",`$${rc.tradeStop?.toFixed(2)}`,C.put],
                      ["Signal Target",`$${rc.tradeTarget?.toFixed(2)}`,C.call],
                    ].map(([l,v,c])=>(
                      <div key={l} style={{background:C.bg,border:`1px solid ${C.border}`,borderRadius:5,padding:"8px 10px"}}>
                        <div style={{fontSize:8,color:C.muted,textTransform:"uppercase",letterSpacing:"0.1em"}}>{l}</div>
                        <div style={{fontSize:15,color:c,fontFamily:mono,fontWeight:600}}>{v}</div>
                      </div>
                    ))}
                  </div>

                  {/* Option targets + move needed */}
                  <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:8,marginBottom:10}}>
                    {[["+ 15%",rc.t15],["+ 20%",rc.t20],["+ 25%",rc.t25]].map(([l,v])=>(
                      <div key={l} style={{background:C.bg,border:`1px solid ${C.border}`,borderRadius:5,padding:"8px 10px"}}>
                        <div style={{fontSize:8,color:C.muted,textTransform:"uppercase",letterSpacing:"0.1em"}}>{l} option gain</div>
                        <div style={{fontSize:16,color:tc2,fontFamily:mono,fontWeight:600}}>${fmtN(v,2)}</div>
                      </div>
                    ))}
                    <div style={{background:C.bg,border:`1px solid #ff666622`,borderRadius:5,padding:"8px 10px"}}>
                      <div style={{fontSize:8,color:"#ff6666",textTransform:"uppercase",letterSpacing:"0.1em"}}>⚠ IV Crush</div>
                      <div style={{fontSize:16,color:C.orange,fontFamily:mono,fontWeight:600}}>${fmtN(rc.ivCrushPrice,2)}</div>
                      <div style={{fontSize:8,color:rc.ivCrushPrice>rc.sugBid?C.call:C.put,marginTop:2}}>
                        {rc.ivCrushPrice>rc.sugBid?"survives ✓":"at risk ✗"}
                      </div>
                    </div>
                  </div>

                  {/* Volume check */}
                  {volCheck&&(
                    <div style={{marginBottom:10,padding:"10px 12px",background:C.bg,borderRadius:5,border:`1px solid ${
                      volCheck.error?"#333":volCheck.targetOk?C.call+"44":C.put+"44"}`}}>
                      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:6}}>
                        <div style={{fontSize:8,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase"}}>OPTIONS VOLUME CHECK</div>
                        {!volCheck.error&&<Tag text={volCheck.targetOk?"LIQUID ✓":"LOW VOLUME ✗"} col={volCheck.targetOk?C.call:C.put}/>}
                      </div>
                      {volCheck.error&&<div style={{fontSize:9,color:"#555"}}>⚠ {volCheck.error}</div>}
                      {!volCheck.error&&(()=>{
                        const c=volCheck.exact;
                        return(<>
                          <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:6,marginBottom:6}}>
                            {[
                              ["Strike",`$${rc.strike}`,true],
                              ["Volume",c?(c.volume||0).toLocaleString():"—",!!(c&&c.volume>=volCheck.MIN_VOL)],
                              ["Open Int.",c?(c.openInterest||0).toLocaleString():"—",!!(c&&c.openInterest>=volCheck.MIN_OI)],
                              ["Spread",c&&c.spread_pct!=null?`${c.spread_pct}%`:"—",!!(c&&c.spread_pct<=volCheck.MAX_SPREAD)],
                            ].map(([l,v,ok])=>(
                              <div key={l} style={{background:C.panel,borderRadius:4,padding:"5px 7px",border:`1px solid ${ok?C.call+"33":C.put+"33"}`}}>
                                <div style={{fontSize:7,color:C.muted,textTransform:"uppercase",letterSpacing:"0.08em"}}>{l}</div>
                                <div style={{fontSize:12,color:ok?C.call:C.put,fontFamily:mono,fontWeight:600}}>{v}</div>
                              </div>
                            ))}
                          </div>
                          {volCheck.expiryScores&&(
                            <div style={{display:"flex",gap:6,marginBottom:volCheck.suggest?8:0,fontSize:8,color:C.muted}}>
                              <span>Expiry selected:</span>
                              <span style={{color:C.text,fontFamily:mono}}>{volCheck.expiry} ({rc.dte}DTE)</span>
                              <span>·</span>
                              <span>1DTE liq score: <span style={{color:C.accent,fontFamily:mono}}>{volCheck.expiryScores.near.score??'—'}</span></span>
                              <span>·</span>
                              <span>7DTE liq score: <span style={{color:C.accent,fontFamily:mono}}>{volCheck.expiryScores.week.score??'—'}</span></span>
                            </div>
                          )}
                          {volCheck.suggest&&(
                            <div style={{padding:"7px 10px",background:"#c8ff0011",border:`1px solid ${C.accent}44`,borderRadius:4,display:"flex",justifyContent:"space-between",alignItems:"center"}}>
                              <div>
                                <div style={{fontSize:8,color:C.accent,marginBottom:2}}>⚡ MORE LIQUID ALTERNATIVE</div>
                                <div style={{fontSize:10,color:C.text,fontFamily:mono}}>
                                  ${volCheck.suggest.strike} {rc.type.toUpperCase()} · Vol: {(volCheck.suggest.volume||0).toLocaleString()} · OI: {(volCheck.suggest.openInterest||0).toLocaleString()}
                                </div>
                              </div>
                              <Btn small col={C.accent} onClick={()=>{
                                const s=volCheck.suggest;
                                const newT=Math.max((rc.dte-1)/365,0.001);
                                const newIv1=(rc.iv*(1+rc.ivBump/100))/100;
                                const newOpt=(s.mid>0)?s.mid:bs(rc.price,s.strike,newT,RF_RATE,newIv1,rc.type);
                                const newBid=(s.bid>0)?s.bid:newOpt*0.95;
                                setIntradayRec(r=>({...r,strike:s.strike,optPrice:newOpt,sugBid:newBid,
                                  delta:getDelta(rc.price,s.strike,newT,RF_RATE,newIv1,rc.type),
                                  t15:newOpt*1.15,t20:newOpt*1.20,t25:newOpt*1.25}));
                              }}>USE THIS</Btn>
                            </div>
                          )}
                        </>);
                      })()}
                    </div>
                  )}

                  {/* Why section */}
                  <div style={{marginBottom:12,padding:"10px 12px",background:C.bg,borderRadius:5,border:`1px solid ${C.border}`}}>
                    <div style={{fontSize:8,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase",marginBottom:6}}>WHY THIS CONTRACT</div>
                    {rc.why.map((w,i)=>(
                      <div key={i} style={{fontSize:9,color:"#888",marginBottom:4,display:"flex",gap:6}}>
                        <span style={{color:tc2}}>›</span><span>{w}</span>
                      </div>
                    ))}
                  </div>

                  <Btn col={tc2} onClick={loadIntradayRecIntoPricer}>↗ LOAD INTO PRICER &amp; CALCULATE</Btn>
                </div>
              );
            })()}

            <div style={{display:"flex",gap:8,alignItems:"center"}}>
              <span style={{fontSize:9,color:C.muted,letterSpacing:"0.1em",textTransform:"uppercase"}}>Option Type:</span>
              {["call","put"].map(t=>(
                <button key={t} onClick={()=>{setOptType(t);setResult(null);}} style={{
                  background:optType===t?(t==="call"?C.call:C.put):"transparent",
                  color:optType===t?C.bg:(t==="call"?C.call:C.put),
                  border:`1px solid ${t==="call"?C.call:C.put}`,
                  borderRadius:4,padding:"7px 22px",fontSize:11,
                  fontFamily:mono,letterSpacing:"0.1em",cursor:"pointer",textTransform:"uppercase",fontWeight:600
                }}>{t==="call"?"▲ CALL":"▼ PUT"}</button>
              ))}
              <span style={{fontSize:9,color:tc,marginLeft:4}}>
                {optType==="call"?"Bounce play — stock gapped DOWN":"Fade play — stock gapped UP"}
              </span>
            </div>

            {/* Ticker + live fetch */}
            <div style={{display:"grid",gridTemplateColumns:"140px 1fr",gap:10,alignItems:"end"}}>
              <Field label="Ticker">
                <input value={ticker} onChange={e=>setTicker(e.target.value.toUpperCase())} placeholder="RBLX"
                  style={{...baseInp,color:tc,fontSize:16,letterSpacing:"0.1em"}}
                  onFocus={e=>e.target.style.borderColor=tc}
                  onBlur={e=>e.target.style.borderColor=C.border}
                  onKeyDown={e=>e.key==="Enter"&&fetchLive()}/>
              </Field>
              <Field label="Live Data (Yahoo Finance)" hint={fetchNote||"Pulls price via api.py — make sure it's running"}>
                <div style={{display:"flex",gap:8,alignItems:"center"}}>
                  <Btn onClick={fetchLive} loading={fetching} col={tc}>⚡ FETCH LIVE</Btn>
                  {livePrice&&<span style={{fontSize:12,color:C.accent,fontFamily:mono}}>Live: ${livePrice}</span>}
                  {fetchErr&&<span style={{fontSize:9,color:C.put}}>↑ check ticker or start api.py</span>}
                </div>
              </Field>
            </div>

            {rsi!==null&&<RSIBar rsi={rsi}/>}

            {/* Inputs */}
            <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:10}}>
              <Field label="Prev Close" hint="Auto-filled from Yahoo"><Inp value={close} onChange={setClose} placeholder="55.26"/></Field>
              <Field label="Current / Pre-Mkt" hint="Live price from Yahoo">
                <Inp value={preMkt} onChange={setPreMkt} placeholder="41.50" col={tc}/>
              </Field>
              <Field label={optType==="call"?"Call Strike":"Put Strike"} hint="Your target strike">
                <Inp value={strike} onChange={setStrike} placeholder={optType==="call"?"60":"173"}/>
              </Field>
              <Field label="DTE" hint="Days to expiry"><Inp value={dte} onChange={setDte} placeholder="1" step="1"/></Field>
              <Field label="IV %" hint="Auto-estimated from HV · bump for earnings"><Inp value={iv} onChange={setIv} placeholder="65"/></Field>
              <Field label="IV Bump %" hint="Expected spike at open"><Inp value={ivBump} onChange={setIvBump} placeholder="10"/></Field>
              <Field label="Option Price" hint="Current mark / last trade"><Inp value={optPrice} onChange={setOptPrice} placeholder="0.80"/></Field>
              <Field label="Contracts" hint="Position size"><Inp value={contracts} onChange={setContracts} placeholder="1" step="1"/></Field>
            </div>

            <div style={{display:"flex",alignItems:"center",gap:12}}>
              <Btn onClick={calculate} col={tc}>CALCULATE EDGE</Btn>
              {calcError&&<span style={{fontSize:9,color:C.put,fontFamily:mono}}>⚠ {calcError}</span>}
            </div>

            {result&&(
              <div style={{display:"flex",flexDirection:"column",gap:10}}>

                {/* Signal bar */}
                <div style={{display:"flex",alignItems:"center",gap:10,padding:"10px 14px",background:C.panel,borderRadius:6,border:`1px solid ${result.status==="FAVORABLE"?tc+"44":result.status==="UNFAVORABLE"?C.put+"33":C.border}`}}>
                  <div style={{width:7,height:7,borderRadius:"50%",background:result.status==="FAVORABLE"?tc:result.status==="UNFAVORABLE"?C.put:C.muted}}/>
                  <span style={{fontSize:10,color:result.status==="FAVORABLE"?tc:result.status==="UNFAVORABLE"?C.put:C.muted,letterSpacing:"0.08em"}}>
                    {result.type==="call"
                      ?result.status==="FAVORABLE"?`BOUNCE PLAY: ${fmtN(Math.abs(result.gapPct))}% gap down — call priced for recovery`:"CAUTION: Call may be overpriced"
                      :result.status==="FAVORABLE"?`FADE SIGNAL: ${fmtN(result.gapPct)}% gap up — put priced for reversal`:"CAUTION: Put may be overpriced"}
                  </span>
                  {ticker&&<span style={{marginLeft:"auto",fontFamily:disp,fontSize:18,color:tc,letterSpacing:"0.06em"}}>{ticker}</span>}
                </div>

                {/* Core 4 */}
                <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:10}}>
                  <Stat label={result.type==="call"?"Gap Down":"Gap Up"} value={`${fmtN(Math.abs(result.gapPct))}%`} accent col={tc}/>
                  <Stat label="Est. Option at Open" value={fmtC(result.priceAtOpen)} accent sub="Black-Scholes + IV bump" col={tc}/>
                  <Stat label="Suggested Bid" value={fmtC(result.suggestedBid)} accent sub="5% under theoretical" col={tc}/>
                  <Stat label="Delta at Open" value={fmtN(result.delta,3)} accent sub="$ per $1 move" col={tc}/>
                </div>

                {/* Greeks */}
                <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:10}}>
                  <Stat label="Gamma" value={fmtN(result.gamma,4)}/>
                  <Stat label="Theta/Day" value={fmtC(result.theta)}/>
                  <Stat label="Vega/1%IV" value={fmtC(result.vega)}/>
                  <Stat label="IV at Open" value={`${fmtN(result.ivAtOpen)}%`}/>
                </div>

                {/* ── Payoff Diagram + Sliders ── */}
                <div style={{background:C.panel,border:`1px solid ${tc}44`,borderRadius:8,padding:"16px 18px"}}>
                  <div style={{display:"flex",alignItems:"center",gap:10,marginBottom:14}}>
                    <span style={{fontFamily:disp,fontSize:18,color:tc,letterSpacing:"0.06em"}}>PAYOFF DIAGRAM</span>
                    <Tag text="OptionStrat Style" col={tc}/>
                    {ticker&&<span style={{fontFamily:disp,fontSize:14,color:C.muted,letterSpacing:"0.04em"}}>{ticker}</span>}
                  </div>

                  <PayoffChart
                    K={result.K} T1={result.T1}
                    iv1={result.iv1} ivSim={simIV}
                    premium={result.priceAtOpen}
                    optType={optType}
                    spotRange={[result.S1*0.70, result.S1*1.30]}
                    simSpot={simSpot} simDTE={simDTE}
                    contracts={result.nC}
                  />

                  {/* Sliders */}
                  <div style={{borderTop:`1px solid ${C.border}`,marginTop:14,paddingTop:14,display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:20}}>
                    <Slider label="Stock Price"
                      min={result.S1*0.70} max={result.S1*1.30}
                      value={simSpot} onChange={setSimSpot}
                      fmt={v=>`$${v.toFixed(2)}`} col={tc}
                      hint={`Entry: $${result.S1.toFixed(2)}`}/>
                    <Slider label="Days to Expiry"
                      min={0} max={Math.max(parseFloat(dte)||1,1)}
                      value={simDTE} onChange={setSimDTE}
                      fmt={v=>`${v.toFixed(1)}d`} col={C.orange}
                      hint="Drag left = theta decay"/>
                    <Slider label="Implied Volatility"
                      min={Math.max(result.ivAtOpen*0.50,5)} max={result.ivAtOpen*1.80}
                      value={simIV} onChange={setSimIV}
                      fmt={v=>`${v.toFixed(0)}%`} col={C.accent}
                      hint={`At open: ${fmtN(result.ivAtOpen)}%`}/>
                  </div>

                  {/* Live sim readout */}
                  {simResult&&(
                    <div style={{marginTop:12,display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:10}}>
                      <Stat label="Option Price (Sim)" value={fmtC(simResult.optP)} accent col={tc}/>
                      <Stat label="P&L %" value={`${simResult.pnlPct>=0?"+":""}${fmtN(simResult.pnlPct)}%`} accent col={simResult.pnlPct>=0?tc:C.put}/>
                      <Stat label="P&L $" value={fmtD(simResult.pnlDollar)} accent col={simResult.pnlDollar>=0?tc:C.put} sub={`${result.nC} contract${result.nC>1?"s":""}`}/>
                      <Stat label="Delta (Sim)" value={fmtN(simResult.d,3)} sub={`Gamma: ${fmtN(simResult.g,4)}`}/>
                    </div>
                  )}

                  {/* Quick-set buttons */}
                  <div style={{display:"flex",gap:8,marginTop:10,flexWrap:"wrap"}}>
                    <span style={{fontSize:8,color:C.muted,alignSelf:"center",letterSpacing:"0.1em",textTransform:"uppercase"}}>Quick set:</span>
                    <Btn small secondary onClick={()=>{setSimSpot(result.S1);setSimDTE(parseFloat(dte)-1||0);setSimIV(result.ivAtOpen);}}>At Open</Btn>
                    <Btn small secondary onClick={()=>{setSimDTE(0);setSimIV(result.ivAtOpen*0.7);}}>Expiry</Btn>
                    {[{pct:0.15,stockP:result.targetStock15},{pct:0.20,stockP:result.targetStock20},{pct:0.25,stockP:result.targetStock25}].map(t=>(
                      <Btn key={t.pct} small secondary onClick={()=>setSimSpot(t.stockP)} col={tc}>
                        +{Math.round(t.pct*100)}% (${t.stockP.toFixed(1)})
                      </Btn>
                    ))}
                  </div>
                </div>

                {/* Exit Targets */}
                <div style={{background:C.panel,border:`1px solid ${C.border}`,borderRadius:6,padding:"12px 14px"}}>
                  <div style={{fontSize:9,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase",marginBottom:10}}>
                    Exit Targets — stock needs to {result.type==="call"?"RISE ▲":"DROP ▼"}
                  </div>
                  <div style={{display:"grid",gridTemplateColumns:"repeat(3,1fr)",gap:10}}>
                    {[["15%",result.target15,result.targetStock15],["20%",result.target20,result.targetStock20],["25%",result.target25,result.targetStock25]].map(([pct,opt,stock])=>(
                      <div key={pct} style={{background:C.bg,border:`1px solid ${C.border}`,borderRadius:5,padding:"10px 12px"}}>
                        <div style={{fontSize:8,color:C.muted,textTransform:"uppercase",letterSpacing:"0.1em",marginBottom:4}}>+{pct} Gain</div>
                        <div style={{fontSize:18,color:tc,fontFamily:mono,fontWeight:600}}>{fmtC(opt)}</div>
                        <div style={{fontSize:9,color:"#555",marginTop:2}}>stock @ {fmtC(stock)}</div>
                      </div>
                    ))}
                  </div>
                </div>

                {/* Scenario table */}
                <div style={{background:C.panel,border:`1px solid ${C.border}`,borderRadius:6,padding:"12px 14px"}}>
                  <div style={{fontSize:9,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase",marginBottom:10}}>
                    {result.type==="call"?"Bounce Scenarios ▲":"Fade Scenarios ▼"}
                  </div>
                  <div style={{display:"grid",gridTemplateColumns:"repeat(5,1fr)",gap:6}}>
                    {result.scenarioTable.map((s,i)=>(
                      <div key={i} style={{background:C.bg,border:`1px solid ${C.border}`,borderRadius:4,padding:"8px 10px",textAlign:"center"}}>
                        <div style={{fontSize:9,color:tc,fontFamily:mono}}>{fmtC(s.stockPrice)}</div>
                        <div style={{fontSize:13,color:C.text,fontFamily:mono,fontWeight:600,marginTop:2}}>{fmtC(s.optPrice)}</div>
                        <div style={{fontSize:9,color:s.pct>0?tc:C.put}}>{s.pct>0?"+":""}{fmtN(s.pct)}%</div>
                        <div style={{fontSize:8,color:"#444",marginTop:2}}>{fmtD(s.dollar)}</div>
                      </div>
                    ))}
                  </div>
                </div>

                {/* IV Crush + Checklist */}
                <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:10}}>
                  <div style={{background:C.panel,border:`1px solid #ff666622`,borderRadius:6,padding:"12px 14px"}}>
                    <div style={{fontSize:9,color:"#ff6666",letterSpacing:"0.1em",textTransform:"uppercase",marginBottom:8}}>⚠ IV Crush (−20% IV)</div>
                    <div style={{display:"flex",flexDirection:"column",gap:5}}>
                      <div><span style={{fontSize:8,color:C.muted}}>Option price: </span><span style={{fontSize:14,color:C.orange}}>{fmtC(result.ivCrushPrice)}</span></div>
                      <div><span style={{fontSize:8,color:C.muted}}>Status: </span><span style={{fontSize:12,color:result.crushSurvives?tc:C.put}}>{result.crushSurvives?"SURVIVES":"AT RISK"}</span></div>
                      <div><span style={{fontSize:8,color:C.muted}}>Impact: </span><span style={{fontSize:12,color:C.orange}}>{fmtC(result.ivCrushPrice-result.priceAtOpen)}/share</span></div>
                    </div>
                  </div>
                  <div style={{background:C.panel,border:`1px solid ${C.border}`,borderRadius:6,padding:"12px 14px"}}>
                    <div style={{fontSize:9,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase",marginBottom:8}}>Checklist</div>
                    {[
                      ["Gap > 8%",Math.abs(result.gapPct)>=8,Math.abs(result.gapPct)>=8?"PASS":"WEAK"],
                      ["Delta > 0.15",Math.abs(result.delta)>=0.15,Math.abs(result.delta)>=0.15?"PASS":"LOW"],
                      ["IV Crush ok",result.crushSurvives,result.crushSurvives?"PASS":"REVIEW"],
                      ["RSI signal",rsi!==null?(result.type==="call"?rsi<40:rsi>60):null,
                        rsi===null?"FETCH DATA":result.type==="call"?rsi<40?`OVERSOLD ${fmtN(rsi,0)}`:`RSI ${fmtN(rsi,0)} — weak`:rsi>60?`OVERBOUGHT ${fmtN(rsi,0)}`:`RSI ${fmtN(rsi,0)} — weak`],
                      ["Priced ok",result.status==="FAVORABLE",result.status==="FAVORABLE"?"PASS":result.status],
                      ["OI/Volume",null,"MANUAL CHECK"],
                    ].map(([label,pass,val])=>(
                      <div key={label} style={{display:"flex",justifyContent:"space-between",marginBottom:5}}>
                        <span style={{fontSize:8,color:"#444"}}>{label}</span>
                        <span style={{fontSize:8,color:pass===true?tc:pass===false?C.put:C.orange}}>{val}</span>
                      </div>
                    ))}
                  </div>
                </div>

                <Btn secondary small onClick={()=>{setLogForm(f=>({...f,ticker,type:optType,strike}));setTab(3);}}>+ LOG THIS TRADE</Btn>
              </div>
            )}
          </div>
        )}

        {/* ── SCREENER ── */}
        {tab===1&&(
          <div style={{display:"flex",flexDirection:"column",gap:14}}>
            <div style={{fontFamily:disp,fontSize:20,color:C.text,letterSpacing:"0.06em"}}>LIVE TICKER SCANNER</div>
            <div style={{fontSize:9,color:C.muted}}>Enter any ticker to pull live quote, gap %, HV, and RSI via Yahoo Finance (api.py)</div>

            <div style={{display:"grid",gridTemplateColumns:"160px 1fr",gap:10,alignItems:"end"}}>
              <Field label="Ticker to Scan">
                <input value={scanTicker} onChange={e=>setScanTicker(e.target.value.toUpperCase())}
                  placeholder="GOOG, AMZN, META..."
                  style={{...baseInp,color:C.accent,fontSize:14,letterSpacing:"0.1em"}}
                  onKeyDown={e=>e.key==="Enter"&&runScan()}
                  onFocus={e=>e.target.style.borderColor=C.accent}
                  onBlur={e=>e.target.style.borderColor=C.border}/>
              </Field>
              <div style={{display:"flex",alignItems:"flex-end",gap:10}}>
                <Btn onClick={runScan} loading={scanning} col={C.accent}>⚡ SCAN</Btn>
                {scanErr&&<span style={{fontSize:9,color:C.put}}>{scanErr}</span>}
              </div>
            </div>

            {scanData&&(
              <div style={{display:"flex",flexDirection:"column",gap:10}}>
                <div style={{display:"grid",gridTemplateColumns:"repeat(5,1fr)",gap:10}}>
                  <Stat label="Ticker" value={scanData.ticker} accent col={C.accent}/>
                  <Stat label="Live Price" value={fmtC(scanData.quote.current)} accent col={scanData.gapPct>0?C.put:C.call}/>
                  <Stat label="Prev Close" value={fmtC(scanData.quote.prevClose)}/>
                  <Stat label="Gap %" value={`${scanData.gapPct>0?"+":""}${fmtN(scanData.gapPct)}%`} accent col={scanData.gapPct>0?C.put:C.call}/>
                  <Stat label="HV (20d)" value={`${fmtN(scanData.hv)}%`} sub="use as IV baseline"/>
                </div>
                <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:10}}>
                  <RSIBar rsi={scanData.rsi}/>
                  <div style={{background:C.panel,border:`1px solid ${C.border}`,borderRadius:6,padding:"12px 14px"}}>
                    <div style={{fontSize:9,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase",marginBottom:8}}>Trade Signal</div>
                    <div style={{fontSize:10,color:scanData.gapPct<-8?C.call:scanData.gapPct>8?C.put:C.muted,marginBottom:10}}>
                      {Math.abs(scanData.gapPct)>=8
                        ?scanData.gapPct<0?`⚡ GAP DOWN ${fmtN(Math.abs(scanData.gapPct))}% — consider CALLS`
                          :`⚡ GAP UP ${fmtN(scanData.gapPct)}% — consider PUTS`
                        :`Gap ${fmtN(Math.abs(scanData.gapPct))}% — below 8% threshold`}
                    </div>
                    <Btn small col={scanData.gapPct<0?C.call:C.put} onClick={loadFromScan}>LOAD INTO PRICER →</Btn>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {/* ── SIGNALS ── */}
        {tab===2&&(
          <div style={{display:"flex",flexDirection:"column",gap:14}}>
            <div style={{display:"flex",alignItems:"center",justifyContent:"space-between",flexWrap:"wrap",gap:10}}>
              <div>
                <div style={{fontFamily:disp,fontSize:20,color:C.text,letterSpacing:"0.06em"}}>SIGNALS</div>
                <div style={{fontSize:9,color:C.muted,marginTop:2}}>python api.py required · pick an algo below then run scan</div>
              </div>
              <div style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap"}}>
                {sigLastScanned&&<span style={{fontSize:8,color:C.muted}}>last scan: {sigLastScanned}</span>}
                <Btn onClick={runSignalScan} loading={sigScanning} col={C.accent}>⚡ RUN SCAN</Btn>
                {signals.filter(s=>s.signal!=="SKIP").length>0&&(
                  <button onClick={saveScanOpen} disabled={scanSaving} style={{
                    padding:"5px 12px",borderRadius:4,fontSize:8,fontWeight:600,fontFamily:"monospace",
                    letterSpacing:"0.08em",border:`1px solid ${C.call}55`,background:C.call+"18",
                    color:C.call,cursor:scanSaving?"not-allowed":"pointer",opacity:scanSaving?0.5:1,textTransform:"uppercase",
                  }}>{scanSaving?"...":"💾 Save Open"}</button>
                )}
                <button onClick={scanLogClose} disabled={scanClosing} style={{
                  padding:"5px 12px",borderRadius:4,fontSize:8,fontWeight:600,fontFamily:"monospace",
                  letterSpacing:"0.08em",border:`1px solid #f59e0b55`,background:"#f59e0b18",
                  color:"#f59e0b",cursor:scanClosing?"not-allowed":"pointer",opacity:scanClosing?0.5:1,textTransform:"uppercase",
                }}>{scanClosing?"...":"📸 Log 3:45"}</button>
                <button onClick={runScanValidate} disabled={scanValidating} style={{
                  padding:"5px 12px",borderRadius:4,fontSize:8,fontWeight:600,fontFamily:"monospace",
                  letterSpacing:"0.08em",border:`1px solid #a855f755`,background:"#a855f718",
                  color:"#a855f7",cursor:scanValidating?"not-allowed":"pointer",opacity:scanValidating?0.5:1,textTransform:"uppercase",
                }}>{scanValidating?"...":"✓ Validate"}</button>
              </div>
            </div>

            {scanSaveMsg&&(
              <div style={{padding:"6px 12px",borderRadius:5,fontSize:9,fontFamily:"monospace",
                background:scanSaveMsg.ok?"#22c55e18":"#ef444418",
                border:`1px solid ${scanSaveMsg.ok?"#22c55e":"#ef4444"}44`,
                color:scanSaveMsg.ok?"#22c55e":"#ef4444"}}>
                {scanSaveMsg.text}
              </div>
            )}

            <div style={{display:"grid",gridTemplateColumns:"repeat(3,1fr)",gap:8}}>
              {[
                ["largecap","LARGE CAP","TSX banks · energy · NASDAQ megacap · semis · cloud","🏦"],
                ["spx","S&P 500","S&P 500 + NASDAQ — pure US large caps across all sectors","📊"],
                ["smallcap","SMALL CAP","Momentum plays — crypto · EV · fintech · penny stocks","🚀"],
              ].map(([key,label,desc,icon])=>{
                const active=sigAlgo===key;
                return(
                  <button key={key} onClick={()=>{setSigAlgo(key);setSignals([]);setSigRegime("");setAutoRec(null);setVolCheck(null);}} style={{
                    background:active?C.accent+"18":"transparent",
                    border:`1px solid ${active?C.accent:C.border}`,
                    borderRadius:5,padding:"10px 12px",cursor:"pointer",textAlign:"left",
                  }}>
                    <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:3}}>
                      <span style={{fontSize:13}}>{icon}</span>
                      <span style={{fontSize:10,color:active?C.accent:C.text,fontFamily:mono,letterSpacing:"0.08em",textTransform:"uppercase",fontWeight:600}}>{label}</span>
                      {active&&<span style={{fontSize:7,color:C.accent,fontFamily:mono,letterSpacing:"0.08em",marginLeft:"auto"}}>ACTIVE</span>}
                    </div>
                    <div style={{fontSize:8,color:C.muted,fontFamily:mono,lineHeight:1.4}}>{desc}</div>
                  </button>
                );
              })}
            </div>

            {sigRegime&&(
              <div style={{display:"flex",gap:10,alignItems:"center",padding:"8px 14px",background:C.panel,border:`1px solid ${C.border}`,borderRadius:5}}>
                <span style={{fontSize:8,color:C.muted,letterSpacing:"0.1em",textTransform:"uppercase"}}>MARKET REGIME</span>
                <span style={{fontSize:10,color:sigRegime.includes("🟢")?C.call:sigRegime.includes("🔴")?C.put:C.accent}}>{sigRegime}</span>
                <span style={{marginLeft:"auto",fontSize:8,color:C.accent,fontFamily:mono,letterSpacing:"0.08em",background:C.accent+"18",padding:"2px 8px",borderRadius:3,textTransform:"uppercase"}}>
                  {{largecap:"LARGE CAP",spx:"S&P 500",smallcap:"SMALL CAP"}[sigAlgo]}
                </span>
              </div>
            )}

            {sigError&&(
              <div style={{fontSize:9,color:C.put,padding:"10px 14px",background:"#ff000011",border:`1px solid ${C.put}44`,borderRadius:5}}>{sigError}</div>
            )}

            {signals.length>0&&(
              <div style={{display:"flex",gap:6,alignItems:"center"}}>
                <span style={{fontSize:8,color:C.muted,letterSpacing:"0.08em"}}>SHOW:</span>
                {["ALL","BUY","WATCH"].map(f=>(
                  <button key={f} onClick={()=>setSigFilter(f)} style={{
                    background:sigFilter===f?C.accent:"transparent",
                    color:sigFilter===f?C.bg:C.muted,
                    border:`1px solid ${sigFilter===f?C.accent:C.border}`,
                    borderRadius:4,padding:"3px 10px",fontSize:8,
                    fontFamily:mono,letterSpacing:"0.1em",cursor:"pointer",textTransform:"uppercase"
                  }}>
                    {f} ({f==="ALL"?signals.filter(s=>s.signal!=="SKIP").length:signals.filter(s=>s.signal===f).length})
                  </button>
                ))}
              </div>
            )}

            {sigScanning&&(
              <div style={{background:C.panel,border:`1px solid ${C.accent}33`,borderRadius:6,padding:48,textAlign:"center"}}>
                <div style={{fontSize:11,color:C.accent,letterSpacing:"0.12em"}}>SCANNING LARGE CAP UNIVERSE</div>
                <div style={{fontSize:9,color:C.muted,marginTop:8}}>TSX · S&P TSX Composite · NASDAQ — takes ~60 seconds</div>
              </div>
            )}

            {!sigScanning&&signals.length===0&&!sigError&&(
              <div style={{background:C.panel,border:`1px solid ${C.border}`,borderRadius:6,padding:48,textAlign:"center"}}>
                <div style={{fontSize:10,color:C.muted,marginBottom:10}}>No signals loaded yet</div>
                <div style={{fontSize:9,color:"#444",lineHeight:1.8}}>
                  1. In your terminal: <span style={{color:C.accent,fontFamily:mono}}>cd Trade_suggestion && python api.py</span><br/>
                  2. Select an algo above<br/>
                  3. Click <span style={{color:C.accent}}>⚡ RUN SCAN</span>
                </div>
              </div>
            )}

            <div style={{display:"grid",gridTemplateColumns:"repeat(2,1fr)",gap:10}}>
              {signals
                .filter(s=>s.signal!=="SKIP")
                .filter(s=>sigFilter==="ALL"||s.signal===sigFilter)
                .slice(0,20)
                .map(sig=>{
                  const isBuy=sig.signal==="BUY";
                  const sigCol=isBuy?C.call:C.accent;
                  const isCA=["TSX","TSXV","CSE"].includes(sig.market);
                  return(
                    <div key={sig.symbol} style={{background:C.panel,border:`1px solid ${sigCol}33`,borderRadius:6,padding:"12px 14px",position:"relative",overflow:"hidden"}}>
                      <div style={{position:"absolute",top:0,left:0,right:0,height:2,background:sigCol}}/>
                      <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:10}}>
                        <div style={{display:"flex",gap:8,alignItems:"center",flexWrap:"wrap"}}>
                          <span style={{fontFamily:disp,fontSize:22,color:sigCol,letterSpacing:"0.04em"}}>{sig.symbol}</span>
                          <Tag text={sig.signal} col={sigCol}/>
                          <Tag text={sig.market} col={C.muted}/>
                          {isCA&&<span style={{fontSize:11}}>🇨🇦</span>}
                        </div>
                        <div style={{textAlign:"right"}}>
                          <div style={{fontSize:15,color:C.text,fontFamily:mono,fontWeight:600}}>${sig.price?.toFixed(2)}</div>
                          <div style={{fontSize:9,color:sig.change>=0?C.call:C.put}}>{sig.change>=0?"+":""}{sig.change?.toFixed(2)}%</div>
                        </div>
                      </div>
                      <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:6,marginBottom:10}}>
                        {[
                          ["SCORE",sig.score?.toFixed(1),sigCol],
                          ["R/R",(sig.risk_reward?.toFixed(1)??"—")+"R",C.text],
                          ["RSI D",sig.rsi_d??"—",sig.rsi_d<40?C.call:sig.rsi_d>65?C.put:C.text],
                          ["VOL",(sig.vol_spike??"—")+"x",sig.vol_spike>=2?C.accent:C.text],
                        ].map(([l,v,c])=>(
                          <div key={l} style={{background:C.bg,borderRadius:4,padding:"6px 8px"}}>
                            <div style={{fontSize:7,color:C.muted,textTransform:"uppercase",letterSpacing:"0.1em"}}>{l}</div>
                            <div style={{fontSize:14,color:c,fontFamily:mono,fontWeight:600,lineHeight:1.2}}>{v}</div>
                          </div>
                        ))}
                      </div>
                      <div style={{display:"flex",gap:14,fontSize:9,marginBottom:8,flexWrap:"wrap"}}>
                        <span style={{color:C.muted}}>Stop: <span style={{color:C.put}}>${sig.stop?.toFixed(2)}</span></span>
                        <span style={{color:C.muted}}>Target: <span style={{color:C.call}}>${sig.target?.toFixed(2)}</span></span>
                        {sig.sector&&<span style={{color:"#444"}}>{sig.sector}</span>}
                      </div>
                      {sig.reasons?.length>0&&(
                        <div style={{fontSize:8,color:"#555",marginBottom:10,lineHeight:1.5}}>{sig.reasons.slice(0,3).join(" · ")}</div>
                      )}
                      <div style={{display:"flex",gap:6,alignItems:"center",flexWrap:"wrap"}}>
                        <Btn small col={C.accent} onClick={()=>runAutoRecommend(sig)}>⚡ AUTO RECOMMEND</Btn>
                        <Btn small secondary onClick={()=>loadFromSignal(sig)}>MANUAL →</Btn>
                        {typeof sig.earnings_days==="number"&&sig.earnings_days>=0&&sig.earnings_days<=7&&(
                          <Tag text={`ERN ${sig.earnings_days}d`} col={C.orange}/>
                        )}
                        {sig.warnings?.length>0&&(
                          <span style={{fontSize:7,color:"#555"}}>⚠ {sig.warnings[0]}</span>
                        )}
                      </div>
                    </div>
                  );
                })}
            </div>

            {/* Validation Panel */}
            {scanValidation&&!scanValidation.error&&(
              <div style={{marginTop:10}}>
                <div style={{display:"flex",alignItems:"center",gap:12,marginBottom:10,flexWrap:"wrap"}}>
                  <span style={{fontSize:10,color:"#a855f7",letterSpacing:"0.1em",textTransform:"uppercase",fontFamily:mono,fontWeight:600}}>
                    Validation — {scanValidation.date} · {scanValidation.algo?.toUpperCase()}
                    {scanValidation.saved_at&&<span style={{color:"#555",fontWeight:400}}> (saved {scanValidation.saved_at})</span>}
                  </span>
                  {scanValidation.win_rate!=null&&(
                    <>
                      <span style={{fontSize:11,color:"#a855f7",fontFamily:mono,fontWeight:700,background:"#a855f718",padding:"2px 10px",borderRadius:4}}>
                        WR: {scanValidation.win_rate}%
                      </span>
                      <span style={{fontSize:9,color:C.muted,fontFamily:mono}}>
                        {scanValidation.wins}W / {scanValidation.validated - scanValidation.wins}L of {scanValidation.validated} signals
                      </span>
                    </>
                  )}
                  {!scanValidation.validated&&<span style={{fontSize:9,color:C.muted}}>No close prices yet — press 📸 Log 3:45 first</span>}
                </div>
                <div style={{overflowX:"auto",borderRadius:6,border:`1px solid #222`}}>
                  <table style={{width:"100%",borderCollapse:"collapse",fontFamily:mono}}>
                    <thead>
                      <tr style={{background:C.panel}}>
                        {["Symbol","Signal","Score","Sector","Open Px","Close Px","Move","Outcome"].map(h=>(
                          <th key={h} style={{padding:"7px 10px",textAlign:"left",borderBottom:"1px solid #222",fontSize:8,color:"#555",fontWeight:600,whiteSpace:"nowrap",letterSpacing:"0.08em",textTransform:"uppercase"}}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {scanValidation.results.map((r,i)=>{
                        const oc=r.outcome==="TARGET"?"#f59e0b":r.outcome==="WIN"?"#22c55e":r.outcome==="LOSS"||r.outcome==="STOP"?"#ef4444":"#555";
                        const mc=r.move_pct==null?"#555":r.move_pct>0?"#22c55e":"#ef4444";
                        return(
                          <tr key={i} style={{background:i%2===0?"transparent":C.panel+"88"}}>
                            <td style={{padding:"7px 10px",fontSize:12,fontWeight:700,borderBottom:"1px solid #1a1a1a",color:r.signal==="BUY"?"#22c55e":C.accent}}>{r.symbol}</td>
                            <td style={{padding:"7px 10px",fontSize:9,borderBottom:"1px solid #1a1a1a",color:r.signal==="BUY"?"#22c55e":C.accent}}>{r.signal}</td>
                            <td style={{padding:"7px 10px",fontSize:11,borderBottom:"1px solid #1a1a1a",color:C.accent}}>{r.score?.toFixed(1)}</td>
                            <td style={{padding:"7px 10px",fontSize:8,borderBottom:"1px solid #1a1a1a",color:"#444"}}>{r.sector||"—"}</td>
                            <td style={{padding:"7px 10px",fontSize:11,borderBottom:"1px solid #1a1a1a",color:"#888"}}>{r.open_px?`$${r.open_px}`:"—"}</td>
                            <td style={{padding:"7px 10px",fontSize:11,borderBottom:"1px solid #1a1a1a",color:"#888"}}>{r.close_px?`$${r.close_px}`:"—"}</td>
                            <td style={{padding:"7px 10px",fontSize:11,borderBottom:"1px solid #1a1a1a",color:mc,fontWeight:600}}>{r.move_pct!=null?`${r.move_pct>0?"+":""}${r.move_pct}%`:"—"}</td>
                            <td style={{padding:"7px 10px",borderBottom:"1px solid #1a1a1a"}}>
                              <span style={{color:oc,fontSize:9,fontWeight:700,letterSpacing:"0.06em"}}>
                                {r.outcome==="TARGET"?"★ TARGET":r.outcome==="WIN"?"✓ WIN":r.outcome==="STOP"?"✗ STOP":r.outcome==="LOSS"?"✗ LOSS":"— pending"}
                              </span>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
            {scanValidation?.error&&(
              <div style={{fontSize:9,color:C.put,padding:"8px 12px",background:"#ff000011",border:`1px solid ${C.put}44`,borderRadius:5}}>
                {scanValidation.error}
              </div>
            )}

            {/* Auto Recommendation Panel */}
            {autoRec&&autoRecSym&&(()=>{
              const rc=autoRec;
              const tc2=rc.type==="call"?C.call:C.put;
              return(
                <div style={{background:"#060606",border:`2px solid ${tc2}`,borderRadius:8,padding:"18px 20px",position:"relative"}}>
                  <button onClick={()=>{setAutoRec(null);setAutoRecSym(null);setVolCheck(null);}} style={{position:"absolute",top:10,right:14,background:"transparent",border:"none",color:C.muted,cursor:"pointer",fontSize:16,lineHeight:1}}>✕</button>
                  <div style={{marginBottom:14}}>
                    <div style={{fontSize:9,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase",marginBottom:4}}>RECOMMENDED CONTRACT</div>
                    <div style={{display:"flex",alignItems:"baseline",gap:12,flexWrap:"wrap"}}>
                      <span style={{fontFamily:disp,fontSize:28,color:tc2,letterSpacing:"0.04em"}}>
                        {rc.sig.symbol} ${rc.strike} {rc.type==="call"?"C":"P"} {rc.dte}DTE
                      </span>
                      <Tag text={rc.type==="call"?"▲ CALL":"▼ PUT"} col={tc2}/>
                      {rc.isGapFade&&<Tag text="GAP FADE" col={C.orange}/>}
                    </div>
                    <div style={{fontSize:9,color:C.muted,marginTop:4}}>
                      Stock @ ${rc.sig.price?.toFixed(2)} · IV est. {rc.iv}% · ideal strike ${rc.idealStrike?.toFixed(2)} · {rc.ivBump}% IV bump
                    </div>
                  </div>

                  {/* Illiquid / spread warning */}
                  {(rc.illiquidWarning||volCheck?.spreadWide)&&(
                    <div style={{padding:"8px 12px",background:"#ff990011",border:`1px solid ${C.orange}44`,borderRadius:5,marginBottom:10,fontSize:9,color:C.orange}}>
                      ⚠ {rc.illiquidWarning||(volCheck?.spreadWide?"Spread > 18% — use limit orders at mid-price":"")}
                    </div>
                  )}

                  <div style={{display:"grid",gridTemplateColumns:"repeat(5,1fr)",gap:8,marginBottom:10}}>
                    {[
                      ["Est. Price",`$${fmtN(rc.optPrice,2)}`,true],
                      ["Suggested Bid",`$${fmtN(rc.sugBid,2)}`,true],
                      ["Delta",fmtN(rc.delta,3),false],
                      ["Theta/Day",`$${fmtN(rc.theta,3)}`,false],
                      ["Vega/1%IV",`$${fmtN(rc.vega,3)}`,false],
                    ].map(([l,v,a])=>(
                      <Stat key={l} label={l} value={v} accent={a} col={tc2}/>
                    ))}
                  </div>

                  {/* If target hit */}
                  {rc.tradeTarget&&rc.tradeTarget!==rc.sig.price&&(
                    <div style={{padding:"10px 14px",background:tc2+"11",border:`1px solid ${tc2}44`,borderRadius:6,marginBottom:10}}>
                      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",flexWrap:"wrap",gap:8}}>
                        <div>
                          <div style={{fontSize:8,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase",marginBottom:3}}>
                            IF SIGNAL TARGET HIT (${rc.tradeTarget?.toFixed(2)} · {rc.expectedMovePct?.toFixed(1)}% {rc.type==="call"?"rally":"drop"})
                          </div>
                          <div style={{display:"flex",gap:16,alignItems:"baseline",flexWrap:"wrap"}}>
                            <span style={{fontSize:20,color:tc2,fontFamily:mono,fontWeight:700}}>${fmtN(rc.valueAtTarget,2)}</span>
                            <span style={{fontSize:14,color:rc.pnlAtTarget>=0?tc2:C.put,fontFamily:mono,fontWeight:600}}>
                              {rc.pnlAtTarget>=0?"+":""}{fmtN(rc.pnlAtTarget,0)}%
                            </span>
                            <span style={{fontSize:9,color:C.muted}}>per contract: {rc.pnlAtTarget>=0?"+$":"-$"}{Math.abs((rc.valueAtTarget-rc.optPrice)*100).toFixed(0)}</span>
                          </div>
                        </div>
                        <div style={{fontSize:8,color:"#444",textAlign:"right"}}>
                          <div>Stop hit → ~$0</div>
                          <div style={{marginTop:2}}>Ideal strike: ${rc.idealStrike?.toFixed(2)}</div>
                        </div>
                      </div>
                    </div>
                  )}

                  <div style={{display:"grid",gridTemplateColumns:"repeat(3,1fr)",gap:8,marginBottom:10}}>
                    {[["+ 15%",rc.t15],["+ 20%",rc.t20],["+ 25%",rc.t25]].map(([l,v])=>(
                      <div key={l} style={{background:C.bg,border:`1px solid ${C.border}`,borderRadius:5,padding:"8px 10px"}}>
                        <div style={{fontSize:8,color:C.muted,textTransform:"uppercase",letterSpacing:"0.1em"}}>{l} target</div>
                        <div style={{fontSize:18,color:tc2,fontFamily:mono,fontWeight:600}}>${fmtN(v,2)}</div>
                      </div>
                    ))}
                  </div>
                  <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:8,marginBottom:12}}>
                    <div style={{background:C.bg,border:`1px solid ${C.border}`,borderRadius:5,padding:"10px 12px"}}>
                      <div style={{fontSize:8,color:C.muted,textTransform:"uppercase",letterSpacing:"0.1em",marginBottom:4}}>Stock move needed for +20%</div>
                      <div style={{fontSize:15,color:C.text,fontFamily:mono,fontWeight:600}}>${fmtN(rc.reqMove,2)}</div>
                      <div style={{fontSize:9,color:C.muted}}>{fmtN(rc.reqMovePct,1)}% from current price</div>
                    </div>
                    <div style={{background:C.bg,border:"1px solid #ff666622",borderRadius:5,padding:"10px 12px"}}>
                      <div style={{fontSize:8,color:"#ff6666",textTransform:"uppercase",letterSpacing:"0.1em",marginBottom:4}}>⚠ IV Crush scenario (−20% IV)</div>
                      <div style={{fontSize:15,color:C.orange,fontFamily:mono,fontWeight:600}}>${fmtN(rc.ivCrushPrice,2)}</div>
                      <div style={{fontSize:9,color:rc.ivCrushPrice>rc.sugBid?C.call:C.put}}>
                        {rc.ivCrushPrice>rc.sugBid?"Survives crush ✓":"At risk — review strike ✗"}
                      </div>
                    </div>
                  </div>

                  {/* Options Volume Check */}
                  <div style={{marginBottom:10,padding:"12px 14px",background:C.bg,borderRadius:5,border:`1px solid ${
                    volChecking?"#333":volCheck?.error?"#333":volCheck?.targetOk?C.call+"44":C.put+"44"}`}}>
                    <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:8}}>
                      <div style={{fontSize:8,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase"}}>OPTIONS VOLUME CHECK</div>
                      {volChecking&&<span style={{fontSize:8,color:C.accent}}>checking...</span>}
                      {!volChecking&&volCheck&&!volCheck.error&&(
                        <Tag text={volCheck.targetOk?"LIQUID ✓":"LOW VOLUME ✗"} col={volCheck.targetOk?C.call:C.put}/>
                      )}
                    </div>
                    {volChecking&&<div style={{fontSize:9,color:C.muted}}>Fetching options chain for {rc.sig.symbol}...</div>}
                    {!volChecking&&volCheck?.error&&(
                      <div style={{fontSize:9,color:"#555"}}>⚠ {volCheck.error} — verify volume manually on your broker</div>
                    )}
                    {!volChecking&&volCheck&&!volCheck.error&&(()=>{
                      const c=volCheck.exact;
                      return(<>
                        <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:6,marginBottom:8}}>
                          {[
                            ["Strike",`$${rc.strike}`,true],
                            ["Volume",c?(c.volume||0).toLocaleString():"—",!!(c&&c.volume>=volCheck.MIN_VOL)],
                            ["Open Int.",c?(c.openInterest||0).toLocaleString():"—",!!(c&&c.openInterest>=volCheck.MIN_OI)],
                            ["Spread",c&&c.spread_pct!=null?`${c.spread_pct}%`:"—",!!(c&&c.spread_pct<=volCheck.MAX_SPREAD)],
                          ].map(([l,v,ok])=>(
                            <div key={l} style={{background:C.panel,borderRadius:4,padding:"6px 8px",border:`1px solid ${ok?C.call+"33":C.put+"33"}`}}>
                              <div style={{fontSize:7,color:C.muted,textTransform:"uppercase",letterSpacing:"0.08em"}}>{l}</div>
                              <div style={{fontSize:13,color:ok?C.call:C.put,fontFamily:mono,fontWeight:600}}>{v}</div>
                            </div>
                          ))}
                        </div>
                        {volCheck.suggest&&(
                          <div style={{padding:"8px 10px",background:"#c8ff0011",border:`1px solid ${C.accent}44`,borderRadius:4,display:"flex",justifyContent:"space-between",alignItems:"center"}}>
                            <div>
                              <div style={{fontSize:8,color:C.accent,letterSpacing:"0.08em",marginBottom:3}}>⚡ MORE LIQUID ALTERNATIVE</div>
                              <div style={{fontSize:11,color:C.text,fontFamily:mono}}>
                                ${volCheck.suggest.strike} {rc.type.toUpperCase()} — Vol: {(volCheck.suggest.volume||0).toLocaleString()} · OI: {(volCheck.suggest.openInterest||0).toLocaleString()} · Spread: {volCheck.suggest.spread_pct??'—'}%
                              </div>
                            </div>
                            <Btn small col={C.accent} onClick={()=>{
                              const s=volCheck.suggest;
                              const newT=Math.max((rc.dte-1)/365,0.001);
                              const newIv1=(rc.iv*(1+rc.ivBump/100))/100;
                              const newOpt=(s.mid>0)?s.mid:bs(rc.sig.price,s.strike,newT,RF_RATE,newIv1,rc.type);
                              const newBid=(s.bid>0)?s.bid:newOpt*0.95;
                              setAutoRec(r=>({...r,strike:s.strike,optPrice:newOpt,sugBid:newBid,
                                delta:getDelta(rc.sig.price,s.strike,newT,RF_RATE,newIv1,rc.type),
                                t15:newOpt*1.15,t20:newOpt*1.20,t25:newOpt*1.25}));
                            }}>USE THIS</Btn>
                          </div>
                        )}
                        {volCheck.nearby?.length>0&&(
                          <div style={{marginTop:8}}>
                            <div style={{fontSize:7,color:"#444",letterSpacing:"0.1em",textTransform:"uppercase",marginBottom:4}}>NEARBY STRIKES</div>
                            <div style={{display:"grid",gridTemplateColumns:"repeat(5,1fr)",gap:4}}>
                              {volCheck.nearby.map(c=>{
                                const liq=(c.volume>=volCheck.MIN_VOL||c.openInterest>=volCheck.MIN_OI);
                                return(
                                  <div key={c.strike} style={{background:C.panel,borderRadius:3,padding:"5px 7px",border:`1px solid ${liq?C.call+"33":"#1a1a1a"}`}}>
                                    <div style={{fontSize:8,color:liq?C.call:C.muted,fontFamily:mono,fontWeight:600}}>${c.strike}</div>
                                    <div style={{fontSize:7,color:"#555"}}>V:{(c.volume||0).toLocaleString()}</div>
                                    <div style={{fontSize:7,color:"#555"}}>OI:{(c.openInterest||0).toLocaleString()}</div>
                                  </div>
                                );
                              })}
                            </div>
                          </div>
                        )}
                      </>);
                    })()}
                  </div>

                  <div style={{marginBottom:14,padding:"10px 12px",background:C.bg,borderRadius:5,border:`1px solid ${C.border}`}}>
                    <div style={{fontSize:8,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase",marginBottom:8}}>WHY THIS CONTRACT</div>
                    {rc.why.map((w,i)=>(
                      <div key={i} style={{fontSize:9,color:"#888",marginBottom:4,display:"flex",gap:6}}>
                        <span style={{color:tc2}}>›</span><span>{w}</span>
                      </div>
                    ))}
                  </div>
                  <div style={{display:"flex",gap:8,flexWrap:"wrap"}}>
                    <Btn col={tc2} onClick={loadAutoRecIntoPricer}>↗ LOAD INTO PRICER &amp; CALCULATE</Btn>
                    <Btn secondary small onClick={()=>{setLogForm(f=>({...f,ticker:rc.sig.symbol,type:rc.type,strike:String(rc.strike)}));setTab(3);}}>+ LOG TRADE</Btn>
                  </div>
                </div>
              );
            })()}
          </div>
        )}

        {/* ── TRADE LOG ── */}
        {tab===3&&(
          <div style={{display:"flex",flexDirection:"column",gap:14}}>
            <div style={{fontFamily:disp,fontSize:20,color:C.text,letterSpacing:"0.06em"}}>TRADE LOG</div>
            {trades.length>0&&(
              <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:10}}>
                <Stat label="Trades" value={trades.length}/>
                <Stat label="Win Rate" value={`${((wins/trades.length)*100).toFixed(0)}%`} accent col={C.accent}/>
                <Stat label="Avg P&L" value={`${avgPnl}%`} accent={+avgPnl>0} col={+avgPnl>0?C.call:C.put}/>
                <Stat label="W / L" value={`${wins} / ${trades.length-wins}`}/>
              </div>
            )}
            <div style={{background:C.panel,border:`1px solid ${C.border}`,borderRadius:6,padding:16}}>
              <div style={{fontSize:9,color:C.muted,letterSpacing:"0.12em",textTransform:"uppercase",marginBottom:12}}>New Entry</div>
              <div style={{display:"grid",gridTemplateColumns:"repeat(5,1fr)",gap:10,marginBottom:10}}>
                {[["Ticker","ticker","RBLX"],["Strike","strike","60"],["Entry","entry","0.80"],["Exit","exit","1.10"]].map(([label,key,ph])=>(
                  <Field key={key} label={label}>
                    <input value={logForm[key]} onChange={e=>setLogForm(f=>({...f,[key]:e.target.value.toUpperCase()}))} placeholder={ph}
                      style={{...baseInp}} onFocus={e=>e.target.style.borderColor=C.accent} onBlur={e=>e.target.style.borderColor=C.border}/>
                  </Field>
                ))}
                <Field label="Type">
                  <select value={logForm.type} onChange={e=>setLogForm(f=>({...f,type:e.target.value}))} style={{...baseInp,cursor:"pointer"}}>
                    <option value="call">CALL ▲</option><option value="put">PUT ▼</option>
                  </select>
                </Field>
              </div>
              <div style={{display:"grid",gridTemplateColumns:"120px 1fr auto",gap:10,alignItems:"end"}}>
                <Field label="Outcome">
                  <select value={logForm.outcome} onChange={e=>setLogForm(f=>({...f,outcome:e.target.value}))} style={{...baseInp,cursor:"pointer"}}>
                    <option>WIN</option><option>LOSS</option><option>SCRATCH</option>
                  </select>
                </Field>
                <Field label="Notes">
                  <input value={logForm.notes} onChange={e=>setLogForm(f=>({...f,notes:e.target.value}))}
                    placeholder="e.g. RBLX C60, RSI 12, +37% in 30min"
                    style={{...baseInp}} onFocus={e=>e.target.style.borderColor=C.accent} onBlur={e=>e.target.style.borderColor=C.border}/>
                </Field>
                <Btn onClick={logTrade} col={C.accent}>+ LOG</Btn>
              </div>
            </div>
            {trades.length===0
              ?<div style={{background:C.panel,border:`1px solid ${C.border}`,borderRadius:6,padding:24,textAlign:"center"}}><div style={{fontSize:10,color:C.muted}}>No trades yet — go get some</div></div>
              :trades.map(t=>{
                const tCol=t.type==="call"?C.call:C.put;
                const pnl=+t.pnlPct;
                return(
                  <div key={t.id} style={{background:C.panel,border:`1px solid ${C.border}`,borderRadius:5,padding:"10px 14px",display:"grid",gridTemplateColumns:"60px 55px 70px 70px 70px 80px 1fr",gap:12,alignItems:"center"}}>
                    <div style={{fontFamily:disp,fontSize:18,color:C.accent,letterSpacing:"0.06em"}}>{t.ticker}</div>
                    <Tag text={t.type} col={tCol}/>
                    <div><div style={{fontSize:8,color:C.muted}}>Strike</div><div style={{fontSize:11}}>{t.strike?"$"+t.strike:"—"}</div></div>
                    <div><div style={{fontSize:8,color:C.muted}}>Entry</div><div style={{fontSize:11}}>{t.entry?"$"+t.entry:"—"}</div></div>
                    <div><div style={{fontSize:8,color:C.muted}}>Exit</div><div style={{fontSize:11}}>{t.exit?"$"+t.exit:"—"}</div></div>
                    <div><div style={{fontSize:8,color:C.muted}}>P&L</div><div style={{fontSize:15,color:pnl>0?C.call:C.put,fontWeight:600}}>{isNaN(pnl)?"—":`${pnl>0?"+":""}${fmtN(pnl)}%`}</div></div>
                    <div><div style={{fontSize:8,color:C.muted}}>{t.date}</div><div style={{fontSize:9,color:"#555"}}>{t.notes}</div></div>
                  </div>
                );
              })
            }
          </div>
        )}
      </div>
      <style>{`input[type=number]::-webkit-inner-spin-button{-webkit-appearance:none;}select option{background:#0d0d0d;color:#f0f0f0;}*{box-sizing:border-box;}`}</style>
    </div>
  );
}
