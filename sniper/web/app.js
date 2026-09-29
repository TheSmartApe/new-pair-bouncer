// Polls /api/state and redraws. Plain JS, no build step.
const POLL_MS = 3000;
const $ = (id) => document.getElementById(id);
const seenPools = new Set();
const seenAlerts = new Set();
let firstLoad = true;

const short = (w) => (w ? `${w.slice(0, 6)}…${w.slice(-4)}` : "–");
const num = (n) => (n == null ? "–" : Number(n).toLocaleString("en-US"));
function usd(x, signed = false) {
  if (x == null) return "–";
  const sign = signed ? (x > 0 ? "+" : x < 0 ? "−" : "") : x < 0 ? "−" : "";
  const a = Math.abs(x);
  const body = a >= 1e6 ? `${(a / 1e6).toFixed(2)}M` : a >= 1e4 ? `${(a / 1e3).toFixed(1)}K` : a >= 100 ? Math.round(a).toLocaleString("en-US") : a.toFixed(a >= 10 ? 0 : 2);
  return `${sign}$${body}`;
}
function ago(ts, now) {
  const s = Math.max(0, Math.round(now - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)}h ago`;
}
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString("en-GB", { hour12: false });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function renderHeader(s) {
  $("chain").textContent = s.chains.map((c) => (c === "robinhood" ? "Robinhood Chain" : c)).join(" · ");
  const age = s.now - (s.last_sweep || 0);
  $("dot").className = "dot " + (age < s.interval_s * 2.5 ? "live" : "stale");
  $("sweep").textContent = s.last_sweep ? `live · last sweep ${ago(s.last_sweep, s.now)}` : "waiting for the collector";
  $("credits").textContent = `${num(Math.round(s.credits_today))} credits today`;
  $("snipe-s").textContent = s.snipe_s;
}

function renderKpis(s) {
  const l24 = s.last24 || {};
  $("k-launches").textContent = num(s.launches_captured);
  const hours = s.tracking_since ? (s.now - s.tracking_since) / 3600 : 0;
  $("k-launches-sub").textContent = `first 2 minutes of every new pool · ${hours < 48 ? hours.toFixed(1) + "h" : (hours / 24).toFixed(1) + " days"} tracked`;
  $("k-sniper").textContent = l24.with_sniper_pct == null ? "–" : `${Math.round(l24.with_sniper_pct)}%`;
  $("k-bots").textContent = l24.with_bot_pct == null ? "–" : `${Math.round(l24.with_bot_pct)}%`;
  const c = l24.classes || {};
  const total = (c.serial_sniper || 0) + (c.round_tripper || 0) + (c.dust_bot || 0) + (c.serial_launcher || 0);
  $("k-wallets").textContent = num(total);
  $("k-wallets-sub").textContent = `${num(c.serial_sniper || 0)} snipers · ${num(c.round_tripper || 0)} round-trip bots · ${num(c.dust_bot || 0)} dust bots · ${num(c.serial_launcher || 0)} devs`;
}

function renderFeed(s) {
  const rows = s.feed.map((f) => {
    const fresh = !firstLoad && !seenPools.has(f.pool);
    seenPools.add(f.pool);
    const [token, quote] = (f.name || "?").split(" / ");
    const snipers = f.serial_snipers
      ? `<span class="tag sniper">${f.serial_snipers} sniper${f.serial_snipers > 1 ? "s" : ""}</span>${f.top_sniper ? `<span class="none mono">${short(f.top_sniper.wallet)} · ${f.top_sniper.launches} launches</span>` : ""}`
      : `<span class="none">–</span>`;
    const bots = f.bots ? `<span class="tag bot">${f.bots} bot${f.bots > 1 ? "s" : ""} · ${Math.round(f.bot_share * 100)}% of buyers</span>` : `<span class="none">–</span>`;
    const dev = f.launcher ? `<span class="tag dev">dev bought</span>` : "";
    return `<tr class="${fresh ? "fresh" : ""}">
      <td class="time">${clock(f.created_ts)}</td>
      <td class="token">${esc(token)}<span class="dex">/ ${esc(quote || "")}</span> ${dev}</td>
      <td class="num">${f.buyers}</td>
      <td class="num">${f.first10}</td>
      <td>${snipers}</td>
      <td>${bots}</td>
      <td class="num">${usd(f.snipe_usd)}</td>
    </tr>`;
  });
  $("feed").innerHTML = rows.join("");
}

function renderMoney(s) {
  const m = s.money;
  const c = m && m.classes && m.classes.serial_sniper;
  if (!c) {
    $("m-foot").textContent = "run `python -m sniper money` to fill this in";
    return;
  }
  $("m-in").textContent = usd(c.put_in_usd);
  $("m-out").textContent = usd(c.took_out_usd);
  $("m-net").textContent = usd(c.net_cash_usd, true);
  const w = c.winners_usd, l = Math.abs(c.losers_usd);
  $("m-winbar").style.width = `${(100 * w) / (w + l || 1)}%`;
  $("m-losebar").style.width = `${(100 * l) / (w + l || 1)}%`;
  $("m-win").textContent = `${c.cash_winners} made ${usd(w, true)}`;
  $("m-lose").textContent = `${c.wallets - c.cash_winners} lost ${usd(-l, true)}`;
  const wp = c.winners_profile, lp = c.losers_profile;
  $("m-speed").innerHTML = `Winners buy <b class="green">+${wp.median_entry_s}s</b> after launch, losers <b class="red">+${lp.median_entry_s}s</b>. Same size: ${usd(wp.median_buy_usd)} vs ${usd(lp.median_buy_usd)}.`;
  $("m-positions").innerHTML = `<b>${Math.round(c.positions_cash_positive_pct)}%</b> of ${num(c.positions)} snipes made money · <b>${Math.round(c.positions_2x_pct)}%</b> doubled · median snipe ${usd(c.median_position_cash_usd, true)}`;
  const b = m.best_position;
  $("m-best").innerHTML = b ? `Best: <span class="mono">${short(b.wallet)}</span> put ${usd(b.buy_usd)} into ${esc(b.symbol || short(b.token))}, took out <b class="green">${usd(b.sell_usd)}</b>` : "";
  $("m-foot").textContent = `cash in vs cash out on the tokens they sniped · ${c.wallets} wallets · CoinGecko wallet PnL · updated ${ago(m.computed_ts, s.now)}`;
  $("leaders").innerHTML = (m.leaderboard || []).slice(0, 6).map((r) => `<tr>
      <td class="mono">${short(r.wallet)}</td>
      <td class="num">${r.launches}</td>
      <td class="num">+${r.entry_s}s</td>
      <td class="num ${r.cash_usd >= 0 ? "green" : "red"}">${usd(r.cash_usd, true)}</td>
    </tr>`).join("");
}

function renderAlerts(s) {
  $("alerts").innerHTML = s.alerts.map((a) => {
    const key = `${a.ts}-${a.wallet}`;
    const fresh = !firstLoad && !seenAlerts.has(key);
    seenAlerts.add(key);
    const [token] = (a.name || "?").split(" / ");
    return `<li class="${fresh ? "fresh" : ""}"><span class="a-time">${clock(a.ts)}</span><span><span class="mono">${short(a.wallet)}</span> <span class="none">(${a.prior_launches} launches)</span> sniped <b>${esc(token)}</b> at +${Math.round(a.sec_offset)}s${a.count > 1 ? ` <span class="none">with ${a.count - 1} more</span>` : ""}</span></li>`;
  }).join("");
}

async function tick() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    const s = await r.json();
    if (s.error) throw new Error(s.error);
    renderHeader(s);
    renderKpis(s);
    renderFeed(s);
    renderMoney(s);
    renderAlerts(s);
    firstLoad = false;
  } catch (e) {
    $("dot").className = "dot stale";
    $("sweep").textContent = "dashboard can't read the database";
  }
}

tick();
setInterval(tick, POLL_MS);
