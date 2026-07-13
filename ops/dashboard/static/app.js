"use strict";
// Ops dashboard frontend. Read-only: polls /api/snapshot + /api/events every
// REFRESH_MS. DOM is built exclusively via createElement/textContent — journal
// payloads can carry arbitrary operator/ticker strings, so innerHTML is never
// used with data. XSS is impossible by construction, not by escaping.

const REFRESH_MS = 5000;
const SVG_NS = "http://www.w3.org/2000/svg";

let inFlight = false;       // req 1: skip a tick while a fetch is outstanding
let lastUpdate = null;      // Date of last fully-successful poll (banner text)
let feedFilter = "";        // selected kind in the client-side feed filter
const logState = {};        // per-file: has it been fetched into its <pre> yet

// ---- tiny DOM helpers (textContent only) -----------------------------------

function el(tag, opts, kids) {
  const n = document.createElement(tag);
  opts = opts || {};
  if (opts.class) n.className = opts.class;
  if (opts.text != null) n.textContent = opts.text;
  if (opts.attrs) {
    for (const k in opts.attrs) n.setAttribute(k, opts.attrs[k]);
  }
  for (const c of kids || []) if (c) n.appendChild(c);
  return n;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function isError(section) {
  return section && typeof section === "object" && "error" in section;
}

// A failed/omitted section renders one muted chip, never a blank panel (req 4).
function chip(node, err) {
  clear(node);
  node.appendChild(el("span", { class: "chip", text: "unavailable: " + err }));
}

function fmtTime(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso == null ? "" : iso);
  return d.toLocaleTimeString([], { hour12: false });
}

function relAge(iso) {
  const t = Date.parse(iso);
  if (isNaN(t)) return "";
  let s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function ageLabel(seconds) {
  if (seconds == null) return "—";
  const s = Math.round(seconds);
  if (s < 90) return s + "s";
  if (s < 5400) return Math.round(s / 60) + "m";
  return Math.round(s / 3600) + "h";
}

// ---- display formatting for Decimal strings ---------------------------------
// Money/qty arrive as full-precision Decimal strings. Display-rounding is done
// by string surgery (never parseFloat): a float round-trip could misrepresent
// the books; trimming digits for the eye cannot.

function roundDecStr(s, dp) {
  s = String(s);
  const neg = s.startsWith("-") || s.startsWith("−");
  if (neg) s = s.slice(1);
  if (!/^\d+(\.\d*)?$/.test(s)) return null;   // non-numeric: caller shows raw
  let [i, f = ""] = s.split(".");
  if (f.length <= dp) return { neg: neg, i: i, f: f.padEnd(dp, "0") };
  // Round half-up at the cut, carrying through the integer part if needed.
  const digits = (i + f.slice(0, dp)).split("");
  if (f.charCodeAt(dp) >= 53 /* '5' */) {
    let k = digits.length - 1;
    while (k >= 0 && digits[k] === "9") { digits[k] = "0"; k--; }
    if (k < 0) digits.unshift("1"); else digits[k] = String(+digits[k] + 1);
  }
  const all = digits.join("");
  const cut = all.length - dp;
  return { neg: neg, i: all.slice(0, cut).replace(/^0+(?=\d)/, ""), f: all.slice(cut) };
}

function fmtMoney(s) {
  if (s == null) return "—";
  const r = roundDecStr(s, 2);
  if (!r) return String(s);
  const grouped = r.i.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return (r.neg ? "−$" : "$") + grouped + "." + r.f;
}

function fmtQty(s) {
  if (s == null) return "—";
  const r = roundDecStr(s, 4);
  if (!r) return String(s);
  const f = r.f.replace(/0+$/, "");
  return (r.neg ? "−" : "") + r.i + (f ? "." + f : "");
}

// ---- fetch + polling loop --------------------------------------------------

async function fetchJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(url + " → HTTP " + r.status);
  return r.json();
}

async function tick() {
  if (inFlight) return;               // req 1: coalesce — never overlap fetches
  inFlight = true;
  try {
    const results = await Promise.allSettled([
      fetchJSON("/api/snapshot"),
      fetchJSON("/api/events?limit=80"),
    ]);
    const [snap, events] = results;
    if (snap.status === "fulfilled") renderSnapshot(snap.value);
    if (events.status === "fulfilled") renderFeed(events.value);

    if (snap.status === "fulfilled" && events.status === "fulfilled") {
      lastUpdate = new Date();
      setDisconnected(false);         // req 2: clears on next success
    } else {
      setDisconnected(true);
    }
  } catch (e) {
    setDisconnected(true);
  } finally {
    inFlight = false;
  }
}

