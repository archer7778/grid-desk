"""What an operator should hear about: tight supply, price spikes, units dropping out, and demand
far from its learned normal. Worked out from the stored data, so every viewer sees the same thing;
phone alerts (optional, through ntfy) are sent once per event by the fetch job."""
import datetime as _dt
import urllib.request

from . import __version__
from . import db as dbm
from . import mms
from .config import REGIONS

HOUR = 3600_000
NEM = mms.NEM_TZ


def _when(periods):
    """[[start, end], ...] -> 'Tue 20 Oct, 06:00–09:00 AEST' (market time)."""
    if not periods:
        return None
    f = lambda t: _dt.datetime.fromtimestamp(t / 1000, NEM)
    a, b = f(periods[0][0]), f(periods[0][1])
    if b - _dt.timedelta(minutes=1) < a.replace(hour=0, minute=0) + _dt.timedelta(days=1):
        txt = f"{a:%a} {a.day} {a:%b}, {a:%H:%M}–{'24:00' if b.hour == 0 and b.minute == 0 and b.date() > a.date() else f'{b:%H:%M}'}"
    else:
        txt = f"{a:%a} {a.day} {a:%b} {a:%H:%M} to {b:%a} {b.day} {b:%b} {b:%H:%M}"
    more = len(periods) - 1
    return txt + " AEST" + (f" and {more} more period{'s' if more > 1 else ''}" if more else "")


def lor_status(conn, now, hours=24, days_ahead=7):
    """Lack of Reserve conditions per region. AEMO issues LOR1 (reserve getting low), LOR2 (low) and
    LOR3 (load shedding possible or happening), as forecasts (often days ahead) and as actual
    conditions, updates them, and cancels them when they pass.

    A condition is keyed by region, level, forecast/actual and the day it's for, so cancelling
    Tuesday's forecast doesn't cancel Wednesday's. The newest notice for a key wins (AEMO's updates
    replace the earlier figures). A condition stops being shown once its stated period is over; one
    without a stated period is shown for `hours` after it was issued."""
    rows = conn.execute("SELECT id, t_ms, region, lor, kind, reference, reason FROM notices WHERE lor IS NOT NULL "
                        "AND t_ms>? AND t_ms<=? ORDER BY t_ms, id", (now - days_ahead * 24 * HOUR, now)).fetchall()
    state = {}
    for r in rows:
        if r["region"] not in REGIONS:
            continue
        d = mms.lor_details(r["reason"], r["reference"])
        basis = "actual" if r["kind"] == "actual" or "actual" in (r["reference"] or "").lower() else "forecast"
        key = (r["region"], r["lor"], basis, d["day"])
        if r["kind"] == "cancelled":
            for k in list(state):
                if k[:3] == key[:3] and (d["day"] is None or k[3] == d["day"]):
                    state[k] = None
            continue
        prev = state.get(key)
        v = dict(r, **d)
        v.pop("reason", None)
        v["updates"] = (prev or {}).get("updates", []) + ([prev["id"]] if prev else [])
        state[key] = v
    active = []
    for (reg, lvl, basis, day), v in state.items():
        if not v:
            continue
        if v["periods"]:
            if max(b for _, b in v["periods"]) <= now:
                continue
        elif day and basis == "forecast":
            end = int(_dt.datetime.fromisoformat(day).replace(tzinfo=NEM).timestamp() * 1000) + 24 * HOUR
            if end <= now:
                continue
        elif now - v["t_ms"] > hours * HOUR:
            continue
        start = min((a for a, _ in v["periods"]), default=None)
        active.append(dict(v, level=lvl, basis=basis, when=_when(v["periods"]), start=start,
                           in_progress=bool(start is not None and start <= now)))
    return sorted(active, key=lambda a: (a["basis"] != "actual", -a["level"], a["start"] or a["t_ms"]))


