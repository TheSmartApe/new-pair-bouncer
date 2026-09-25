// Badge, footer, locked-card helper, and a cached capabilities() fetch. Plain JS, no build step.

const LOGOS = {
  light: "/brand/coingecko-api-on-light.svg",
  dark: "/brand/coingecko-api-on-dark.svg",
  mono: "/brand/coingecko-api-mono.svg",
};

const API_URL = "https://www.coingecko.com/en/api";
const PRICING_URL = "https://www.coingecko.com/en/api/pricing";
const DOCS_URL = "https://docs.coingecko.com";

function themeMode() {
  // Reads --theme-mode off <html>, so a reskin only has to change theme.css.
  const raw = getComputedStyle(document.documentElement).getPropertyValue("--theme-mode").trim();
  return LOGOS[raw] ? raw : "light";
}

function renderBadge(el) {
  const mode = themeMode();
  el.innerHTML = `<a href="${API_URL}" target="_blank" rel="noopener"><img src="${LOGOS[mode]}" alt="Data powered by CoinGecko API" style="height:28px"></a>`;
}

function renderFooter(el) {
  el.innerHTML = `
    <div class="cg-footer">
      <a href="${DOCS_URL}" target="_blank" rel="noopener">Docs</a>
      <a href="${PRICING_URL}" target="_blank" rel="noopener">Pricing</a>
      <a href="${API_URL}" target="_blank" rel="noopener">CoinGecko API</a>
      <p class="cg-disclaimer">Data comes from the CoinGecko API. Nothing here is financial advice, and paper trades are simulated — no real orders are placed.</p>
    </div>`;
}

function lockedCard(message, upgradeUrl) {
  return `<div class="cg-locked"><p>${message}</p><a href="${upgradeUrl || PRICING_URL}" target="_blank" rel="noopener">See plans →</a></div>`;
}

let _capabilities = null;
async function capabilities() {
  // Cached for the page's lifetime: the plan doesn't change mid-session.
  if (!_capabilities) {
    _capabilities = fetch("/api/capabilities").then((r) => r.json());
  }
  return _capabilities;
}

window.CGBrand = { renderBadge, renderFooter, lockedCard, capabilities };
