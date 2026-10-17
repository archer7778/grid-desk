"""The data-centre layer: what's published, what's announced, and what we can estimate.

Every number this module hands out carries a `kind`:
  published  copied from a cited report (AEMO's forecasts)
  announced  an operator's stated capacity: a maximum, not usage
  estimated  our own inference from grid data, always with a range and a method
Public market data doesn't meter data centres separately, so nothing here is a live reading of
any one facility, and nothing links a facility to a particular grid event.
"""
import datetime as _dt
import math
import random
import tomllib
from pathlib import Path

from .config import REGIONS
from .linalg import robust_fit
from .model import NEM, K, _interp, _S, COOL_C, HEAT_C

STATUSES = ("operating", "construction", "approved", "proposed")
HOURS_PER_YEAR = 8766


class RegistryError(ValueError):
    pass


def _load(path):
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise RegistryError(f"{path}: {e}")


def registry(path):
    """Facilities with their sources resolved. Raises RegistryError on anything unsourced or odd,
    so a bad edit fails the tests instead of reaching the page."""
    d = _load(path)
    src = {s["id"]: s for s in d.get("source", [])}
    for s in src.values():
        if not str(s.get("url", "")).startswith("https://"):
            raise RegistryError(f"source {s.get('id')} needs an https link")
    out = []
    for f in d.get("facility", []):
        name = f.get("name") or "?"
        if not f.get("sources") or any(s not in src for s in f["sources"]):
            raise RegistryError(f"{name}: every facility needs at least one known source")
        if f.get("status") not in STATUSES:
            raise RegistryError(f"{name}: status must be one of {', '.join(STATUSES)}")
        if f.get("region") not in REGIONS:
            raise RegistryError(f"{name}: unknown region {f.get('region')}")
        mw = f.get("capacity_mw")
        if mw is not None and not (isinstance(mw, (int, float)) and 0 < mw < 10_000):
            raise RegistryError(f"{name}: capacity_mw should be a positive number of megawatts")
        approx = "lat" not in f
        lat = f.get("lat", REGIONS[f["region"]]["lat"])
        lon = f.get("lon", REGIONS[f["region"]]["lon"])
        out.append({"name": name, "operator": f.get("operator", "not stated"), "region": f["region"],
                    "place": f.get("place", "metro"), "lat": lat, "lon": lon, "position_approximate": approx,
                    "status": f["status"], "capacity_mw": mw, "kind": "announced",
                    "sources": [{"title": src[s]["title"], "url": src[s]["url"], "accessed": src[s].get("accessed")}
                                for s in f["sources"]]})
    return out


def forecasts(path):
    d = _load(path)
    src = {s["id"]: s for s in d.get("source", [])}
    figs = []
    for f in d.get("figure", []):
        if not f.get("sources") or any(s not in src for s in f["sources"]):
            raise RegistryError(f"figure {f.get('id')}: needs a known source")
        figs.append({**{k: f[k] for k in ("id", "label", "value", "unit", "basis") if k in f}, "kind": "published",
                     "sources": [{"title": src[s]["title"], "url": src[s]["url"]} for s in f["sources"]]})
    notes = [{"text": n["text"], "kind": "published",
              "sources": [{"title": src[s]["title"], "url": src[s]["url"]} for s in n.get("sources", []) if s in src]}
             for n in d.get("note", [])]
    return {"figures": figs, "notes": notes}


def twh_to_avg_mw(twh):
    """A year's energy as the average power it implies (1 TWh/year ≈ 114 MW around the clock)."""
    return twh * 1e6 / HOURS_PER_YEAR


def pressure(conn, facilities, figs, now):
    """Per region: announced capacity by status, and how it compares with that region's demand.
    Comparisons use announced capacity, which is an upper bound on use."""
    out = {}
    for reg, info in REGIONS.items():
        fs = [f for f in facilities if f["region"] == reg]
        by = {s: sum(f["capacity_mw"] or 0 for f in fs if f["status"] == s) for s in STATUSES}
        r = conn.execute("SELECT AVG(demand) avg, MAX(demand) pk FROM regional WHERE region=? AND t_ms>? AND t_ms<=?",
                         (reg, now - 28 * 86_400_000, now)).fetchone()
        avg, pk = r["avg"], r["pk"]
        out[reg] = {"region": reg, "name": info["name"], "facilities": len(fs), "kind": "announced",
                    "mw_by_status": by, "mw_pipeline": by["construction"] + by["approved"] + by["proposed"],
                    "avg_demand_mw": round(avg, 1) if avg else None, "peak_demand_mw": round(pk, 1) if pk else None,
                    "operating_vs_avg_demand": round(by["operating"] / avg, 3) if avg else None,
                    "pipeline_vs_peak_demand": round((by["construction"] + by["approved"] + by["proposed"]) / pk, 3) if pk else None}
    nem = {f["id"]: f for f in figs}
    if "dc-now" in nem:
        out["NEM"] = {"kind": "published", "avg_mw_now": round(twh_to_avg_mw(nem["dc-now"]["value"])),
                      "avg_mw_decade": round(twh_to_avg_mw(nem["dc-decade"]["value"])) if "dc-decade" in nem else None,
                      "basis": nem["dc-now"].get("basis")}
    return out


# ---------- the growth estimate ----------

def _month(t):
    d = _dt.datetime.fromtimestamp(t / 1000, NEM)
    return d.year * 12 + d.month - 1


def _label(m):
    return f"{m // 12}-{m % 12 + 1:02d}"


def _shape(t):
    d = _dt.datetime.fromtimestamp(t / 1000, NEM)
    h = (d.hour + d.minute / 60) / 24
    wk = d.weekday() >= 5
    f = [1.0 if wk else 0.0]
    for on in (0.0 if wk else 1.0, 1.0 if wk else 0.0):
        for k in range(1, K + 1):
            a = 2 * math.pi * k * h
            f += [on * math.cos(a), on * math.sin(a)]
    return f


