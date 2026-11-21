#!/usr/bin/env python3
"""
Ops Room — Phase 3 historical backtest & threshold calibration (stdlib only).

Rebuilds the dashboard's daily scores, gates and confirmed regime from ~2004 to today,
using the SAME definitions as scripts/fetch_data.py and the SAME engine as index.html,
then measures what the regimes would have meant for SPY and gold.

Anti look-ahead rules:
  * Each series becomes "known" only after a conservative publication lag (LAGS below).
  * Percentile scores use only past data (trailing window).
  * The regime confirmed at the close of day t is applied to returns from t+1 to t+2.

Calibration protocol (same spirit as Ahmed's EA protocol):
  * In-sample  : 2018-04 .. 2022-12  (first period with the full systemic set incl. SOFR)
  * Out-sample : 2023-01 .. today
  * Small grid only; the chosen setting must also hold out-of-sample.
"""
import bisect
import csv
import datetime as dt
import io
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(ROOT, "backtest")
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
UA = {"User-Agent": "Mozilla/5.0 (OpsRoom backtest; GitHub Actions)"}
START = "2000-01-01"
WIN_D, WIN_W = 756, 156

IS_START, IS_END = "2018-04-03", "2022-12-31"
OOS_START = "2023-01-01"

# calendar days after the observation date before a value is considered public
LAGS = {"WALCL": 1, "WTREGEN": 1, "RRPONTSYD": 0, "SOFR": 1, "IORB": 0, "IOER": 0, "DTWEXBGS": 7,
        "BAMLH0A0HYM2": 1, "DXY": 0, "T10Y2Y": 0, "DFII10": 0, "VIXCLS": 0, "SKEW": 0, "BREADTH": 0}

WEIGHTS = {"netLiquidity": .14, "repoSofr": .12, "dxy": .10, "hygOas": .16, "yieldCurve": .09, "realYield": .09,
           "vix": .10, "breadth": .08, "skewPcr": .06}  # crossAsset (BTC OI) has no long history -> renormalised
SYSTEMIC = {"netLiquidity", "repoSofr", "dxy", "hygOas", "yieldCurve", "realYield"}

DEFAULT = {"es": 50, "sy": 54, "cr": 70, "hy": 5.5, "sofr": 15, "nl": -200, "vix": 30, "dxp": 85}
CASH = {"expansion": .125, "valuationDip": .25, "earlyStress": .425, "crisis": .625}  # midpoints of the dashboard ranges
SEV = {"expansion": 0, "valuationDip": 1, "earlyStress": 2, "crisis": 3}

EVENTS = [("2008-09-15", "ليمان براذرز"), ("2011-08-05", "خفض تصنيف أمريكا"), ("2015-08-24", "تخفيض اليوان"),
          ("2018-12-24", "قاع الربع الرابع 2018"), ("2020-02-24", "بداية كوفيد"), ("2022-06-13", "صدمة التضخم 2022"),
          ("2023-03-10", "انهيار SVB"), ("2024-08-05", "فك تجارة الين"), ("2025-04-04", "صدمة الرسوم الجمركية")]

errors = []


def http(url, retries=2, timeout=40):
    last = None
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def fred(sid):
    q = urllib.parse.urlencode({"series_id": sid, "api_key": FRED_KEY, "file_type": "json", "observation_start": START})
    js = json.loads(http("https://api.stlouisfed.org/fred/series/observations?" + q))
    return [(o["date"], float(o["value"])) for o in js["observations"] if o["value"] not in (".", "")]


def yahoo(sym):
    last = None
    for host in ("query1", "query2"):
        try:
            # NOTE: range=max silently downgrades to MONTHLY bars -> always pass explicit period1/period2 for daily data
            p2 = int(time.time())
            url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
                   f"?period1=946684800&period2={p2}&interval=1d&events=div%2Csplit")
            r = json.loads(http(url, retries=1))["chart"]["result"][0]
            off = int(r["meta"].get("gmtoffset", 0))
            closes = r["indicators"].get("adjclose", [{}])[0].get("adjclose") or r["indicators"]["quote"][0]["close"]
            return [(dt.datetime.fromtimestamp(t + off, dt.timezone.utc).strftime("%Y-%m-%d"), float(c))
                    for t, c in zip(r["timestamp"], closes) if c is not None]
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def cboe_skew():
    rows = list(csv.reader(io.StringIO(http("https://cdn.cboe.com/api/global/us_indices/daily_prices/SKEW_History.csv"))))
    out = []
    for r in rows[1:]:
        try:
            out.append((dt.datetime.strptime(r[0].strip(), "%m/%d/%Y").strftime("%Y-%m-%d"), float(r[-1])))
        except (ValueError, IndexError):
            pass
    return sorted(out)


