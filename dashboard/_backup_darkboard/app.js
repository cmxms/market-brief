/* Brief Live — render /api/state. Every value ships with an explicit sign, which
 * is also the secondary encoding that makes the green/red direction pair legal
 * for colorblind readers (see style.css header). Never drop the sign.
 */

const $ = (id) => document.getElementById(id);
const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toFixed(d));
const signed = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(Number(v)).toFixed(d));
const dir = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");

/* Three different clocks live on this board: yields are live, futures are ~10min
 * delayed by Yahoo's CME licence, gamma is on the recorder's 15min grid. Every
 * panel must therefore say how old its own data is — a number that looks live but
 * is 11 minutes stale is the failure mode we are guarding against. */
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
/** Renders "<source> · 10:44 ET · 11m behind", flagged amber past `warnAt`. */
function stamp(el, source, iso, warnAt) {
  const m = lagMin(iso);
  if (m === null) { el.textContent = source; el.className = "sub"; return; }
  const age = m < 1 ? "live" : `${m.toFixed(0)}m behind`;
  el.textContent = `${source} · ${fmtET(iso)} ET · ${age}`;
  el.className = "sub" + (m > warnAt ? " sub-warn" : "");
}

function renderRegime(gex) {
  const el = $("regime");
  if (!gex || gex.error) {
    el.innerHTML = `<div class="regime-detail">GEX unavailable — ${gex ? gex.error : "no data"}</div>`;
    $("gex-sub").textContent = "";
    return;
  }
  // Recorder writes every 15 min during RTH, so >16 means it is falling behind.
  const gsub = $("gex-sub");
  gsub.textContent = `DB recorder · 15m grid · ${gex.last_snapshot_et} · ${gex.age_min}m ago`;
  gsub.className = "sub" + (gex.age_min > 16 ? " sub-warn" : "");
  el.innerHTML = Object.entries(gex.symbols || {}).map(([sym, s]) => {
    const short = s.regime === "negative";
    // Short gamma = dealers amplify = the fragile state. Long gamma = dampened.
    const stateTxt = short ? "short gamma" : "long gamma";
    const stateCls = short ? "down" : "up";
    const side = s.above_flip ? "above flip" : "below flip";
    const walls = (s.walls || []).slice(0, 3).map((w) => num(w.strike, 0)).join(" · ");
    return `<div class="regime-row">
      <div class="regime-sym">${sym}</div>
      <div class="regime-spot">${num(s.spot, 2)}</div>
      <div>
        <div class="regime-state ${stateCls}">${stateTxt}</div>
        <div class="regime-detail">${side} ${num(s.flip, 2)} · ${signed(s.dist_to_flip, 2)}</div>
      </div>
      <div class="regime-walls">walls ${walls}</div>
    </div>`;
  }).join("");
}

function renderIndices(ir) {
  const el = $("indices");
  if (!ir) { el.innerHTML = ""; return; }
  // All five come off the same Yahoo feed; NQ is the only one carrying an intraday
  // timestamp, so use it as the feed's lag proxy. ~10min is NORMAL (licence delay),
  // so only warn past 20.
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

function renderRotation(sr) {
  const el = $("rotation");
  if (!sr || !sr.ranked) { el.innerHTML = ""; return; }
  // 5-day relative returns — minutes of feed lag are immaterial here, so no age stamp.
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

function renderMacro(mp) {
  const el = $("macro");
  if (!mp) { el.innerHTML = ""; return; }
  const out = [];
  const y = mp.yields;
  // CNBC yields are genuinely live — if this ever drifts, the feed broke.
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
      <div class="v ${backw ? "down" : ""}" style="font-size:14px">${backw ? "BACKWARD" : "CONTANGO"}</div>
      <div class="d flat">${signed(ts.spread_9d_minus_30d, 2)}</div></div>`);
  }
  el.innerHTML = out.join("");
}

function renderEvents(eg) {
  const el = $("events");
  if (!eg) { el.innerHTML = ""; return; }
  const today = eg.today_events || [];
  const up = eg.upcoming || [];
  let h = "";
  if (today.length) {
    h += today.map((e) => `<div class="ev t1"><div class="t">${e.time_et || ""}</div><div class="n">${e.name || e}</div></div>`).join("");
  } else {
    h += `<div class="ev"><div class="t">—</div><div class="n">No T1 event today</div></div>`;
  }
  h += up.slice(0, 3).map((e) => {
    const d = e.date || "";
    return `<div class="ev"><div class="t">${d.slice(5)}</div><div class="n">${e.name || e}
      <span style="color:var(--muted)">${e.days_away !== undefined ? `+${e.days_away}d` : ""} ${e.time_et || ""}</span></div></div>`;
  }).join("");
  h += `<div class="ev"><div class="t"></div><div class="n" style="color:var(--muted);font-size:11px">
        T1 gate only — non-T1 prints (retail sales, claims, PPI) are not listed here.</div></div>`;
  el.innerHTML = h;
}

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
    renderRegime(sec.gex);
    renderIndices(sec.index_recap);
    renderRotation(sec.sector_rotation);
    renderMacro(sec.macro_pulse);
    renderEvents(sec.event_gate);
    renderHealth(s);
  } catch (e) {
    $("stamp").textContent = "server unreachable";
  }
}

tick();
setInterval(tick, 5000);
