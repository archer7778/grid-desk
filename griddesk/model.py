"""The learned normal for each region's demand.

Electricity demand follows a daily rhythm, like a satellite's temperature follows its orbit, so
the same idea as Downlink Desk's health model applies with time of day in place of orbit phase:

    demand ≈ daily shape (weekday or weekend, a few harmonics)
           + cooling  (hours above 20 °C push demand up: air conditioning)
           + heating  (hours below 15 °C push it up too)
           + rooftop solar (sunny hours pull operational demand down: homes supply themselves)
           + a slow drift over the weeks

fitted to the last four weeks with weights that shrug off odd intervals. Then:
  expected  what the model says for right now, given the actual weather and sunshine
  typical   the same slot on an ordinary day (median weather and sunshine for that hour)
  explained how far weather and sunshine move today from typical
  unexplained what's left: an outage, a big load switching, a holiday, or a data glitch
"""
import bisect
import datetime as _dt
import math

from .config import REGIONS
from .linalg import mad, quantile, robust_fit

DAY = 86_400_000
NEM = _dt.timezone(_dt.timedelta(hours=10))
K = 6                  # harmonics of the daily shape
COOL_C, HEAT_C = 20.0, 15.0
MIN_ROWS = 288 * 5     # five days of intervals before judging


def _interp(series, t, max_gap, hold=0):
    """Linear interpolation in a sorted [(t, v)] list; None across gaps longer than max_gap.
    hold: past the last point, keep its value for this long (for feeds published with a lag)."""
    if not series:
        return None
    ts = [s[0] for s in series] if not hasattr(series, "_ts") else series._ts
    i = bisect.bisect_left(ts, t)
    if i < len(ts) and ts[i] == t:
        return series[i][1]
    if i >= len(ts) and hold and t - ts[-1] <= hold:
        return series[-1][1]
    if i == 0 or i >= len(ts):
        return None
    (t0, v0), (t1, v1) = series[i - 1], series[i]
    if t1 - t0 > max_gap:
        return None
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


class _S(list):
    """A sorted series with its time column cached for bisect."""
    def __init__(self, rows):
        super().__init__(sorted(rows))
        self._ts = [r[0] for r in self]


def _slot(t):
    d = _dt.datetime.fromtimestamp(t / 1000, NEM)
    return (d.hour + d.minute / 60) / 24, d.weekday() >= 5


def _feats(t, temp, roof, t0, has_roof):
    h, weekend = _slot(t)
    f = [1.0]
    for wk in (False, True):
        on = 1.0 if weekend == wk else 0.0
        if wk:
            f.append(on)
        for k in range(1, K + 1):
            a = 2 * math.pi * k * h
            f += [on * math.cos(a), on * math.sin(a)]
    cool = max(0.0, temp - COOL_C)
    heat = max(0.0, HEAT_C - temp)
    day = max(0.0, math.sin(math.pi * (h * 24 - 6) / 14)) if 6 <= h * 24 <= 20 else 0.0
    f += [cool, cool * cool / 10, heat, cool * day]
    if has_roof:
        f.append(roof)
    f.append((t - t0) / (7 * DAY))
    return f


def load(conn, region, since, until):
    dem = [(r[0], r[1]) for r in conn.execute(
        "SELECT t_ms, demand FROM regional WHERE region=? AND t_ms>? AND t_ms<=? AND demand IS NOT NULL ORDER BY t_ms",
        (region, since, until))]
    wx = _S([(r[0], r[1]) for r in conn.execute(
        "SELECT t_ms, temp FROM weather WHERE region=? AND t_ms>? AND t_ms<=?", (region, since - 3 * 3600_000, until + 3 * 3600_000))])
    roof = _S([(r[0], r[1]) for r in conn.execute(
        "SELECT t_ms, mw FROM rooftop WHERE region=? AND t_ms>? AND t_ms<=?", (region, since - 3600_000, until + 3600_000))])
    return dem, wx, roof