# ------------------------------------------------------------------ helpers
def add_days(d, n):
    return (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat()


def asof(series, d):
    best = None
    for sd, v in series:
        if sd <= d:
            best = v
        else:
            break
    return best


def rolling_pct(series, win, invert=False):
    """Percentile of each point vs its own trailing window (inclusive). Past data only."""
    out, window, sorted_w = [], [], []
    for d, v in series:
        window.append(v)
        bisect.insort(sorted_w, v)
        if len(window) > win:
            old = window.pop(0)
            del sorted_w[bisect.bisect_left(sorted_w, old)]
        lo, hi = bisect.bisect_left(sorted_w, v), bisect.bisect_right(sorted_w, v)
        p = 100.0 * (lo + 0.5 * (hi - lo)) / len(sorted_w)
        if len(sorted_w) >= min(win, 120):  # need some history before trusting a percentile
            out.append((d, 100 - p if invert else p))
    return out


def lagged(series, lag):
    return [(add_days(d, lag), v) for d, v in series]


class AsOf:
    """Fast forward-only as-of lookup over a sorted (date, value) list."""
    def __init__(self, series):
        self.s, self.i, self.v = series, 0, None

    def at(self, d):
        while self.i < len(self.s) and self.s[self.i][0] <= d:
            self.v = self.s[self.i][1]
            self.i += 1
        return self.v


# ------------------------------------------------------------------ build daily panel
def assert_daily(name, series, min_rows):
    """Guard against silent frequency downgrades (the bug that hit run #1)."""
    if len(series) < min_rows:
        raise RuntimeError(f"{name}: only {len(series)} rows — expected daily data (>= {min_rows})")


def build_panel():
    F = {sid: fred(sid) for sid in ["WALCL", "WTREGEN", "RRPONTSYD", "SOFR", "IORB", "IOER", "DTWEXBGS",
                                    "BAMLH0A0HYM2", "T10Y2Y", "DFII10", "VIXCLS", "DTB3"]}
    spy, gold, rsp, dxy = yahoo("SPY"), yahoo("GC=F"), yahoo("RSP"), yahoo("DX-Y.NYB")
    skew = cboe_skew()
    assert_daily("SPY", spy, 5000); assert_daily("GC=F", gold, 4000); assert_daily("RSP", rsp, 4000); assert_daily("DXY", dxy, 5000)
    assert_daily("SKEW", skew, 5000); assert_daily("VIXCLS", F["VIXCLS"], 5000)

    # --- indicator series (native frequency), then lag to availability date
    net = []
    for d, w in F["WALCL"]:
        t, r = asof(F["WTREGEN"], d), asof(F["RRPONTSYD"], d)
        if t is not None and r is not None:
            net.append((d, w / 1000 - t / 1000 - r))
    net_ch = [(net[i][0], net[i][1] - net[i - 4][1]) for i in range(4, len(net))]

    ioer = dict(F["IOER"]); iorb = dict(F["IORB"])
    spread = []
    for d, v in F["SOFR"]:
        ref = iorb.get(d, ioer.get(d))
        if ref is not None:
            spread.append((d, (v - ref) * 100))

    dx = dxy; dx_ch = [(dx[i][0], (dx[i][1] / dx[i - 63][1] - 1) * 100) for i in range(63, len(dx))]
    cv = F["T10Y2Y"]; cv_sp = [(cv[i][0], abs(cv[i][1] - cv[i - 20][1]) * 100) for i in range(20, len(cv))]
    rspd = dict(rsp); ratio = [(d, rspd[d] / v) for d, v in spy if d in rspd and v]
    br = [(ratio[i][0], (ratio[i][1] / ratio[i - 50][1] - 1) * 100) for i in range(50, len(ratio))]

    scores = {
        "netLiquidity": lagged(rolling_pct(net_ch, WIN_W, invert=True), LAGS["WALCL"]),
        "repoSofr": lagged(rolling_pct(spread, WIN_D), LAGS["SOFR"]),
        "dxy": lagged(rolling_pct(dx_ch, WIN_D), LAGS["DXY"]),
        "hygOas": lagged(rolling_pct(F["BAMLH0A0HYM2"], WIN_D), LAGS["BAMLH0A0HYM2"]),
        "yieldCurve": lagged(rolling_pct(cv_sp, WIN_D), LAGS["T10Y2Y"]),
        "realYield": lagged(rolling_pct(F["DFII10"], WIN_D), LAGS["DFII10"]),
        "vix": lagged(rolling_pct(F["VIXCLS"], WIN_D), LAGS["VIXCLS"]),
        "breadth": lagged(rolling_pct(br, WIN_D, invert=True), LAGS["BREADTH"]),
        "skewPcr": lagged(rolling_pct(skew, WIN_D), LAGS["SKEW"]),
    }
    raws = {
        "hy": lagged(F["BAMLH0A0HYM2"], LAGS["BAMLH0A0HYM2"]),
        "sofr": lagged(spread, LAGS["SOFR"]),
        "nl": lagged(net_ch, LAGS["WALCL"]),
        "vix": lagged(F["VIXCLS"], LAGS["VIXCLS"]),
    }
    S = {k: AsOf(v) for k, v in scores.items()}
    R = {k: AsOf(v) for k, v in raws.items()}
    G, T = AsOf(gold), AsOf(F["DTB3"])

    panel = []
    for d, px in spy:
        if d < "2004-01-01":
            continue
        row = {"d": d, "spy": px, "gold": G.at(d), "tb3": T.at(d)}
        for k in S:
            row[k] = S[k].at(d)
        for k in R:
            row["r_" + k] = R[k].at(d)
        panel.append(row)
    return panel


# ------------------------------------------------------------------ engine (port of index.html)
def metrics(row):
    ws = wsum = ss = sw = vs = vw = 0.0
    for k, w in WEIGHTS.items():
        v = row.get(k)
        if v is None:
            continue
        ws += v * w; wsum += w
        if k in SYSTEMIC:
            ss += v * w; sw += w
        else:
            vs += v * w; vw += w
    if wsum == 0:
        return None
    return {"comp": ws / wsum, "sys": ss / sw if sw else None, "val": vs / vw if vw else None}


def gates(row, P):
    g = 0
    if row["r_hy"] is not None and row["r_hy"] >= P["hy"]: g += 1
    if row["r_sofr"] is not None and row["r_sofr"] >= P["sofr"]: g += 1
    if row["r_nl"] is not None and row["r_nl"] <= P["nl"]: g += 1
    if row["r_vix"] is not None and row["dxy"] is not None and row["r_vix"] >= P["vix"] and row["dxy"] >= P["dxp"]: g += 1
    return g


def raw_regime(m, g, cur, P, h=3):
    comp, sys_ = m["comp"], m["sys"] if m["sys"] is not None else 0
    crisis_score = comp >= (P["cr"] - h if cur == "crisis" else P["cr"] + h)
    if (crisis_score or comp >= P["cr"] + 8) and (g >= 2 or sys_ >= 72):
        return "crisis"
    es_thr = P["es"] - h if cur == "earlyStress" else P["es"] + h
    if comp >= es_thr or sys_ >= P["sy"] or g >= 1:
        return "earlyStress"
    dip_thr = 22 if cur == "valuationDip" else 28
    return "valuationDip" if comp >= dip_thr else "expansion"


def run_engine(panel, P, start="0000"):
    confirmed, cand, n = None, None, 0
    out = []
    for row in panel:
        if row["d"] < start:
            continue
        m = metrics(row)
        if m is None:
            out.append(None); continue
        g = gates(row, P)
        r = raw_regime(m, g, confirmed, P)
        if r == confirmed:
            cand, n = None, 0
        else:
            n = n + 1 if r == cand else 1
            cand = r
            if n >= 3:
                confirmed, cand, n = r, None, 0
        out.append({"d": row["d"], "comp": round(m["comp"], 1), "sys": round(m["sys"] or 0, 1), "g": g,
                    "raw": r, "reg": confirmed})
    return out


# ------------------------------------------------------------------ evaluation
def perf(rets):
    if len(rets) < 20:
        return None
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1 + r; peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
    yrs = len(rets) / 252
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) or 1e-9
    cagr = eq ** (1 / yrs) - 1
    return {"cagr": round(cagr * 100, 2), "vol": round(sd * math.sqrt(252) * 100, 2),
            "sharpe": round(mu / sd * math.sqrt(252), 2), "maxdd": round(mdd * 100, 2),
            "calmar": round(cagr / abs(mdd), 2) if mdd else None}


