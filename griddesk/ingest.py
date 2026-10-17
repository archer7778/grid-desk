"""Turn parsed AEMO tables into rows in the database."""
import csv
from pathlib import Path

from .config import REGIONS
from .mms import nem_time, num

FUELS = ["coal", "gas", "hydro", "wind", "solar", "battery", "rooftop solar", "other", "unmapped"]


def _rows(tables, *keys):
    for k in keys:
        for r in tables.get(k, []):
            yield r


def _main_run(r):
    # INTERVENTION=1 rows are AEMO's "what if we hadn't intervened" re-run; the real dispatch is 0
    return str(r.get("INTERVENTION", "0")).strip() in ("0", "")


def regional(conn, tables):
    n = 0
    for r in _rows(tables, "DISPATCH,REGIONSUM", "DREGION,"):
        reg, t = r.get("REGIONID"), nem_time(r.get("SETTLEMENTDATE"))
        if reg not in REGIONS or t is None or not _main_run(r):
            continue
        d = num(r.get("TOTALDEMAND"))
        if d is None or not 0 <= d < 30_000:
            continue
        conn.execute("INSERT INTO regional(region,t_ms,demand,avail_gen,net_interchange) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(region,t_ms) DO UPDATE SET demand=excluded.demand, avail_gen=excluded.avail_gen, "
                     "net_interchange=excluded.net_interchange",
                     (reg, t, d, num(r.get("AVAILABLEGENERATION")), num(r.get("NETINTERCHANGE"))))
        n += 1
    for r in _rows(tables, "DISPATCH,PRICE"):
        reg, t = r.get("REGIONID"), nem_time(r.get("SETTLEMENTDATE"))
        p = num(r.get("RRP"))
        if reg not in REGIONS or t is None or p is None or not _main_run(r) or not -2000 <= p <= 25_000:
            continue
        conn.execute("INSERT INTO regional(region,t_ms,price) VALUES(?,?,?) "
                     "ON CONFLICT(region,t_ms) DO UPDATE SET price=excluded.price", (reg, t, p))
    for r in _rows(tables, "DISPATCH,INTERCONNECTORRES"):
        t, ic = nem_time(r.get("SETTLEMENTDATE")), (r.get("INTERCONNECTORID") or "").strip()[:20]
        if t is None or not ic or not _main_run(r):
            continue
        conn.execute("INSERT OR REPLACE INTO interconnectors(ic,t_ms,flow,export_limit,import_limit) VALUES(?,?,?,?,?)",
                     (ic, t, num(r.get("MWFLOW")), num(r.get("EXPORTLIMIT")), num(r.get("IMPORTLIMIT"))))
    return n


def scada(conn, tables):
    """Unit output. Kept per unit for two days (for spotting trips), and summed by region and fuel
    for the long run."""
    known = {r["duid"]: (r["region"], r["fuel"]) for r in conn.execute("SELECT duid, region, fuel FROM duids")}
    sums, n = {}, 0
    for r in _rows(tables, "DISPATCH,UNIT_SCADA"):
        duid, t, mw = (r.get("DUID") or "").strip()[:20], nem_time(r.get("SETTLEMENTDATE")), num(r.get("SCADAVALUE"))
        if not duid or t is None or mw is None or abs(mw) > 5000:
            continue
        conn.execute("INSERT OR REPLACE INTO units(duid,t_ms,mw) VALUES(?,?,?)", (duid, t, mw))
        n += 1
        reg, fuel = known.get(duid, (None, None))
        if reg in REGIONS:
            k = (reg, t, fuel if fuel in FUELS else "other")
        else:
            k = ("?", t, "unmapped")
        sums[k] = sums.get(k, 0.0) + mw
    for (reg, t, fuel), mw in sums.items():
        if reg == "?":
            continue
        conn.execute("INSERT OR REPLACE INTO mix(region,t_ms,fuel,mw) VALUES(?,?,?,?)", (reg, t, fuel, round(mw, 2)))
    unmapped = sum(1 for k in sums if k[0] == "?")
    return n, unmapped


def rooftop(conn, tables):
    n = 0
    for r in _rows(tables, "ROOFTOP,ACTUAL"):
        reg, t, mw = r.get("REGIONID"), nem_time(r.get("INTERVAL_DATETIME")), num(r.get("POWER"))
        if reg not in REGIONS or t is None or mw is None or not 0 <= mw < 30_000:
            continue
        conn.execute("INSERT OR REPLACE INTO rooftop(region,t_ms,mw) VALUES(?,?,?)", (reg, t, mw))
        n += 1
    return n


FUEL_WORDS = [("battery", "battery"), ("coal", "coal"), ("natural gas", "gas"), ("coal seam", "gas"),
              ("gas", "gas"), ("diesel", "gas"), ("distillate", "gas"), ("kerosene", "gas"), ("hydro", "hydro"),
              ("water", "hydro"), ("wind", "wind"), ("solar", "solar"), ("photovoltaic", "solar"), ("storage", "battery")]


def fuel_of(text):
    t = (text or "").lower()
    for w, f in FUEL_WORDS:
        if w in t:
            return f
    return "other"


def load_duids(conn, path):
    """Unit metadata from data/duids.csv (duid,region,fuel,station,capacity_mw), built from AEMO's
    registration list by tools/build_duids.py. Without it, generation still counts per unit but
    can't be split by fuel."""
    p = Path(path)
    if not p.is_file():
        return 0
    n = 0
    with p.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            duid = (r.get("duid") or "").strip()[:20]
            if not duid:
                continue
            fuel = r.get("fuel") if r.get("fuel") in FUELS else fuel_of(r.get("fuel"))
            conn.execute("INSERT OR REPLACE INTO duids(duid,region,fuel,station,capacity_mw) VALUES(?,?,?,?,?)",
                         (duid, (r.get("region") or "").strip()[:6], fuel, (r.get("station") or "")[:80],
                          num(r.get("capacity_mw"))))
            n += 1
    conn.commit()
    return n