def trips(conn, cfg, now, hours=24):
    """Units whose output fell sharply within one 5-minute interval: likely trips, not planned
    shutdowns (those ramp down over several intervals)."""
    big = cfg["alerts"]["trip_mw"]
    rows = conn.execute("SELECT duid, t_ms, mw FROM units WHERE t_ms>? AND t_ms<=? ORDER BY duid, t_ms",
                        (now - hours * HOUR - 600_000, now)).fetchall()
    meta = {r["duid"]: dict(r) for r in conn.execute("SELECT * FROM duids")}
    out, prev = [], None
    for r in rows:
        if prev and prev["duid"] == r["duid"] and r["t_ms"] - prev["t_ms"] <= 300_000:
            m = meta.get(r["duid"], {})
            # batteries (and pumped hydro run as storage) stop or reverse in an instant by design: not trips
            if m.get("fuel") == "battery":
                prev = r
                continue
            if prev["mw"] >= big and r["mw"] <= 0.2 * prev["mw"] and prev["mw"] - r["mw"] >= big:
                out.append({"t": r["t_ms"], "duid": r["duid"], "station": m.get("station") or r["duid"],
                            "region": m.get("region"), "fuel": m.get("fuel"), "from_mw": prev["mw"], "to_mw": r["mw"]})
        prev = r
    return out


def price_spikes(conn, cfg, now, hours=24):
    lim = cfg["alerts"]["price_spike"]
    return [dict(r) for r in conn.execute(
        "SELECT region, t_ms, price FROM regional WHERE price>=? AND t_ms>? AND t_ms<=? ORDER BY t_ms",
        (lim, now - hours * HOUR, now))]


def current(conn, cfg, now=None, assessment=None):
    """Open alerts right now: [{key, sev, kind, region, t, msg}]."""
    now = now or dbm.now_ms()
    out = []
    for a in lor_status(conn, now):
        sev = "crit" if a["level"] >= 2 and a["basis"] == "actual" else "warn"
        short = REGIONS[a["region"]]["short"]
        if a["basis"] == "actual":
            head = f"Actual LOR{a['level']} in {short}"
        else:
            head = f"Forecast LOR{a['level']} in {short}" + (f" for {a['when']}" if a["when"] else
                                                               f" for {a['day']}" if a["day"] else "")
        tail = ""
        if a.get("available_mw") is not None and a.get("required_mw") is not None:
            tail += f" Reserve {'forecast ' if a['basis'] == 'forecast' else ''}as low as {a['available_mw']:,.0f} MW against {a['required_mw']:,.0f} MW needed."
        if a["basis"] == "forecast":
            tail += " This is a forecast, not a current shortage." if not a["in_progress"] else " The forecast period is under way."
        out.append({"key": f"lor:{a['region']}:{a['level']}:{a['basis']}:{a['day'] or a['id']}", "sev": sev, "kind": "reserve",
                    "region": a["region"], "t": a["t_ms"],
                    "msg": f"{head}: {LOR_TEXT[a['level']]}.{tail} (AEMO notice {a['id']}"
                           f"{', updating ' + ', '.join(map(str, a['updates'])) if a['updates'] else ''})"})
    latest = conn.execute("SELECT region, t_ms, price FROM regional WHERE price IS NOT NULL AND t_ms=("
                          "SELECT MAX(t_ms) FROM regional WHERE price IS NOT NULL AND t_ms<=?)", (now,)).fetchall()
    for r in latest:
        if r["price"] >= cfg["alerts"]["price_spike"]:
            out.append({"key": f"price:{r['region']}:{r['t_ms'] // (6 * HOUR)}", "sev": "warn", "kind": "price",
                        "region": r["region"], "t": r["t_ms"],
                        "msg": f"{REGIONS[r['region']]['short']} price ${r['price']:,.0f}/MWh (spike threshold "
                               f"${cfg['alerts']['price_spike']:,.0f})"})
    for tr in trips(conn, cfg, now, hours=2):
        out.append({"key": f"trip:{tr['duid']}:{tr['t']}", "sev": "warn", "kind": "trip", "region": tr["region"],
                    "t": tr["t"], "msg": f"{tr['station']} dropped from {tr['from_mw']:.0f} to {tr['to_mw']:.0f} MW "
                                        f"in five minutes (possible trip)"})
    for reg, a in (assessment or {}).items():
        if a.get("status") == "ok" and abs(a.get("z_sustained") or 0) >= cfg["alerts"]["departure_sigma"] and now - a["t"] < HOUR:
            out.append({"key": f"dem:{reg}:{a['t'] // (3 * HOUR)}", "sev": "warn", "kind": "demand", "region": reg,
                        "t": a["t"], "msg": f"{REGIONS[reg]['short']} demand {a['unexplained']:+,.0f} MW from its normal "
                                           f"({a['z']:+.1f}σ). {a.get('reason', '')}"})
    return sorted(out, key=lambda x: ({"crit": 0, "warn": 1}.get(x["sev"], 2), -x["t"]))