function setDisconnected(down) {
  const b = document.getElementById("disconnected");
  if (!down) {
    b.hidden = true;
    return;
  }
  const when = lastUpdate ? lastUpdate.toLocaleTimeString([], { hour12: false }) : "never";
  b.textContent = "dashboard disconnected — last update " + when;
  b.hidden = false;
}

// ---- top-level render ------------------------------------------------------

function renderSnapshot(snap) {
  renderHealth(snap.health);
  renderMarket(snap.market);
  renderBanner(snap.health);
  renderSleeves(snap.sleeves);
  renderFunnel(snap.funnel);
  renderOvernight(snap.funnel);
  renderAnomalies(snap.anomalies_7d);
}

// ---- health strip + alert banner -------------------------------------------

const DOT_CLASS = { RUNNING: "green", STALE: "amber", STOPPED: "red", UNKNOWN: "red" };

function renderHealth(health) {
  const node = document.getElementById("health");
  if (isError(health)) return chip(node, health.error);
  clear(node);

  const verdict = health.verdict || "UNKNOWN";
  node.appendChild(el("span", { class: "dot " + (DOT_CLASS[verdict] || "red") }));
  node.appendChild(el("span", { class: "verdict", text: verdict }));
  node.appendChild(el("span", { class: "sep", text: "·" }));
  node.appendChild(el("span", { class: "muted", text: health.broker_mode || "—" }));
  node.appendChild(el("span", { class: "sep", text: "·" }));

  const g = health.guardian || {};
  node.appendChild(el("span", { class: "muted", text: "guardian " },
    [el("span", { class: "num", text: ageLabel(g.age_seconds) })]));
}

// req 3: STOPPED/STALE verdict, any halts.* true, or research_paused → banner.
function renderBanner(health) {
  const banner = document.getElementById("banner");
  if (isError(health)) {
    banner.hidden = true;
    return;
  }
  const conditions = [];
  let severe = false;
  const verdict = health.verdict;
  if (verdict === "STOPPED") { conditions.push("service STOPPED"); severe = true; }
  else if (verdict === "STALE") { conditions.push("service STALE — guardian not reporting"); }

  const halts = health.halts || {};
  if (halts.daily_halt_today) { conditions.push("daily drawdown halt in effect"); severe = true; }
  if (halts.kill_switch_this_week) { conditions.push("weekly kill-switch tripped"); severe = true; }
  if (health.research_paused) { conditions.push("research paused"); }

  if (conditions.length === 0) {
    banner.hidden = true;
    clear(banner);
    return;
  }
  clear(banner);
  banner.className = "banner " + (severe ? "red" : "amber");
  banner.appendChild(el("strong", { text: severe ? "ALERT" : "NOTICE" }));
  const ul = el("ul");
  for (const c of conditions) ul.appendChild(el("li", { text: c }));
  banner.appendChild(ul);
  banner.hidden = false;
}

function renderMarket(market) {
  const node = document.getElementById("market");
  if (isError(market)) return chip(node, market.error);
  clear(node);
  const open = market.is_open;
  node.appendChild(el("span", { class: "dot " + (open ? "green" : "red") }));
  node.appendChild(el("span", { text: "market " + (open ? "OPEN" : "CLOSED") }));
  const nextIso = open ? market.previous_close : market.next_open;
  if (nextIso) {
    node.appendChild(el("span", { class: "sep", text: "·" }));
    node.appendChild(el("span", {
      class: "muted",
      text: (open ? "prev close " : "next open ") + fmtTime(nextIso),
    }));
  }
}

// ---- sleeves: cards + positions + fills ------------------------------------

const SLEEVE_ORDER = ["momentum", "research", "baseline"];