def portfolio(panel, regs, start, end):
    """Regime confirmed at close t sets cash weight for the return t+1 -> t+2 (one full day of delay)."""
    idx = {r["d"]: i for i, r in enumerate(panel)}
    strat, bh, spy_r, cash_r, wts = [], [], [], [], []
    days = [x for x in regs if x and start <= x["d"] <= end]
    for x in days:
        i = idx[x["d"]]
        if i + 2 >= len(panel) or x["reg"] is None:
            continue
        a, b = panel[i + 1], panel[i + 2]
        r_spy = b["spy"] / a["spy"] - 1
        r_cash = (a["tb3"] or 0) / 100 / 252
        w_cash = CASH[x["reg"]]
        strat.append((1 - w_cash) * r_spy + w_cash * r_cash)
        bh.append(r_spy); spy_r.append(r_spy); cash_r.append(r_cash); wts.append(w_cash)
    # Honest benchmark: FIXED cash weight equal to the strategy's average cash weight.
    # If the strategy can't beat this, its gain comes from holding less equity, not from timing.
    avg_w = sum(wts) / len(wts) if wts else 0
    static = [(1 - avg_w) * r + avg_w * c for r, c in zip(spy_r, cash_r)]
    st = perf(static)
    if st:
        st["avg_cash_pct"] = round(avg_w * 100, 1)
    portfolio.last_static = st
    return perf(strat), perf(bh)


