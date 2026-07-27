/* Brief Live — render /api/state into the 3-column light board.
 *
 * Every value ships with an explicit sign — that sign is the secondary encoding that
 * keeps the green/red direction pair legible for colorblind readers. Never drop it.
 *
 * Panels marked "pending" are intentional stubs: the layout is being tried out before
 * the data behind those boxes (portfolio, live intraday analysis) is wired.
 */

const $ = (id) => document.getElementById(id);
const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toFixed(d));
const signed = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(Number(v)).toFixed(d));
const dir = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");

/* Three clocks on this board: yields live, futures ~10min delayed (Yahoo CME licence),
 * gamma on the recorder's 15min grid. Every panel says how old ITS data is — a value
 * that looks live but is 11 min stale is the failure mode we guard against. */
const lagMin = (iso) => {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : (Date.now() - t) / 60000;
};
const fmtET = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("en-US", {
    timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false,
  });
};
function stamp(el, source, iso, warnAt) {
  if (!el) return;
  const m = lagMin(iso);
  if (m === null) { el.textContent = source; el.className = "sub"; return; }
  const age = m < 1 ? "live" : `${m.toFixed(0)}m behind`;
  el.textContent = `${source} · ${fmtET(iso)} ET · ${age}`;
  el.className = "sub" + (m > warnAt ? " sub-warn" : "");
}

/* ---- AI market analysis (morning brief TL;DR) --------------------------- */
function renderAnalysis(b) {
  const el = $("analysis");
  if (!b || b.error) {
    el.innerHTML = `<div class="pending"><b>No brief loaded</b>${b ? b.error : "waiting for gather"}</div>`;
    $("analysis-sub").textContent = "";
    return;
  }
  $("analysis-sub").textContent = b.brief_date || "";
  el.innerHTML = `
    <div class="analysis-tldr">${b.tldr || "—"}</div>
    <div class="analysis-note">Showing this morning's brief TL;DR. Continuous
      intraday updates (news / macro / regime deltas) not wired yet.</div>`;
}

/* ---- economic calendar -------------------------------------------------- */
function renderEvents(eg) {
  const el = $("events");
  if (!eg) { el.innerHTML = ""; return; }
  const today = eg.today_events || [];
  const up = eg.upcoming || [];
  let h = "";
  h += today.length
    ? today.map((e) => `<div class="ev t1"><div class="t">${e.time_et || ""}</div><div class="n">${e.name || e}</div></div>`).join("")
    : `<div class="ev"><div class="t">—</div><div class="n">No T1 event today</div></div>`;
  h += up.slice(0, 4).map((e) => {
    const d = e.date || "";
    return `<div class="ev"><div class="t">${d.slice(5)}</div><div class="n">${e.name || e}
      <span class="sub">${e.days_away !== undefined ? `+${e.days_away}d` : ""} ${e.time_et || ""}</span></div></div>`;
  }).join("");
  h += `<div class="ev-note">T1 gate only — "important" non-T1 prints (PPI, retail
        sales, claims) and Fed speakers are not wired into this panel yet.</div>`;
  el.innerHTML = h;
}

/* ---- center: indices / macro / fear&greed / rotation ------------------- */
function renderIndices(ir) {
  const el = $("indices");
  if (!ir) { el.innerHTML = ""; return; }
  stamp($("idx-sub"), "Yahoo CME · delayed", (ir.nq_intraday || {}).last_ts_et, 20);
  const keys = ["ES", "YM", "NQ", "RTY", "CL=F"];
  el.innerHTML = keys.filter((k) => ir[k] && !ir[k].error).map((k) => {
    const d = ir[k];
    return `<div class="tile">
      <div class="k">${k === "CL=F" ? "WTI" : k}</div>
      <div class="v">${num(d.last, 2)}</div>
      <div class="d ${dir(d.chg_pct)}">${signed(d.chg_pct, 2)}%</div>
    </div>`;
  }).join("");
}

function renderMacro(mp) {
  const el = $("macro");
  if (!mp) { el.innerHTML = ""; return; }
  const out = [];
  const y = mp.yields;
  if (y) stamp($("macro-sub"), `${y.source || "CNBC"}${y.mode === "EOD" ? " · EOD" : ""}`, y.as_of, 10);
  if (y && y.tenors) {
    for (const t of ["2Y", "10Y", "30Y"]) {
      const v = y.tenors[t];
      if (!v) continue;
      const last = v.last ?? v.value ?? v;
      const chg = v.chg;
      out.push(`<div class="tile"><div class="k">${t}</div><div class="v">${num(last, 2)}</div>
        <div class="d ${dir(chg)}">${chg === undefined ? "" : signed(chg * 100, 1) + "bp"}</div></div>`);
    }
  }
  for (const [k, label] of [["^VIX", "VIX"], ["^VIX9D", "VIX9D"]]) {
    const v = mp[k];
    if (!v || v.error) continue;
    out.push(`<div class="tile"><div class="k">${label}</div><div class="v">${num(v.last, 2)}</div>
      <div class="d ${dir(v.chg)}">${signed(v.chg, 2)}</div></div>`);
  }
  const ts = mp.term_structure;
  if (ts) {
    const backw = String(ts.state || "").startsWith("BACKWARD");
    out.push(`<div class="tile"><div class="k">Term</div>
      <div class="v ${backw ? "down" : ""}" style="font-size:15px">${backw ? "BACKWARD" : "CONTANGO"}</div>
      <div class="d flat">${signed(ts.spread_9d_minus_30d, 2)}</div></div>`);
  }
  el.innerHTML = out.join("");
}