function renderSleeves(sleeves) {
  const cards = document.getElementById("sleeves");
  const posNode = document.getElementById("positions");
  const fillNode = document.getElementById("fills");

  if (isError(sleeves)) {
    chip(cards, sleeves.error);
    chip(posNode, sleeves.error);
    chip(fillNode, sleeves.error);
    return;
  }
  clear(cards);
  const names = SLEEVE_ORDER.filter((n) => n in (sleeves || {}));
  for (const name of names) cards.appendChild(sleeveCard(name, sleeves[name]));

  renderPositions(posNode, sleeves, names);
  renderFills(fillNode, sleeves, names);
}

function sleeveCard(name, s) {
  const card = el("div", { class: "sleeve" });
  card.appendChild(el("div", { class: "sleeve-name", text: name }));
  if (isError(s)) {
    card.appendChild(el("div", { class: "chip", text: "unavailable: " + s.error }));
    return card;
  }
  // Equity display-rounds to cents (string surgery, no float); the raw
  // full-precision figure stays one hover away in the tooltip.
  const eq = el("div", { class: "sleeve-equity num", text: fmtMoney(s.equity) });
  if (s.equity != null) eq.setAttribute("title", s.equity);
  card.appendChild(eq);

  const row = el("div", { class: "sleeve-row" });
  const pnls = el("div", { class: "pnls" }, [
    labeledPnl("day", s.day_pnl_pct),
    labeledPnl("life", s.lifetime_pnl_pct),
  ]);
  row.appendChild(pnls);
  const spark = sparkline(s.series);
  if (spark) row.appendChild(spark);
  card.appendChild(row);
  card.appendChild(el("div", { class: "sleeve-cash muted num", text: "cash " + fmtMoney(s.cash) }));
  return card;
}

function labeledPnl(label, pct) {
  return el("span", { class: "pnl-pair" }, [
    el("span", { class: "pnl-lbl", text: label }),
    pnlSpan(pct),
  ]);
}

// req 5: +4.00% green / −1.20% red / — when null.
function pnlSpan(pct) {
  if (pct == null) return el("span", { class: "pnl flat num", text: "—" });
  const v = parseFloat(pct);                    // ratio, not money — float ok
  if (isNaN(v)) return el("span", { class: "pnl flat num", text: "—" });
  const cls = v > 0 ? "green" : v < 0 ? "red" : "flat";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  const txt = sign + Math.abs(v * 100).toFixed(2) + "%";
  return el("span", { class: "pnl " + cls + " num", text: txt });
}

// req 5: inline SVG polyline, 120×28, single accent stroke, no axes.
function sparkline(series) {
  if (!Array.isArray(series) || series.length < 2) return null;
  const W = 120, H = 28, P = 2;
  // parseFloat is allowed here: y-scaling only, float loss is cosmetic (req 8).
  const ys = series.map((p) => parseFloat(p.equity));
  if (ys.some((y) => isNaN(y))) return null;
  let lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
  if (hi === lo) { hi += 1; lo -= 1; }
  const n = ys.length;
  const pts = ys.map((y, i) => {
    const x = P + (i / (n - 1)) * (W - 2 * P);
    const yy = P + (1 - (y - lo) / (hi - lo)) * (H - 2 * P);
    return x.toFixed(1) + "," + yy.toFixed(1);
  }).join(" ");

  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("width", String(W));
  svg.setAttribute("height", String(H));
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  const line = document.createElementNS(SVG_NS, "polyline");
  line.setAttribute("points", pts);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "var(--accent)");
  line.setAttribute("stroke-width", "2");
  line.setAttribute("stroke-linecap", "round");
  line.setAttribute("stroke-linejoin", "round");
  svg.appendChild(line);
  return svg;
}

// Positions live in one small expandable panel per sleeve. The DOM is rebuilt
// every poll, so each <details>' open state must survive in posOpen, not in
// the elements themselves.
const posOpen = { momentum: false, research: false, baseline: false };