def fwd(panel, i, key, n):
    if i + n >= len(panel) or panel[i][key] is None or panel[i + n][key] is None:
        return None
    return panel[i + n][key] / panel[i][key] - 1


def fwd_mdd(panel, i, n):
    p0, lo = panel[i]["spy"], panel[i]["spy"]
    for j in range(i + 1, min(len(panel), i + n + 1)):
        lo = min(lo, panel[j]["spy"])
    return lo / p0 - 1


def regime_stats(panel, regs, start, end):
    idx = {r["d"]: i for i, r in enumerate(panel)}
    acc = {}
    for x in regs:
        if not x or x["reg"] is None or not (start <= x["d"] <= end):
            continue
        i = idx[x["d"]]
        a = acc.setdefault(x["reg"], {"n": 0, "spy20": [], "spy60": [], "gold20": [], "gold60": [], "mdd60": []})
        a["n"] += 1
        for key, nd, lst in (("spy", 20, "spy20"), ("spy", 60, "spy60"), ("gold", 20, "gold20"), ("gold", 60, "gold60")):
            v = fwd(panel, i, key, nd)
            if v is not None:
                a[lst].append(v)
        a["mdd60"].append(fwd_mdd(panel, i, 60))
    total = sum(a["n"] for a in acc.values()) or 1
    avg = lambda L: round(sum(L) / len(L) * 100, 2) if L else None
    return {k: {"days": a["n"], "share": round(a["n"] / total * 100, 1), "spy20": avg(a["spy20"]),
                "spy60": avg(a["spy60"]), "gold20": avg(a["gold20"]), "gold60": avg(a["gold60"]),
                "mdd60": avg(a["mdd60"])} for k, a in sorted(acc.items(), key=lambda kv: SEV[kv[0]])}


def alarms(panel, regs, start, end, dd=-0.07):
    """Every confirmed entry into earlyStress/crisis; false if SPY never fell >= 7% within 60 days."""
    idx = {r["d"]: i for i, r in enumerate(panel)}
    prev, out = None, []
    for x in regs:
        if not x or not (start <= x["d"] <= end):
            if x: prev = x["reg"]
            continue
        if x["reg"] in ("earlyStress", "crisis") and (prev is None or SEV.get(prev, 0) < 2):
            m = fwd_mdd(panel, idx[x["d"]], 60)
            out.append({"d": x["d"], "reg": x["reg"], "mdd60": round(m * 100, 1), "true": m <= dd})
        prev = x["reg"]
    yrs = max(1e-9, (dt.date.fromisoformat(min(end, panel[-1]["d"])) - dt.date.fromisoformat(max(start, panel[0]["d"]))).days / 365.25)
    false_n = sum(1 for a in out if not a["true"])
    return {"entries": len(out), "true": len(out) - false_n, "false": false_n,
            "false_per_year": round(false_n / yrs, 2), "list": out}


def episodes(panel, regs):
    by_d = {x["d"]: x for x in regs if x}
    dates = [x["d"] for x in regs if x]
    out = []
    for ed, name in EVENTS:
        if ed > dates[-1] or ed < dates[0]:
            continue
        lo, hi = add_days(ed, -90), add_days(ed, 30)
        first = next((d for d in dates if lo <= d <= hi and by_d[d]["reg"] in ("earlyStress", "crisis")), None)
        if first:  # walk back to the start of that stress spell (it may have begun before the window)
            j = dates.index(first)
            while j > 0 and by_d[dates[j - 1]]["reg"] in ("earlyStress", "crisis"):
                j -= 1
            first = dates[j]
        at = next((by_d[d] for d in dates if d >= ed), None)
        lead = (dt.date.fromisoformat(ed) - dt.date.fromisoformat(first)).days if first else None
        out.append({"event": name, "date": ed, "regime_at_event": at["reg"] if at else None,
                    "composite_at_event": at["comp"] if at else None, "gates_at_event": at["g"] if at else None,
                    "first_warning": first, "lead_days": lead})
    return out