function renderFearGreed() {
  // CNN Fear & Greed is not in the feed yet — honest stub so the block holds its place.
  $("feargreed").innerHTML = `<div class="fg">
    <div class="num">—</div>
    <div class="lab">Fear &amp; Greed index not wired yet</div></div>`;
}

function renderRotation(sr) {
  const el = $("rotation");
  if (!sr || !sr.ranked) { el.innerHTML = ""; return; }
  const b = sr.bench || {};
  $("rot-sub").textContent = `5d vs ${b.etf || "SPY"} ${signed(b.chg_5d, 2)}%`;
  const rows = sr.ranked.map((r) => ({ etf: r.etf, label: r.label || "", rel: r.rel_5d }));
  const max = Math.max(...rows.map((r) => Math.abs(r.rel || 0)), 0.1);
  el.innerHTML = rows.map((r) => {
    const pct = (Math.abs(r.rel || 0) / max) * 50;
    const cls = r.rel >= 0 ? "pos" : "neg";
    return `<div class="rot-row" title="${r.etf} — ${r.label}: ${signed(r.rel, 2)} vs SPY over 5d">
      <div class="rot-name">${r.etf}</div>
      <div class="rot-track"><div class="rot-bar ${cls}" style="width:${pct}%"></div></div>
      <div class="rot-val ${dir(r.rel)}">${signed(r.rel, 2)}</div>
    </div>`;
  }).join("");
}

/* ---- right column: portfolio + trades (stubs) -------------------------- */
function renderPortfolio() {
  $("portfolio-sub").textContent = "not connected";
  $("portfolio").innerHTML = `
    <div class="kpis">
      <div class="kpi"><div class="k">Total Balance</div><div class="v">—</div></div>
      <div class="kpi"><div class="k">Day Gain/Loss</div><div class="v">—</div></div>
      <div class="kpi"><div class="k">30-Day Perf</div><div class="v">—</div></div>
    </div>
    <div class="pending"><b>No account connected</b>
      Pick a source (flow-trader paper book, or a live broker) and this fills in.</div>`;
}

function renderTrades() {
  $("trades-sub").textContent = "not connected";
  $("trades").innerHTML = `<div class="pending"><b>No account connected</b>
    Recent fills and open positions will list here once a portfolio source is wired.</div>`;
}

/* ---- health / alerts ---------------------------------------------------- */
function renderHealth(state) {
  const el = $("health");
  const meta = state.meta || {};
  const alerts = [];
  const gex = (state.sections || {}).gex;
  if (gex && gex.stale) {
    alerts.push(`<div class="alert critical"><span class="ico">■</span>
      <div><b>GEX recorder is stale.</b> Last write ${gex.last_snapshot_et} (${gex.age_min} min ago).
      Gamma history cannot be backfilled — every minute it is down is lost permanently.</div></div>`);
  }
  if (gex && gex.error) {
    alerts.push(`<div class="alert warning"><span class="ico">■</span><div><b>GEX unavailable.</b> ${gex.error}</div></div>`);
  }
  for (const [k, m] of Object.entries(meta)) {
    if (m && !m.ok) {
      alerts.push(`<div class="alert warning"><span class="ico">■</span><div><b>${k} failed.</b> ${m.error}</div></div>`);
    }
  }
  $("alerts").innerHTML = alerts.join("");

  el.innerHTML = Object.entries(meta).map(([k, m]) => {
    const age = m.at ? Math.round((Date.now() - new Date(m.at).getTime()) / 1000) : null;
    const cls = !m.ok ? "bad" : "";
    return `<div class="f ${cls}"><span class="dot"></span>${k} ${m.ok && age !== null ? age + "s" : "fail"}</div>`;
  }).join("");
}

async function tick() {
  try {
    const r = await fetch("/api/state");
    const s = await r.json();
    $("stamp").textContent = (s.as_of_et || "") + (s.in_rth ? "  ·  RTH" : "  ·  closed");
    const sec = s.sections || {};
    renderAnalysis(sec.brief);
    renderEvents(sec.event_gate);
    renderIndices(sec.index_recap);
    renderMacro(sec.macro_pulse);
    renderFearGreed();
    renderRotation(sec.sector_rotation);
    renderPortfolio();
    renderTrades();
    renderHealth(s);
  } catch (e) {
    $("stamp").textContent = "server unreachable";
  }
}

tick();
setInterval(tick, 5000);