function renderPositions(node, sleeves, names) {
  clear(node);
  for (const name of names) {
    const s = sleeves[name];
    const details = el("details", { class: "pos-group" });
    details.open = !!posOpen[name];
    details.addEventListener("toggle", () => { posOpen[name] = details.open; });

    const summary = el("summary");
    summary.appendChild(el("span", { class: "pos-name", text: name }));
    details.appendChild(summary);

    if (isError(s)) {
      summary.appendChild(el("span", { class: "pos-count chip", text: "unavailable" }));
      node.appendChild(details);
      continue;
    }
    const positions = s.positions || [];
    summary.appendChild(el("span", {
      class: "pos-count num",
      text: positions.length + (positions.length === 1 ? " position" : " positions"),
    }));

    const body = el("div", { class: "pos-body scroll-x" });
    if (positions.length === 0) {
      body.appendChild(el("span", { class: "chip", text: "no open positions" }));
    } else {
      const rows = positions.map((p) => [
        p.symbol, fmtQty(p.quantity), fmtMoney(p.entry),
        p.stop == null ? null : fmtMoney(p.stop),
      ]);
      body.appendChild(simpleTable(["symbol", "qty", "entry", "stop"], rows,
        { 1: true, 2: true, 3: true }));
    }
    details.appendChild(body);
    node.appendChild(details);
  }
}

function renderFills(node, sleeves, names) {
  const rows = [];
  for (const name of names) {
    const s = sleeves[name];
    if (isError(s)) continue;
    for (const f of s.fills_today || []) {
      rows.push([fmtTime(f.filled_at), name, f.side, f.symbol, fmtQty(f.quantity), fmtMoney(f.price)]);
    }
  }
  if (rows.length === 0) return chip(node, "no fills today");
  const table = simpleTable(
    ["time", "sleeve", "side", "symbol", "qty", "price"],
    rows,
    { 4: true, 5: true }
  );
  clear(node);
  node.appendChild(table);
}

