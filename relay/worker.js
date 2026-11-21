/**
 * Ops Room — live market relay (Cloudflare Worker, free plan).
 *
 * Why it exists: browsers are blocked (CORS) from calling Yahoo Finance directly.
 * This worker fetches the quotes server-side and returns them with CORS enabled.
 *
 * Safety:
 *  - Only symbols in ALLOW can be requested (cannot be abused as an open proxy).
 *  - Responses are cached 15 s at the edge, so many viewers = few upstream calls.
 *
 * Endpoint:  GET /quotes   -> { ts, quotes: { SYMBOL: {price, prevClose, chgPct, time, delayedMin} }, crypto: {...} }
 */
const ALLOW = {
  // 22 symbols: worst case 22 x 2 hosts + 1 crypto = 45 subrequests (< 50 free-plan limit)
  "^VIX": "VIX", "DX-Y.NYB": "DXY",
  "GC=F": "GC", "SI=F": "SI", "CL=F": "CL", "HG=F": "HG", "ES=F": "ES", "NQ=F": "NQ", "ZN=F": "ZN",
  "SPY": "SPY", "RSP": "RSP",
  "XLK": "XLK", "XLF": "XLF", "XLV": "XLV", "XLE": "XLE", "XLY": "XLY", "XLP": "XLP",
  "XLI": "XLI", "XLU": "XLU", "XLB": "XLB", "XLRE": "XLRE", "XLC": "XLC",
};
const CACHE_SECONDS = 15;
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Content-Type": "application/json; charset=utf-8",
};

async function yahooQuote(sym) {
  const hosts = ["query1", "query2"];
  for (const h of hosts) {
    try {
      const url = `https://${h}.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(sym)}?range=1d&interval=1m`;
      const r = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0 (OpsRoom relay)" } });
      if (!r.ok) continue;
      const m = (await r.json()).chart.result[0].meta;
      const price = Number(m.regularMarketPrice);
      const prev = Number(m.chartPreviousClose ?? m.previousClose);
      if (!Number.isFinite(price)) continue;
      return {
        price,
        prevClose: Number.isFinite(prev) ? prev : null,
        chgPct: Number.isFinite(prev) && prev ? +(((price / prev) - 1) * 100).toFixed(2) : null,
        time: (m.regularMarketTime || 0) * 1000,
        delayedMin: m.exchangeDataDelayedBy ?? null,
      };
    } catch (_) { /* try next host */ }
  }
  return null;
}

async function cryptoQuotes() {
  try {
    const r = await fetch("https://api.hyperliquid.xyz/info", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "metaAndAssetCtxs" }),
    });
    const js = await r.json();
    const names = js[0].universe.map(u => u.name);
    const out = {};
    for (const a of ["BTC", "ETH"]) {
      const c = js[1][names.indexOf(a)];
      const px = Number(c.markPx), prev = Number(c.prevDayPx);
      out[a] = { price: px, prevClose: prev, chgPct: +(((px / prev) - 1) * 100).toFixed(2),
                 openInterest: Number(c.openInterest), time: Date.now(), delayedMin: 0 };
    }
    return out;
  } catch (_) {
    return {};
  }
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);
    if (url.pathname !== "/quotes") {
      return new Response(JSON.stringify({ ok: true, use: "/quotes" }), { headers: CORS });
    }
    const cache = caches.default;
    const key = new Request(url.origin + "/quotes", { method: "GET" });
    const hit = await cache.match(key);
    if (hit) return hit;

    const syms = Object.keys(ALLOW);
    const [results, crypto] = await Promise.all([Promise.all(syms.map(yahooQuote)), cryptoQuotes()]);
    const quotes = {};
    syms.forEach((s, i) => { if (results[i]) quotes[ALLOW[s]] = results[i]; });

    const res = new Response(JSON.stringify({ ts: Date.now(), quotes, crypto }), {
      headers: { ...CORS, "Cache-Control": `public, max-age=${CACHE_SECONDS}` },
    });
    ctx.waitUntil(cache.put(key, res.clone()));
    return res;
  },
};