def grid():
    for es in (45, 50, 55):
      for sy in (54, 60, 66):
        for hy in (5.0, 5.5, 6.0):
            for sofr in (10, 15, 20):
                for nl in (-150, -200, -300):
                    for vix in (25, 30, 35):
                        yield {"es": es, "sy": sy, "cr": 70, "hy": hy, "sofr": sofr, "nl": nl, "vix": vix, "dxp": 85}


def objective(panel, P):
    regs = run_engine(panel, P)  # full history, same as live (no artificial warm-up)
    s, b = portfolio(panel, regs, IS_START, IS_END)
    al = alarms(panel, regs, IS_START, IS_END)
    if not s:
        return None
    return {"calmar": s["calmar"] or 0, "sharpe": s["sharpe"], "maxdd": s["maxdd"], "false_py": al["false_per_year"],
            "score": (s["calmar"] or 0) - 0.15 * al["false_per_year"]}


# ------------------------------------------------------------------ main
def main():
    if not FRED_KEY:
        sys.exit("FRED_API_KEY missing")
    os.makedirs(OUTDIR, exist_ok=True)
    panel = build_panel()
    print("panel days:", len(panel), panel[0]["d"], "->", panel[-1]["d"])

    # 1) grid on in-sample only
    results = []
    for P in grid():
        o = objective(panel, P)
        if o:
            results.append((o["score"], P, o))
    results.sort(key=lambda t: -t[0])
    best = results[0][1]
    default_o = objective(panel, DEFAULT)

    # robustness: how many of the 10 best share the chosen value per parameter
    top = [p for _, p, _ in results[:10]]
    robust = {k: sum(1 for p in top if p[k] == best[k]) for k in ("es", "sy", "hy", "sofr", "nl", "vix")}

    report = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "panel": {"start": panel[0]["d"], "end": panel[-1]["d"], "days": len(panel)},
              "lags_days": LAGS, "in_sample": [IS_START, IS_END], "out_sample": [OOS_START, panel[-1]["d"]],
              "grid_size": len(results), "default": DEFAULT, "best": best,
              "best_is": results[0][2], "default_is": default_o,
              "top10": [{"params": p, **o} for _, p, o in results[:10]], "robustness_top10": robust,
              "errors": errors}

    for label, P in (("default", DEFAULT), ("best", best)):
        regs_full = run_engine(panel, P)
        s_is, b_is = portfolio(panel, regs_full, IS_START, IS_END); st_is = portfolio.last_static
        s_oos, b_oos = portfolio(panel, regs_full, OOS_START, "9999"); st_oos = portfolio.last_static
        s_all, b_all = portfolio(panel, regs_full, "2004-01-01", "9999"); st_all = portfolio.last_static
        report[label + "_eval"] = {
            "portfolio": {"is": {"strategy": s_is, "buy_hold": b_is, "static_same_cash": st_is},
                          "oos": {"strategy": s_oos, "buy_hold": b_oos, "static_same_cash": st_oos},
                          "full": {"strategy": s_all, "buy_hold": b_all, "static_same_cash": st_all}},
            "regimes_is": regime_stats(panel, regs_full, IS_START, IS_END),
            "regimes_oos": regime_stats(panel, regs_full, OOS_START, "9999"),
            "regimes_full": regime_stats(panel, regs_full, "2004-01-01", "9999"),
            "alarms_is": alarms(panel, regs_full, IS_START, IS_END),
            "alarms_oos": alarms(panel, regs_full, OOS_START, "9999"),
            "episodes": episodes(panel, regs_full),
            "current": regs_full[-1],
        }
        if label == "best":
            with open(os.path.join(OUTDIR, "regimes.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["date", "composite", "systemic", "gates", "raw", "confirmed"])
                for x in regs_full:
                    if x:
                        w.writerow([x["d"], x["comp"], x["sys"], x["g"], x["raw"], x["reg"]])

    with open(os.path.join(OUTDIR, "results.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps({"best": best, "best_is": results[0][2], "default_is": default_o,
                      "oos_best": report["best_eval"]["portfolio"]["oos"],
                      "oos_default": report["default_eval"]["portfolio"]["oos"]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
