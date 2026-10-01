// Polls /api/state and redraws. Plain JS, no build step.
const POLL_MS = 3000;
const $ = (id) => document.getElementById(id);
const seen = new Set();
const seenAlerts = new Set();
let firstLoad = true;

const num = (n) => (n == null ? "–" : Number(n).toLocaleString("en-US"));
const pct = (x, digits = 0) => (x == null ? "–" : `${(100 * x).toFixed(digits)}%`);
function usd(x, signed = false) {
  if (x == null) return "–";
  const sign = signed ? (x > 0 ? "+" : x < 0 ? "−" : "") : x < 0 ? "−" : "";
  const a = Math.abs(x);
  const body = a >= 1e6 ? `${(a / 1e6).toFixed(2)}M` : a >= 1e4 ? `${(a / 1e3).toFixed(1)}K` : a >= 100 ? Math.round(a).toLocaleString("en-US") : a.toFixed(a >= 10 ? 0 : 2);
  return `${sign}$${body}`;
}
const signedPct = (x) => (x == null ? "–" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x).toFixed(1)}%`);
function ago(ts, now) {
  const s = Math.max(0, Math.round(now - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)}h ago`;
}
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("en-GB", { hour12: false });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const tone = (x) => (x == null ? "" : x > 0 ? "green" : x < 0 ? "red" : "");

function renderHeader(s) {
  $("chain").textContent = s.chains.map((c) => (c === "robinhood" ? "Robinhood Chain" : c)).join(" · ");
  const age = s.now - (s.last_sweep || 0);
  $("dot").className = "dot " + (age < s.interval_s * 2.5 ? "live" : "stale");
  $("sweep").textContent = s.last_sweep ? `live · last sweep ${ago(s.last_sweep, s.now)}` : "waiting for the collector";
  $("credits").textContent = `${num(Math.round(s.credits_today))} credits today`;
}

function renderKpis(s) {
  const b = s.bouncer || {};
  const c = b.counts_24h || {};
  const total = (c.ENTER || 0) + (c.WATCH || 0) + (c.AVOID || 0);
  $("k-checked").textContent = num(total);
  $("k-checked-sub").textContent = `last 24h · ${num(c.AVOID || 0)} turned away`;
  $("k-enter").textContent = total ? `${Math.round((100 * (c.ENTER || 0)) / total)}%` : "–";
  $("k-enter-sub").textContent = `${num(c.ENTER || 0)} pairs passed every check`;
  const bk = (b.books || {}).bouncer || {}, ck = (b.books || {}).control || {};
  $("k-books").innerHTML = `<span class="${tone(bk.return_pct)}">${signedPct(bk.return_pct)}</span> <span class="vs">vs</span> <span class="${tone(ck.return_pct)}">${signedPct(ck.return_pct)}</span>`;
  $("k-books-sub").textContent = `${num(bk.positions || 0)} vs ${num(ck.positions || 0)} pairs · $100 each, live`;
  const bt = s.backtest || {};
  if (bt.bouncer && bt.control) {
    $("k-bt").innerHTML = `<span class="${tone(bt.bouncer.paper_return_pct)}">${signedPct(bt.bouncer.paper_return_pct)}</span> <span class="vs">vs</span> <span class="${tone(bt.control.paper_return_pct)}">${signedPct(bt.control.paper_return_pct)}</span>`;
    $("k-bt-sub").textContent = `${num(bt.bouncer.with_outcome)} pairs let in vs ${num(bt.control.with_outcome)} bought blind`;
  }
}

function verdictPill(v) {
  const cls = v === "ENTER" ? "enter" : v === "WATCH" ? "watch" : "avoid";
  return `<span class="tag ${cls}">${v}</span>`;
}

