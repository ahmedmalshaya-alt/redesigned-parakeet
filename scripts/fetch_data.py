#!/usr/bin/env python3
"""
Ops Room data pipeline (Phase 2) — standard library only, no pip installs.

Runs in GitHub Actions twice a day and writes data.json at the repo root.

Rules (agreed design):
  * A failing source never erases a value: the last good value is kept with its ORIGINAL date,
    so the dashboard marks it stale on its own.
  * A new "observation" exists only when the data DATE of a systemic indicator changes
    (weekends / two runs on the same day do not create observations).
  * Scores = rolling percentile (0-100, higher = more stress) over ~3 years
    (756 daily points / 156 weekly points). Shorter history is flagged with n.
  * The FRED key is read from the FRED_API_KEY secret and is never written anywhere.
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data.json")
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (OpsRoom data bot; GitHub Actions)"}
NOW = dt.datetime.now(dt.timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
START = (NOW - dt.timedelta(days=int(365 * 4.5))).strftime("%Y-%m-%d")
WIN_D, WIN_W = 756, 156
SYSTEMIC = ["netLiquidity", "repoSofr", "dxy", "hygOas", "yieldCurve", "realYield"]
ALL_IDS = SYSTEMIC + ["vix", "breadth", "skewPcr", "crossAsset"]

errors = []


def log_err(where, exc):
    msg = f"{where}: {type(exc).__name__}: {exc}"
    if FRED_KEY:
        msg = msg.replace(FRED_KEY, "***")
    errors.append(msg[:300])
    print("WARN", msg[:300], file=sys.stderr)


def http(url, data=None, headers=None, timeout=30, retries=2):
    h = dict(UA)
    h.update(headers or {})
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def date_ms(d):
    return int(dt.datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


# ------------------------------------------------------------------ math helpers
def pct_rank(window, x):
    if not window:
        return None
    below = sum(1 for v in window if v < x)
    equal = sum(1 for v in window if v == x)
    return 100.0 * (below + 0.5 * equal) / len(window)


def score(vals, win, invert=False):
    w = vals[-win:]
    p = pct_rank(w, w[-1])
    if p is None:
        return None
    return round(100 - p if invert else p, 1)


def diff(series, lag):
    return [(series[i][0], series[i][1] - series[i - lag][1]) for i in range(lag, len(series))]


def pchg(series, lag):
    return [(series[i][0], (series[i][1] / series[i - lag][1] - 1) * 100)
            for i in range(lag, len(series)) if series[i - lag][1]]


def asof(series, d):
    """Latest value with date <= d (series sorted ascending)."""
    best = None
    for sd, v in series:
        if sd <= d:
            best = v
        else:
            break
    return best


# ------------------------------------------------------------------ sources
def fred(sid):
    if not FRED_KEY:
        raise RuntimeError("FRED_API_KEY secret is missing")
    q = urllib.parse.urlencode({"series_id": sid, "api_key": FRED_KEY,
                                "file_type": "json", "observation_start": START})
    js = json.loads(http("https://api.stlouisfed.org/fred/series/observations?" + q))
    return [(o["date"], float(o["value"])) for o in js["observations"] if o["value"] not in (".", "")]


def stooq(sym):
    txt = http(f"https://stooq.com/q/d/l/?s={sym}&i=d")
    rows = list(csv.DictReader(io.StringIO(txt)))
    out = [(r["Date"], float(r["Close"])) for r in rows if r.get("Close") not in (None, "", "N/D")]
    if len(out) < 3:
        raise RuntimeError(f"stooq {sym}: no data ({txt[:60]!r})")
    return out


def cboe_skew():
    txt = http("https://cdn.cboe.com/api/global/us_indices/daily_prices/SKEW_History.csv")
    rows = list(csv.reader(io.StringIO(txt)))
    out = []
    for r in rows[1:]:
        if len(r) < 2:
            continue
        try:
            d = dt.datetime.strptime(r[0].strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
            out.append((d, float(r[-1])))
        except ValueError:
            continue
    if len(out) < 50:
        raise RuntimeError("SKEW csv: unexpected format")
    return sorted(out)


def cot(code, n=3):
    q = urllib.parse.urlencode({"cftc_contract_market_code": code,
                                "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": n})
    js = json.loads(http("https://publicreporting.cftc.gov/resource/6dca-aqww.json?" + q))
    return [(r["report_date_as_yyyy_mm_dd"][:10], float(r["open_interest_all"])) for r in js]


# ------------------------------------------------------------------ previous state
def load_prev():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


PREV = load_prev()
PREV_IND = PREV.get("indicators", {})
PREV_RAW = PREV.get("raw", {})


def keep_prev(ind_id, raw_keys=()):
    """Source failed: keep last good value + its original date (becomes stale naturally)."""
    if ind_id in PREV_IND:
        indicators[ind_id] = dict(PREV_IND[ind_id], kept=True)
    for k in raw_keys:
        if k in PREV_RAW:
            raw[k] = PREV_RAW[k]


indicators, raw = {}, {}


def put(ind_id, sc, date, source, freq, n):
    indicators[ind_id] = {"score": sc, "date": date, "ts": date_ms(date),
                          "source": source, "freq": freq, "n": n}


# ------------------------------------------------------------------ indicators
def ind_net_liquidity():
    walcl, tga, rrp = fred("WALCL"), fred("WTREGEN"), fred("RRPONTSYD")
    net = []
    for d, w in walcl:  # weekly (Wednesday)
        t, r = asof(tga, d), asof(rrp, d)
        if t is None or r is None:
            continue
        net.append((d, w / 1000.0 - t - r))  # WALCL millions -> billions; TGA, RRP billions
    if not net or not (1000 < net[-1][1] < 15000):
        raise RuntimeError(f"net liquidity sanity check failed: {net[-1] if net else None}")
    ch = diff(net, 4)  # 4-week change, billions
    put("netLiquidity", score([v for _, v in ch], WIN_W, invert=True), ch[-1][0], "FRED", "weekly", len(ch[-WIN_W:]))
    raw["netLiqB"] = round(ch[-1][1])
    raw["netLiqLevelB"] = round(net[-1][1])


def ind_repo():
    sofr, iorb = dict(fred("SOFR")), fred("IORB")
    spread = [(d, (sofr[d] - v) * 100) for d, v in iorb if d in sofr]
    put("repoSofr", score([v for _, v in spread], WIN_D), spread[-1][0], "FRED", "daily", len(spread[-WIN_D:]))
    raw["sofrBp"] = round(spread[-1][1], 1)


def ind_dollar():
    s = fred("DTWEXBGS")
    ch = pchg(s, 63)  # ~3-month % change
    sc = score([v for _, v in ch], WIN_D)
    put("dxy", sc, s[-1][0], "FRED", "weekly", len(ch[-WIN_D:]))  # published weekly with daily values
    raw["dxy"] = round(s[-1][1], 2)
    raw["dxyPct"] = sc


def ind_hy():
    s = fred("BAMLH0A0HYM2")
    put("hygOas", score([v for _, v in s], WIN_D), s[-1][0], "FRED", "daily", len(s[-WIN_D:]))
    raw["hygOas"] = round(s[-1][1], 2)


def ind_curve():
    s = fred("T10Y2Y")
    speed = [(d, abs(v) * 100) for d, v in diff(s, 20)]  # 20-day speed of change, bps
    put("yieldCurve", score([v for _, v in speed], WIN_D), s[-1][0], "FRED", "daily", len(speed[-WIN_D:]))
    raw["curveBps"] = round(s[-1][1] * 100)


def ind_real_yield():
    s = fred("DFII10")
    put("realYield", score([v for _, v in s], WIN_D), s[-1][0], "FRED", "daily", len(s[-WIN_D:]))
    raw["realYield"] = round(s[-1][1], 2)


def ind_vix():
    s = fred("VIXCLS")
    put("vix", score([v for _, v in s], WIN_D), s[-1][0], "FRED", "daily", len(s[-WIN_D:]))
    raw["vix"] = round(s[-1][1], 2)


def ind_skew():
    s = [x for x in cboe_skew() if x[0] >= START]
    put("skewPcr", score([v for _, v in s], WIN_D), s[-1][0], "CBOE", "daily", len(s[-WIN_D:]))
    raw["skew"] = round(s[-1][1], 1)


def ind_breadth():
    rsp, spy = dict(stooq("rsp.us")), stooq("spy.us")
    ratio = [(d, rsp[d] / v) for d, v in spy if d in rsp and v]
    ch = pchg(ratio, 50)  # equal-weight vs cap-weight, 50-day % change
    put("breadth", score([v for _, v in ch], WIN_D, invert=True), ch[-1][0], "Stooq", "daily", len(ch[-WIN_D:]))
    raw["rspSpyChg"] = round(ch[-1][1], 2)


def btc_oi_daily():
    # Bybit first; Binance fallback (both may geo-block US runners -> handled by caller)
    try:
        js = json.loads(http("https://api.bybit.com/v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=1d&limit=200"))
        lst = js["result"]["list"]
        return sorted((dt.datetime.fromtimestamp(int(x["timestamp"]) / 1000, dt.timezone.utc).strftime("%Y-%m-%d"),
                       float(x["openInterest"])) for x in lst), "Bybit"
    except Exception as e:  # noqa: BLE001
        log_err("bybit OI daily", e)
    js = json.loads(http("https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=1d&limit=30"))
    return sorted((dt.datetime.fromtimestamp(int(x["timestamp"]) / 1000, dt.timezone.utc).strftime("%Y-%m-%d"),
                   float(x["sumOpenInterest"])) for x in js), "Binance"


def ind_cross_asset():
    s, src = btc_oi_daily()
    put("crossAsset", score([v for _, v in s], 90), s[-1][0], src, "daily", len(s[-90:]))


INDICATOR_JOBS = [
    ("netLiquidity", ind_net_liquidity, ["netLiqB", "netLiqLevelB"]),
    ("repoSofr", ind_repo, ["sofrBp"]),
    ("dxy", ind_dollar, ["dxy", "dxyPct"]),
    ("hygOas", ind_hy, ["hygOas"]),
    ("yieldCurve", ind_curve, ["curveBps"]),
    ("realYield", ind_real_yield, ["realYield"]),
    ("vix", ind_vix, ["vix"]),
    ("skewPcr", ind_skew, ["skew"]),
    ("breadth", ind_breadth, ["rspSpyChg"]),
    ("crossAsset", ind_cross_asset, []),
]

# ------------------------------------------------------------------ assets & sectors
FUT = {  # id: (stooq symbol, CFTC legacy code)
    "GC": ("gc.f", "088691"), "SI": ("si.f", "084691"), "CL": ("cl.f", "067651"),
    "HG": ("hg.f", "085692"), "ES": ("es.f", "13874A"), "NQ": ("nq.f", "209742"),
    "ZN": ("zn.f", "043602"),
}
SECTORS = ["XLK", "XLF", "XLV", "XLE", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC"]


def crypto_snapshot():
    """Price, 24h %, open interest for BTC/ETH. Bybit -> Hyperliquid (no US geo-block)."""
    out = {}
    try:
        js = json.loads(http("https://api.bybit.com/v5/market/tickers?category=linear"))
        rows = {x["symbol"]: x for x in js["result"]["list"]}
        for a, sym in (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")):
            r = rows[sym]
            out[a] = {"price": float(r["lastPrice"]), "chg": round(float(r["price24hPcnt"]) * 100, 2),
                      "oi": float(r["openInterest"]), "source": "Bybit"}
        return out
    except Exception as e:  # noqa: BLE001
        log_err("bybit tickers", e)
    body = json.dumps({"type": "metaAndAssetCtxs"}).encode()
    js = json.loads(http("https://api.hyperliquid.xyz/info", data=body, headers={"Content-Type": "application/json"}))
    names = [u["name"] for u in js[0]["universe"]]
    for a in ("BTC", "ETH"):
        c = js[1][names.index(a)]
        px, prev = float(c["markPx"]), float(c["prevDayPx"])
        out[a] = {"price": px, "chg": round((px / prev - 1) * 100, 2), "oi": float(c["openInterest"]),
                  "source": "Hyperliquid"}
    return out


def oi_change_24h(asset, oi_now, source, hist):
    """24h OI change from our own stored snapshots (same source only), window 18-30h back."""
    snaps = [s for s in hist.get(asset, []) if s[2] == source]
    target = NOW_MS - 24 * 3600 * 1000
    cand = [s for s in snaps if 18 * 3600 * 1000 <= NOW_MS - s[0] <= 30 * 3600 * 1000]
    if not cand:
        return None
    ref = min(cand, key=lambda s: abs(s[0] - target))
    return round((oi_now / ref[1] - 1) * 100, 2) if ref[1] else None


def build_assets():
    assets, hist = {}, PREV.get("oiHistory", {})
    try:
        snap = crypto_snapshot()
        for a, v in snap.items():
            assets[a] = {"price": v["price"], "chg": v["chg"],
                         "oiChg": oi_change_24h(a, v["oi"], v["source"], hist),
                         "date": NOW.strftime("%Y-%m-%d"), "ts": NOW_MS,
                         "source": v["source"], "oiSource": f"{v['source']} (24h)"}
            hist.setdefault(a, []).append([NOW_MS, v["oi"], v["source"]])
            hist[a] = [s for s in hist[a] if NOW_MS - s[0] <= 10 * 24 * 3600 * 1000]
    except Exception as e:  # noqa: BLE001
        log_err("crypto snapshot", e)
    for a, (sym, code) in FUT.items():
        try:
            px = stooq(sym)
            item = {"price": round(px[-1][1], 4), "chg": round((px[-1][1] / px[-2][1] - 1) * 100, 2),
                    "date": px[-1][0], "ts": date_ms(px[-1][0]), "source": "Stooq", "oiChg": None}
            try:
                c = cot(code)
                if len(c) >= 2 and c[1][1]:
                    item["oiChg"] = round((c[0][1] / c[1][1] - 1) * 100, 2)
                    item["oiSource"] = f"CFTC COT أسبوعي ({c[0][0]})"
            except Exception as e:  # noqa: BLE001
                log_err(f"cot {a}", e)
            assets[a] = item
        except Exception as e:  # noqa: BLE001
            log_err(f"stooq {a}", e)
            if a in PREV.get("assets", {}):
                assets[a] = dict(PREV["assets"][a], kept=True)
    return assets, hist


def build_sectors():
    out = {}
    for s in SECTORS:
        try:
            px = stooq(s.lower() + ".us")
            out[s] = {"perf": round((px[-1][1] / px[-2][1] - 1) * 100, 2), "date": px[-1][0]}
        except Exception as e:  # noqa: BLE001
            log_err(f"sector {s}", e)
            if s in PREV.get("sectors", {}):
                out[s] = dict(PREV["sectors"][s], kept=True)
    return out


# ------------------------------------------------------------------ main
def main():
    for ind_id, fn, raw_keys in INDICATOR_JOBS:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log_err(ind_id, e)
            keep_prev(ind_id, raw_keys)

    assets, oi_hist = build_assets()
    sectors = build_sectors()

    # Observation identity = the data dates of the systemic indicators.
    have_all_systemic = all(k in indicators for k in SYSTEMIC)
    obs_key = "|".join(f"{k}:{indicators[k]['date']}" for k in SYSTEMIC if k in indicators)
    recent = list(PREV.get("recent", []))
    if have_all_systemic and (not recent or recent[-1]["obsKey"] != obs_key):
        recent.append({
            "obsKey": obs_key,
            "ts": NOW_MS,
            "values": {k: v["score"] for k, v in indicators.items() if v.get("score") is not None},
            "prov": {k: v["ts"] for k, v in indicators.items()},
            "raw": dict(raw),
        })
        recent = recent[-10:]

    data = {
        "schema": 1,
        "generated": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generatedTs": NOW_MS,
        "obsKey": obs_key,
        "indicators": indicators,
        "raw": raw,
        "assets": assets,
        "sectors": sectors,
        "recent": recent,
        "oiHistory": oi_hist,
        "errors": errors,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    ok = [k for k in ALL_IDS if k in indicators and not indicators[k].get("kept")]
    print(f"indicators fresh: {len(ok)}/{len(ALL_IDS)} -> {ok}")
    print(f"assets: {sorted(assets)} | sectors: {len(sectors)} | recent obs: {len(recent)} | errors: {len(errors)}")
    for e in errors:
        print("  -", e)
    # Fail the run (red X in Actions) only if the core systemic set is entirely missing.
    if not any(k in indicators for k in SYSTEMIC):
        sys.exit(1)


if __name__ == "__main__":
    main()