class RegionModel:
    def __init__(self, conn, region, now, days=28):
        self.region, self.now = region, now
        self.since = now - days * DAY
        dem, self.wx, self.roof = load(conn, region, self.since, now)
        # use rooftop solar only if it covers most of the window (it has its own archive and can
        # lag behind demand history); otherwise leave it out rather than drop most of the data
        self.has_roof = len(self.roof) >= 0.8 * days * 48
        self.t0 = self.since
        rows = []
        for t, d in dem:
            temp = _interp(self.wx, t, 3 * 3600_000)
            if temp is None:
                continue
            r = _interp(self.roof, t, 2 * 3600_000) if self.has_roof else 0.0
            if r is None:
                continue
            rows.append((t, d, temp, r))
        self.rows = rows
        self.coef = None
        if len(rows) < MIN_ROWS:
            return
        # fit on half-hour averages (six times less work, same shape), judge on every 5-minute value
        bins = {}
        for t, d, temp, r in rows:
            b = bins.setdefault(t // 1_800_000, [0, 0.0, 0.0, 0.0, 0.0])
            b[0] += 1
            b[1] += t
            b[2] += d
            b[3] += temp
            b[4] += r
        Xb, yb, wb = [], [], []
        for b in bins.values():
            n = b[0]
            Xb.append(_feats(b[1] / n, b[3] / n, b[4] / n, self.t0, self.has_roof))
            yb.append(b[2] / n)
            wb.append(float(n))
        self.coef = robust_fit(Xb, yb, w0=wb)
        if not self.coef:
            return
        res = [d - self.f(_feats(t, temp, r, self.t0, self.has_roof)) for t, d, temp, r in rows]
        self.sigma = mad(res) or 1.0
        self.res = res
        rs = sorted(res)
        self.q05, self.q95 = quantile(rs, 0.05), quantile(rs, 0.95)
        # typical weather and sunshine for each half-hour of the day, for the "ordinary day" line
        by = {}
        for t, _, temp, r in rows:
            by.setdefault(int(_slot(t)[0] * 48), []).append((temp, r))
        self.typ = {k: (sorted(x[0] for x in v)[len(v) // 2], sorted(x[1] for x in v)[len(v) // 2]) for k, v in by.items()}

    def f(self, x):
        return sum(a * b for a, b in zip(self.coef, x))

    def expected(self, t, temp=None, roof=None):
        temp = _interp(self.wx, t, 3 * 3600_000) if temp is None else temp
        if temp is None:
            return None
        if self.has_roof:
            roof = _interp(self.roof, t, 2 * 3600_000, hold=3600_000) if roof is None else roof
            if roof is None:
                roof = self.typ.get(int(_slot(t)[0] * 48), (None, 0.0))[1]
        return self.f(_feats(t, temp, roof or 0.0, self.t0, self.has_roof))

    def typical(self, t):
        tt, rr = self.typ.get(int(_slot(t)[0] * 48), (None, None))
        if tt is None:
            return None
        return self.expected(t, tt, rr)


def _pct(a, b):
    return round(100 * a / b, 1) if b else None


def assess(conn, now=None, days=28):
    """Every region: latest demand against expected and typical, with the reasons."""
    from .db import now_ms
    now = now or now_ms()
    out = {}
    for reg, info in REGIONS.items():
        last = conn.execute("SELECT t_ms, demand FROM regional WHERE region=? AND demand IS NOT NULL AND t_ms<=? "
                            "ORDER BY t_ms DESC LIMIT 1", (reg, now)).fetchone()
        m = RegionModel(conn, reg, now, days)
        res = {"region": reg, "name": info["name"], "samples": len(m.rows), "need": max(0, MIN_ROWS - len(m.rows))}
        if not last or not m.coef:
            res["status"] = "learning"
            out[reg] = res
            continue
        t, actual = last[0], last[1]
        exp, typ = m.expected(t), m.typical(t)
        temp = _interp(m.wx, t, 3 * 3600_000)
        if exp is None:
            res["status"] = "no weather"
            out[reg] = res
            continue
        z = (actual - exp) / m.sigma
        # one noisy interval isn't news: "sustained" is the smallest of the last three, if they agree in sign
        recent = conn.execute("SELECT t_ms, demand FROM regional WHERE region=? AND demand IS NOT NULL AND t_ms<=? "
                              "ORDER BY t_ms DESC LIMIT 3", (reg, now)).fetchall()
        zs = []
        for rt, rd in recent:
            e = m.expected(rt)
            if e is not None:
                zs.append((rd - e) / m.sigma)
        sustained = (min(zs, key=abs) if len(zs) == 3 and (all(x > 0 for x in zs) or all(x < 0 for x in zs)) else 0.0)
        res.update(status="ok", t=t, actual=actual, expected=round(exp, 1), typical=round(typ, 1) if typ else None,
                   band=[round(exp + m.q05, 1), round(exp + m.q95, 1)], sigma=round(m.sigma, 1), z=round(z, 2),
                   z_sustained=round(sustained, 2),
                   temp=temp, unexplained=round(actual - exp, 1), unexplained_pct=_pct(actual - exp, exp),
                   vs_typical=round(actual - typ, 1) if typ else None, vs_typical_pct=_pct(actual - typ, typ) if typ else None,
                   explained=round(exp - typ, 1) if typ else None, rooftop_in_model=m.has_roof,
                   daylight=6 <= _dt.datetime.fromtimestamp(t / 1000, NEM).hour < 18)
        res["reason"] = explain(res, info)
        out[reg] = res
    return out


def explain(r, info):
    """One plain sentence on why demand is where it is."""
    if r.get("vs_typical_pct") is None:
        return ""
    parts = []
    vt = r["vs_typical_pct"]
    ex, un, z = r.get("explained") or 0, r.get("unexplained") or 0, r.get("z") or 0
    if abs(vt) < 3:
        parts.append(f"Demand is close to a typical day ({vt:+.0f}%)")
    else:
        parts.append(f"Demand is {abs(vt):.0f}% {'above' if vt > 0 else 'below'} a typical day for this time")
        why = (f"{r['temp']:.0f} °C in {info['city']}" if r.get("temp") is not None else "the weather")
        if abs(z) < 2:
            # what weather doesn't explain is within ordinary day-to-day variation: don't over-read it
            parts.append(f"mostly weather and sunshine ({why})" if abs(ex) >= abs(un)
                         else "within ordinary day-to-day variation")
        elif abs(ex) >= abs(un):
            parts.append(f"mostly explained by weather and sunshine ({why})")
        elif not r.get("rooftop_in_model") and r.get("daylight"):
            # without rooftop solar in the model, a sunny day looks like missing demand
            parts.append("more than weather explains, probably rooftop solar, which isn't in the model yet "
                         "(its history is still filling in)")
        elif (r.get("samples") or 0) < 288 * 21:
            # a young model's errors are mostly its own: don't send readers looking for outages
            parts.append(f"more than weather and sunshine explain, but the model has only "
                         f"{(r.get('samples') or 0) / 288:.0f} days of history, so it may simply not know this "
                         f"time of week well yet")
        else:
            parts.append("more than weather and sunshine explain: a large load, an outage or a holiday")
    if abs(r.get("z_sustained") or 0) >= 3:
        parts.append(f"unusual for the model ({r['z']:+.1f}σ, for 15 minutes)")
    return "; ".join(parts) + "."


def series(conn, region, now, hours=48, days=28):
    """Actual, expected and typical demand every 5 minutes for the chart."""
    m = RegionModel(conn, region, now, days)
    rows = conn.execute("SELECT t_ms, demand FROM regional WHERE region=? AND t_ms>? AND t_ms<=? AND demand IS NOT NULL "
                        "ORDER BY t_ms", (region, now - hours * 3600_000, now)).fetchall()
    out = []
    for t, d in rows:
        e = m.expected(t) if m.coef else None
        ty = m.typical(t) if m.coef else None
        out.append([t, round(d, 1), round(e, 1) if e is not None else None, round(ty, 1) if ty is not None else None])
    band = [round(m.q05, 1), round(m.q95, 1)] if m.coef else None
    return {"region": region, "points": out, "band_offsets": band, "learning": not m.coef}