LOR_TEXT = {1: "reserve getting low", 2: "reserve low, a large unit failing could cause load shedding",
            3: "load shedding possible or under way"}


def send(cfg, title, body, priority="default", tags=""):
    n = cfg["notify"]
    topic = str(n.get("ntfy_topic") or "").strip()
    if not topic:
        return False
    req = urllib.request.Request(n["ntfy_server"].rstrip("/") + "/" + topic, data=body.encode(),
                                 headers={"Title": title.encode("latin-1", "replace").decode("latin-1"),
                                          "Priority": priority, "Tags": tags, "User-Agent": f"GridDesk/{__version__}"},
                                 method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception:
        return False


def after_fetch(conn, cfg, log=print):
    from .model import assess
    if not str(cfg["notify"].get("ntfy_topic") or "").strip():
        return
    seen = dbm.meta_get(conn, "notified", []) or []
    sent = 0
    for a in current(conn, cfg, assessment=assess(conn)):
        if a["key"] in seen:
            continue
        if send(cfg, f"Grid: {a['kind']}", a["msg"], "high" if a["sev"] == "crit" else "default", "zap"):
            sent += 1
        seen.append(a["key"])
    dbm.meta_set(conn, "notified", seen[-500:])
    maybe_summary(conn, cfg, log)
    if sent:
        log(f"Sent {sent} phone alert(s).")


def _tz(cfg):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(cfg["notify"]["timezone"])
    except Exception:
        return _dt.timezone(_dt.timedelta(hours=10))


def summary(conn, cfg, now=None):
    """The day in one message: peaks, prices, tight moments, units dropping out."""
    now = now or dbm.now_ms()
    since = now - 24 * HOUR
    lines = []
    for reg, info in REGIONS.items():
        r = conn.execute("SELECT MAX(demand) pk, AVG(price) ap, MAX(price) mp FROM regional WHERE region=? AND t_ms>? AND t_ms<=?",
                         (reg, since, now)).fetchone()
        if r["pk"] is None:
            continue
        lines.append(f"{info['short']}: peak {r['pk']:,.0f} MW, average ${r['ap'] or 0:,.0f}/MWh, highest ${r['mp'] or 0:,.0f}")
    lor = conn.execute("SELECT COUNT(*) FROM notices WHERE lor IS NOT NULL AND t_ms>? AND t_ms<=?", (since, now)).fetchone()[0]
    if lor:
        lines.append(f"{lor} Lack of Reserve notice{'s' if lor != 1 else ''} from AEMO")
    tr = trips(conn, cfg, now)
    if tr:
        lines.append(f"{len(tr)} unit{'s' if len(tr) != 1 else ''} dropped out suddenly: " +
                     ", ".join(sorted({t['station'] for t in tr}))[:200])
    title = "Grid Desk: the last 24 hours, " + _dt.datetime.fromtimestamp(now / 1000, _tz(cfg)).strftime("%a %d %b")
    return {"title": title, "text": "\n".join("• " + x for x in lines) or "No data in the last 24 hours."}


def maybe_summary(conn, cfg, log=print):
    h = int(cfg["notify"].get("summary_hour", 7))
    if h < 0:
        return False
    d = _dt.datetime.fromtimestamp(dbm.now_ms() / 1000, _tz(cfg))
    key = d.strftime("%Y-%m-%d")
    if d.hour < h or dbm.meta_get(conn, "summary_sent") == key:
        return False
    s = summary(conn, cfg)
    if send(cfg, s["title"], s["text"], "default", "sunrise"):
        dbm.meta_set(conn, "summary_sent", key)
        log("Sent the morning summary.")
        return True
    return False
