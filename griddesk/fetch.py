"""Fetch job: run every 5 minutes (systemd timer, or GitHub Actions for the public demo).

    python -m griddesk.fetch              # new 5-minute files, weather, notices, alerts
    python -m griddesk.fetch --history 28 # fill in older days from AEMO's daily archive now
"""
import argparse
import fcntl
import sys
import time

from . import alerts, ingest
from . import db as dbm
from .config import REGIONS, find_config
from .mms import parse_notice, read_any
from .nemweb import Client, FetchError, weather

DAY = 86_400_000
WANT_DISPATCH = {"DISPATCH,REGIONSUM", "DISPATCH,PRICE", "DISPATCH,INTERCONNECTORRES"}


def _new_files(conn, client, feed, cap, first_take):
    """Files in a feed not processed yet, oldest first, at most `cap`. On the very first run only
    the newest `first_take`, so a new install doesn't download two days of files at once."""
    listing = client.listing(feed)
    done = {r[0] for r in conn.execute("SELECT name FROM files")}
    last = dbm.meta_get(conn, f"last:{feed}")
    todo = [x for x in listing if x[1] not in done and (last is None or x[0] >= last)]
    if last is None:
        todo = todo[-first_take:]
    return todo[:cap]


def _mark(conn, feed, stamp, name):
    conn.execute("INSERT OR IGNORE INTO files(name,fetched_ms) VALUES(?,?)", (name, dbm.now_ms()))
    dbm.meta_set(conn, f"last:{feed}", stamp)


def fetch_feed(conn, client, feed, cap, log, first_take=3):
    got = 0
    for stamp, name, href in _new_files(conn, client, feed, cap, first_take):
        data = client.get(href)
        try:
            if feed == "notices":
                n = parse_notice(data.decode("utf-8", "replace"))
                if n:
                    conn.execute("INSERT OR REPLACE INTO notices(id,t_ms,type,region,lor,kind,reference,reason) "
                                 "VALUES(:id,:t_ms,:type,:region,:lor,:kind,:reference,:reason)", n)
            else:
                tables = read_any(data, WANT_DISPATCH if feed == "dispatch" else None)
                if feed == "dispatch":
                    ingest.regional(conn, tables)
                elif feed == "scada":
                    ingest.scada(conn, tables)
                elif feed == "rooftop":
                    ingest.rooftop(conn, tables)
        except Exception as e:   # one bad file is skipped (and logged), never retried forever or fatal
            conn.rollback()
            log(f"Skipped {name}: {type(e).__name__}: {e}")
        _mark(conn, feed, stamp, name)
        conn.commit()
        got += 1
    return got


def fetch_weather(conn, cfg, log, force=False):
    last = dbm.meta_get(conn, "weather_ms", 0) or 0
    if not force and dbm.now_ms() - last < 3600_000:
        return 0
    have = conn.execute("SELECT MIN(t_ms) FROM weather").fetchone()[0]
    days = 2 if have and have < dbm.now_ms() - (cfg["sources"]["history_days"] - 1) * DAY else cfg["sources"]["history_days"] + 1
    n = 0
    for reg, info in REGIONS.items():
        try:
            rows = weather(cfg["sources"]["weather"], info["lat"], info["lon"], days)
        except FetchError as e:
            log(f"Weather for {info['city']}: {e}")
            continue
        conn.executemany("INSERT OR REPLACE INTO weather(region,t_ms,temp) VALUES(?,?,?)", [(reg, t, v) for t, v in rows])
        n += len(rows)
        time.sleep(0.5)
    conn.commit()
    dbm.meta_set(conn, "weather_ms", dbm.now_ms())
    return n


def fill_history(conn, cfg, client, log, days=None, per_run=1):
    """Older days of demand and price from AEMO's daily archive (one zip per day, about 20-40 MB).
    One day per run by default, newest first, so the model has a few weeks to learn from."""
    days = cfg["sources"]["history_days"] if days is None else days
    if days <= 0:
        return 0
    try:
        listing = client.listing("dispatch_archive")
    except FetchError as e:
        log(f"History: {e}")
        return 0
    cutoff = time.strftime("%Y%m%d", time.gmtime((dbm.now_ms() - days * DAY) / 1000))
    done = {r[0] for r in conn.execute("SELECT name FROM files WHERE name LIKE 'PUBLIC_DISPATCHIS_%' AND length(name)<40")}
    todo = [x for x in reversed(listing) if x[0] >= cutoff and x[1] not in done][:per_run]
    for stamp, name, href in todo:
        tables = read_any(client.get(href), WANT_DISPATCH)
        n = ingest.regional(conn, tables)
        conn.execute("INSERT OR IGNORE INTO files(name,fetched_ms) VALUES(?,?)", (name, dbm.now_ms()))
        conn.commit()
        log(f"History: {stamp} added ({n} regional intervals).")
    return len(todo)