// Build a table via createElement. Values are stringified with textContent, so
// arbitrary payload strings can never inject markup. numericCols right-aligns.
function simpleTable(headers, rows, numericCols) {
  numericCols = numericCols || {};
  const table = el("table");
  const thead = el("thead");
  const htr = el("tr");
  headers.forEach((h, i) => htr.appendChild(el("th", { class: numericCols[i] ? "n" : "", text: h })));
  thead.appendChild(htr);
  table.appendChild(thead);
  const tbody = el("tbody");
  for (const r of rows) {
    const tr = el("tr");
    r.forEach((c, i) => {
      const cell = el("td", { class: (numericCols[i] ? "n num" : ""), text: c == null ? "—" : String(c) });
      tr.appendChild(cell);
    });
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

// ---- activity feed ---------------------------------------------------------

function renderFeed(events) {
  const node = document.getElementById("feed");
  if (isError(events)) return chip(node, events.error);
  if (!Array.isArray(events)) return chip(node, "malformed events");

  refreshFilterOptions(events);
  clear(node);
  const shown = feedFilter ? events.filter((e) => e.kind === feedFilter) : events;
  if (shown.length === 0) {
    node.appendChild(el("span", { class: "chip", text: "no events" }));
    return;
  }
  for (const e of shown) node.appendChild(feedRow(e));
}

function feedRow(e) {
  const row = el("div", { class: "event" });
  row.appendChild(el("span", { class: "time num", text: fmtTime(e.at) }));
  row.appendChild(el("span", { class: "tag " + (e.source || ""), text: e.source || "?" }));
  row.appendChild(el("span", { class: "text", text: e.text || e.kind || "" }));
  row.appendChild(el("span", { class: "age", text: relAge(e.at) }));
  return row;
}

// req 6: kind <select> populated from the kinds present; preserves selection.
function refreshFilterOptions(events) {
  const sel = document.getElementById("feed-filter");
  const kinds = Array.from(new Set(events.map((e) => e.kind).filter(Boolean))).sort();
  const want = ["", ...kinds];
  const have = Array.from(sel.options).map((o) => o.value);
  if (want.length === have.length && want.every((v, i) => v === have[i])) return;
  const prev = sel.value;
  clear(sel);
  sel.appendChild(el("option", { attrs: { value: "" }, text: "all" }));
  for (const k of kinds) sel.appendChild(el("option", { attrs: { value: k }, text: k }));
  sel.value = kinds.includes(prev) ? prev : "";
  feedFilter = sel.value;
}

// ---- research funnel -------------------------------------------------------

function renderFunnel(funnel) {
  const node = document.getElementById("funnel");
  if (isError(funnel)) return chip(node, funnel.error);
  clear(node);

  const scr = funnel.screener || {};
  const last = scr.last_run;
  if (last) {
    node.appendChild(kv("screen", last.passed_count + " / " + last.universe_size +
      " passed · " + (last.asof || "")));
  } else {
    node.appendChild(kv("screen", "no run recorded"));
  }

  const memos = funnel.memos || {};
  node.appendChild(el("div", { class: "muted", text: "memos by status" }));
  node.appendChild(pillRow(memos.by_status || {}));

  const sig = funnel.signals_7d || {};
  node.appendChild(el("div", { class: "muted", text: "signals (7d)" }));
  node.appendChild(pillRow(sig));

  const open = memos.open || [];
  if (open.length) {
    const rows = open.map((m) => [m.ticker, m.thesis_type, m.conviction_tier, m.status]);
    node.appendChild(simpleTable(["ticker", "thesis", "tier", "status"], rows, {}));
  }
}

function renderOvernight(funnel) {
  const node = document.getElementById("overnight");
  if (isError(funnel)) return chip(node, funnel.error);
  const ov = funnel.overnight;
  if (isError(ov)) return chip(node, ov.error);
  clear(node);
  if (!ov) return chip(node, "no overnight data");

  node.appendChild(runLine("vetting", ov.last_vetting_run));
  node.appendChild(runLine("drain", ov.last_drain_run));
  if (ov.paused) {
    node.appendChild(el("div", { class: "paused", text: "◼ overnight window PAUSED" }));
  } else {
    node.appendChild(el("div", { class: "muted", text: "window active" }));
  }
}

function runLine(label, run) {
  if (!run) return kv(label, "—");
  const at = run.at ? fmtTime(run.at) + " (" + relAge(run.at) + ")" : "—";
  return kv(label, at);
}

function kv(k, v) {
  return el("div", { class: "kv" }, [
    el("span", { class: "k", text: k }),
    el("span", { class: "num", text: String(v) }),
  ]);
}

function pillRow(counts) {
  const wrap = el("div", { class: "pills" });
  const keys = Object.keys(counts);
  if (keys.length === 0) {
    wrap.appendChild(el("span", { class: "chip", text: "none" }));
    return wrap;
  }
  for (const k of keys) {
    wrap.appendChild(el("span", { class: "pill" }, [
      document.createTextNode(k + " "),
      el("span", { class: "c num", text: String(counts[k]) }),
    ]));
  }
  return wrap;
}

// ---- anomalies -------------------------------------------------------------

function renderAnomalies(anom) {
  const node = document.getElementById("anomalies");
  if (isError(anom)) return chip(node, anom.error);
  const kinds = Object.keys(anom || {});
  // Only surface anomalies that actually occurred — count 0 is noise.
  const rows = kinds
    .filter((k) => (anom[k] && anom[k].count) > 0)
    .map((k) => [k, anom[k].count, anom[k].last_at ? relAge(anom[k].last_at) : "—"]);
  if (rows.length === 0) return chip(node, "none in last 7 days");
  clear(node);
  node.appendChild(simpleTable(["kind", "count", "last"], rows, { 1: true }));
}

// ---- logs (built once; fetch-on-open, req 7) -------------------------------

function buildLogs() {
  const node = document.getElementById("logs");
  clear(node);
  for (const file of ["out", "err"]) {
    const details = el("details");
    details.appendChild(el("summary", { text: "ops." + file + ".log" }));
    const head = el("div", { class: "log-head" });
    const btn = el("button", { text: "refresh" });
    head.appendChild(btn);
    const pre = el("pre", { text: "" });
    details.appendChild(head);
    details.appendChild(pre);
    node.appendChild(details);

    const load = () => loadLog(file, pre);
    // Fetch once on first open; refresh button re-fetches on demand.
    details.addEventListener("toggle", () => {
      if (details.open && !logState[file]) { logState[file] = true; load(); }
    });
    btn.addEventListener("click", load);
  }
}

async function loadLog(file, pre) {
  pre.textContent = "loading…";
  try {
    const data = await fetchJSON("/api/logs?file=" + encodeURIComponent(file) + "&lines=200");
    pre.textContent = data.text ? data.text : "(empty)";
  } catch (e) {
    pre.textContent = "unavailable: " + e.message;
  }
}

// ---- boot ------------------------------------------------------------------

function init() {
  document.getElementById("feed-filter").addEventListener("change", (ev) => {
    feedFilter = ev.target.value;
  });
  buildLogs();
  tick();
  setInterval(tick, REFRESH_MS);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