def baseline(conn, region, now, months=24, min_months=3, boot=200, seed=1):
    """Monthly flat baseline of a region's demand, with weather, time of day, day type and rooftop
    solar removed, and the trend across months.

    A load that runs flat out around the clock (a data centre) raises every interval equally, so it
    shows up in the month terms, not the daily shape. So do other flat loads (industry, electrified
    heating and transport), so this is an upper bound for data-centre growth, never a measurement.
    The range comes from refitting with whole days resampled (bootstrap), so day-to-day weather and
    behaviour noise is carried into the uncertainty.
    """
    since = now - months * 31 * 86_400_000
    rows = conn.execute("SELECT t_ms, demand FROM regional WHERE region=? AND t_ms>? AND t_ms<=? AND demand IS NOT NULL",
                        (region, since, now)).fetchall()
    wx = _S([(r[0], r[1]) for r in conn.execute("SELECT t_ms, temp FROM weather WHERE region=? AND t_ms>?",
                                                (region, since - 3 * 3600_000))])
    roof = _S([(r[0], r[1]) for r in conn.execute("SELECT t_ms, mw FROM rooftop WHERE region=? AND t_ms>?",
                                                  (region, since - 3600_000))])
    span_days = (now - max(since, rows[0][0] if rows else now)) / 86_400_000
    has_roof = len(roof) >= 0.8 * span_days * 48   # only if rooftop history covers the demand history
    # hourly averages keep the fit small enough for a Pi
    bins = {}
    for t, d in rows:
        temp = _interp(wx, t, 3 * 3600_000)
        if temp is None:
            continue
        r = _interp(roof, t, 2 * 3600_000) if has_roof else 0.0
        if r is None:
            continue
        b = bins.setdefault(t // 3_600_000, [0, 0, 0.0, 0.0, 0.0])
        b[0] += 1
        b[1] += t
        b[2] += d
        b[3] += temp
        b[4] += r
    pts = [(b[1] // b[0], b[2] / b[0], b[3] / b[0], b[4] / b[0], b[0]) for b in bins.values()]
    ms = sorted({_month(p[0]) for p in pts})
    # a month needs most of its days to be a fair level
    count = {}
    for p in pts:
        count[_month(p[0])] = count.get(_month(p[0]), 0) + 1
    ms = [m for m in ms if count[m] >= 24 * 18]
    res = {"region": region, "kind": "estimated", "months": [_label(m) for m in ms], "method": METHOD}
    if len(ms) < min_months:
        res.update(status="learning", need_months=min_months - len(ms))
        return res
    idx = {m: i for i, m in enumerate(ms)}
    pts = [p for p in pts if _month(p[0]) in idx]

    def feats(p):
        t, _, temp, r, _ = p
        f = [0.0] * len(ms)
        f[idx[_month(t)]] = 1.0
        cool, heat = max(0.0, temp - COOL_C), max(0.0, HEAT_C - temp)
        return f + _shape(t) + [cool, cool * cool / 10, heat] + ([r] if has_roof else [])

    X = [[1.0] + feats(p)[1:] for p in pts]   # column 0: the first month's level; then offsets
    y = [p[1] for p in pts]
    c = robust_fit(X, y, ridge=1e-7, w0=[float(p[4]) for p in pts], iters=2)
    if not c:
        res["status"] = "unsolvable"
        return res
    nm = len(ms)
    # Each day's level: its demand minus everything the model explains except the month term.
    # Month levels are the average of their days; resampling whole days gives the range.
    day_lv = {}
    for p, x in zip(pts, X):
        fitted_wo_month = sum(a * b for a, b in zip(c[nm:], x[nm:]))
        e = day_lv.setdefault(p[0] // 86_400_000, [_month(p[0]), 0.0, 0])
        e[1] += (p[1] - fitted_wo_month) * p[4]
        e[2] += p[4]
    per_month = {}
    for m, s_, w in day_lv.values():
        per_month.setdefault(m, []).append(s_ / w)
    order = [per_month[m] for m in ms]
    levels = [sum(v) / len(v) for v in order]
    base = levels[0]
    levels = [v - base for v in levels]
    rnd = random.Random(seed)
    slopes = []
    for _ in range(boot):
        lv = [sum(v[rnd.randrange(len(v))] for _ in v) / len(v) for v in order]
        slopes.append(_slope(lv))
    slope = _slope(levels)
    slopes.sort()
    rng = [slopes[int(0.05 * (len(slopes) - 1))], slopes[int(0.95 * (len(slopes) - 1))]]
    res.update(status="ok", levels_mw=[round(x, 1) for x in levels], trend_mw_per_year=round(slope * 12, 1),
               trend_range_mw_per_year=[round(rng[0] * 12, 1), round(rng[1] * 12, 1)],
               bootstrap_runs=len(slopes), seasonal_warning=len(ms) < 13)
    return res


def _slope(levels):
    n = len(levels)
    mx = (n - 1) / 2
    my = sum(levels) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    return sum((i - mx) * (v - my) for i, v in enumerate(levels)) / sxx if sxx else 0.0


METHOD = ("Monthly level of each region's demand after removing the daily shape (weekday and weekend), "
          "temperature (heating and cooling) and rooftop solar, fitted to hourly averages with weights that "
          "resist outliers. A flat around-the-clock load such as a data centre raises the level; so do other "
          "flat loads, so the trend is an upper bound on data-centre growth, not a measurement. The range is "
          "a 90% bootstrap interval from resampling whole days. Under 13 months, seasons other than temperature "
          "(holidays, daylight) can leak into the trend.")