def refresh_duids(conn, cfg, log):
    """The unit list (which fuel each generator burns) from AEMO's registration spreadsheet,
    when it's missing or a month old. Tried at most once a day; generation shows as
    'unclassified' until it works."""
    path = cfg.resolve("data/duids.csv")
    fresh = path.is_file() and time.time() - path.stat().st_mtime < 30 * 86400
    if fresh or dbm.now_ms() - (dbm.meta_get(conn, "duids_tried_ms", 0) or 0) < DAY:
        return
    dbm.meta_set(conn, "duids_tried_ms", dbm.now_ms())
    try:
        from . import duids
        units = duids.build(duids.download())
        if len(units) < 50:
            raise ValueError(f"only {len(units)} units found")
        path.parent.mkdir(parents=True, exist_ok=True)
        duids.write(units, path)
        log(f"Unit list updated: {len(units)} units.")
    except Exception as e:
        log(f"Unit list unavailable ({type(e).__name__}: {e}); generation shows as unclassified for now.")


def prune(conn, cfg):
    keep = dbm.now_ms() - cfg["storage"]["unit_detail_hours"] * 3600_000
    conn.execute("DELETE FROM units WHERE t_ms<?", (keep,))
    conn.execute("DELETE FROM files WHERE fetched_ms<? AND name NOT LIKE 'PUBLIC_DISPATCHIS_%'", (dbm.now_ms() - 7 * DAY,))
    conn.commit()


def run(cfg, log=print, history=True):
    conn = dbm.open_db(cfg)
    lock = open(cfg.db_path.parent / "fetch.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Another fetch is running; skipping.")
        return 0
    try:
        refresh_duids(conn, cfg, log)
        ingest.load_duids(conn, cfg.resolve("data/duids.csv"))
        client = Client(cfg["sources"]["nemweb"], cfg["sources"]["min_gap_s"])
        cap = cfg["sources"]["max_files_per_run"]
        summary, failed = [], []
        for feed, first in (("dispatch", 12), ("scada", 3), ("rooftop", 4), ("notices", 40)):
            try:
                n = fetch_feed(conn, client, feed, cap if feed != "notices" else 60, log, first)
                summary.append(f"{feed} {n}")
            except FetchError as e:
                failed.append(f"{feed}: {e}")
        try:
            w = fetch_weather(conn, cfg, log)
            if w:
                summary.append(f"weather {w}")
        except Exception as e:   # weather is an extra; never lose a fetch over it
            log(f"Weather skipped: {e}")
        if history:
            try:
                fill_history(conn, cfg, client, log)
            except (FetchError, ValueError) as e:
                log(f"History paused: {e}")
        prune(conn, cfg)
        try:
            alerts.after_fetch(conn, cfg, log)
        except Exception as e:
            log(f"Alerts skipped: {type(e).__name__}: {e}")
        ok = not failed or len(failed) < 4
        msg = ("New files: " + ", ".join(summary)) + (("; failed " + "; ".join(failed)) if failed else "")
        dbm.record_fetch(conn, ok, msg)
        log(msg + f" ({client.requests} requests).")
        return 0 if ok else 1
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch the latest NEM data from AEMO's NEMweb")
    ap.add_argument("--config")
    ap.add_argument("--history", type=float, metavar="DAYS", help="fill in this many past days now")
    ap.add_argument("--no-history", action="store_true", help="skip the one-day-per-run history fill")
    a = ap.parse_args(argv)
    cfg = find_config(a.config)
    if a.history:
        conn = dbm.open_db(cfg)
        client = Client(cfg["sources"]["nemweb"], cfg["sources"]["min_gap_s"])
        fetch_weather(conn, cfg, print, force=True)
        n = fill_history(conn, cfg, client, print, days=a.history, per_run=int(a.history) + 1)
        print(f"History: {n} days added.")
        sys.exit(0)
    sys.exit(run(cfg, history=not a.no_history))


if __name__ == "__main__":
    main()
