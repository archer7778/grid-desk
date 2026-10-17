"""What an operator should hear about: tight supply, price spikes, units dropping out, and demand
far from its learned normal. Worked out from the stored data, so every viewer sees the same thing;
phone alerts (optional, through ntfy) are sent once per event by the fetch job."""
import datetime as _dt
import urllib.request

from . import __version__
from . import db as dbm
from .config import REGIONS

HOUR = 3600_000


def lor_status(conn, now, hours=24):
    """Lack of Reserve conditions per region. AEMO issues LOR1 (reserve getting low), LOR2 (low) and
    LOR3 (load shedding possible or happening), as forecasts and as actual conditions, and cancels
    them when they pass. The newest notice for a region and level wins."""
    rows = conn.execute("SELECT id, t_ms, region, lor, kind, reference FROM notices WHERE lor IS NOT NULL AND t_ms>? "
                        "AND t_ms<=? ORDER BY t_ms, id", (now - hours * HOUR, now)).fetchall()
    state = {}
    for r in rows:
        if r["region"] not in REGIONS:
            continue
        key = (r["region"], r["lor"], "actual" if r["kind"] == "actual" or "actual" in (r["reference"] or "").lower() else "forecast")
        state[key] = None if r["kind"] == "cancelled" else dict(r)
    active = [dict(v, level=k[1], basis=k[2]) for k, v in state.items() if v]
    return sorted(active, key=lambda a: (-a["level"], a["basis"] != "actual"))


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
            if prev["mw"] >= big and r["mw"] <= 0.2 * prev["mw"] and prev["mw"] - r["mw"] >= big:
                m = meta.get(r["duid"], {})
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
        out.append({"key": f"lor:{a['region']}:{a['level']}:{a['basis']}:{a['id']}", "sev": sev, "kind": "reserve",
                    "region": a["region"], "t": a["t_ms"],
                    "msg": f"{'Actual' if a['basis'] == 'actual' else 'Forecast'} LOR{a['level']} in "
                           f"{REGIONS[a['region']]['short']}: {LOR_TEXT[a['level']]} (AEMO notice {a['id']})"})
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