function renderFeed(s) {
  const feed = (s.bouncer || {}).feed || [];
  $("feed").innerHTML = feed.map((f) => {
    const fresh = !firstLoad && !seen.has(f.pool);
    seen.add(f.pool);
    const [token, quote] = (f.name || "?").split(" / ");
    const why = f.verdict === "ENTER" ? `<span class="none">passed ${f.passed} of ${f.checks} checks</span>` : esc(f.reasons[0] || "");
    return `<tr class="${fresh ? "fresh" : ""} ${f.verdict === "ENTER" ? "row-enter" : ""}">
      <td class="time">${clock(f.decided_ts)}</td>
      <td class="token">${esc(token)}<span class="dex">/ ${esc(quote || "")}</span></td>
      <td>${verdictPill(f.verdict)}</td>
      <td class="why" title="${esc(f.reasons.join(" · "))}">${why}</td>
      <td class="num">${f.buyers ?? "–"}</td>
      <td class="num">${pct(f.top3)}</td>
      <td class="num">${pct(f.bots)}</td>
    </tr>`;
  }).join("");
}

function renderBooks(s) {
  const b = s.bouncer || {};
  const r = b.rules || {};
  $("rules").textContent = `TP +${r.take_profit_pct}% · SL −${r.stop_loss_pct}% · max ${r.max_hold_min} min`;
  const row = (name, label, k) => `<tr>
      <td><b>${label}</b></td><td class="num">${num(k.positions || 0)}</td><td class="num">${num(k.open || 0)}</td>
      <td class="num">${k.win_rate == null ? "–" : pct(k.win_rate)}</td>
      <td class="num ${tone(k.pnl_usd)}">${usd(k.pnl_usd, true)}</td><td class="num ${tone(k.return_pct)}">${signedPct(k.return_pct)}</td></tr>`;
  const books = b.books || {};
  $("books").innerHTML = row("bouncer", "Bouncer", books.bouncer || {}) + row("control", "Buy everything", books.control || {});
}

function renderBacktest(s) {
  const bt = s.backtest || {};
  if (!bt.bouncer) {
    $("bt").innerHTML = `<li class="none">${esc(bt.error || "needs launches with +60m outcomes")}</li>`;
    return;
  }
  const b = bt.bouncer, c = bt.control;
  $("bt").innerHTML = [
    `Bought blind ~5 min after launch, held up to 1h: <b class="${tone(c.paper_return_pct)}">${signedPct(c.paper_return_pct)}</b> over ${num(c.with_outcome)} pairs`,
    `Only the pairs it let in: <b class="${tone(b.paper_return_pct)}">${signedPct(b.paper_return_pct)}</b> over ${num(b.with_outcome)} pairs`,
    esc(bt.worst_avoided.replace(" were not ENTER", " were turned away")),
    `Top reasons to say no: ${Object.entries(bt.fail_reasons || {}).slice(0, 3).map(([k, v]) => `${k.replace(/_/g, " ")} (${num(v)})`).join(", ")}`,
  ].map((x) => `<li>${x}</li>`).join("");
}

function renderAlerts(s) {
  const alerts = (s.bouncer || {}).cluster_alerts || [];
  $("alerts").innerHTML = alerts.map((a) => {
    const key = `${a.ts}-${a.name}`;
    const fresh = !firstLoad && !seenAlerts.has(key);
    seenAlerts.add(key);
    const [token] = (a.name || "?").split(" / ");
    return `<li class="${fresh ? "fresh" : ""}"><span class="a-time">${clock(a.ts)}</span><span><b>${esc(token)}</b> <span class="tag avoid">AVOID</span> ${esc(a.reason)}</span></li>`;
  }).join("") || `<li class="none">none yet</li>`;
}

async function tick() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    const s = await r.json();
    if (s.error) throw new Error(s.error);
    renderHeader(s);
    renderKpis(s);
    renderFeed(s);
    renderBooks(s);
    renderBacktest(s);
    renderAlerts(s);
    firstLoad = false;
  } catch (e) {
    $("dot").className = "dot stale";
    $("sweep").textContent = "dashboard can't read the database";
  }
}

tick();
setInterval(tick, POLL_MS);
