"""Grid Desk web server: the console page and a read-only JSON API. Standard library only.

There are no write endpoints: the console only shows data, so there is nothing for another
website to trigger through a visitor's browser, and no login to steal.
"""
import argparse
import contextlib
import gzip
import hashlib
import json
import math
import mimetypes
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, alerts, datacentres, model
from . import db as dbm
from .config import REGIONS, find_config

WEB = Path(__file__).resolve().parent.parent / "web"
DATA = Path(__file__).resolve().parent.parent / "data"
STATIC = {}
HOUR = 3600_000


def finite(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [finite(v) for v in o]
    return o


class App:
    def __init__(self, cfg, data_dir=DATA):
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        dbm.open_db(cfg).close()   # make sure the schema exists
        self._pool = queue.LifoQueue()
        self._cache = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._sig = None
        self._watcher = None

    @contextlib.contextmanager
    def ro(self):
        try:
            c = self._pool.get_nowait()
        except queue.Empty:
            c = dbm.connect_ro(self.cfg.db_path)
        try:
            yield c
        finally:
            if self._pool.qsize() < 6:
                self._pool.put(c)
            else:
                c.close()

    def sig(self):
        with self.ro() as c:
            return (c.execute("SELECT MAX(t_ms) FROM regional").fetchone()[0],
                    c.execute("SELECT MAX(id) FROM notices").fetchone()[0],
                    c.execute("SELECT MAX(id) FROM fetches").fetchone()[0])

    def cached(self, key, ttl, fn, sig=None):
        hit = self._cache.get(key)
        now = time.time()
        if hit and now - hit[0] < ttl and hit[1] == sig:
            return hit[2]
        val = fn()
        with self._lock:
            self._cache[key] = (now, sig, val)
        return val

    def latest_t(self, c):
        return c.execute("SELECT MAX(t_ms) FROM regional WHERE demand IS NOT NULL").fetchone()[0]

    # ---------- the live picture ----------
    def state(self):
        def build():
            with self.ro() as c:
                t = self.latest_t(c)
                now = t or dbm.now_ms()
                assess = model.assess(c, now)
                regions = {}
                for reg, info in REGIONS.items():
                    r = c.execute("SELECT * FROM regional WHERE region=? AND demand IS NOT NULL ORDER BY t_ms DESC LIMIT 1",
                                  (reg,)).fetchone()
                    roof = c.execute("SELECT t_ms, mw FROM rooftop WHERE region=? ORDER BY t_ms DESC LIMIT 1", (reg,)).fetchone()
                    mt = c.execute("SELECT MAX(t_ms) FROM mix WHERE region=?", (reg,)).fetchone()[0]
                    mix = {m["fuel"]: m["mw"] for m in c.execute("SELECT fuel, mw FROM mix WHERE region=? AND t_ms=?", (reg, mt))} if mt else {}
                    wx = c.execute("SELECT t_ms, temp FROM weather WHERE region=? AND t_ms<=? ORDER BY t_ms DESC LIMIT 1",
                                   (reg, dbm.now_ms())).fetchone()
                    regions[reg] = {"region": reg, **info, "t": r["t_ms"] if r else None,
                                    "demand": r["demand"] if r else None, "price": r["price"] if r else None,
                                    "avail_gen": r["avail_gen"] if r else None, "net_interchange": r["net_interchange"] if r else None,
                                    "rooftop": roof["mw"] if roof else None, "rooftop_t": roof["t_ms"] if roof else None,
                                    "mix": mix, "mix_t": mt, "temp": wx["temp"] if wx else None,
                                    "normal": assess.get(reg)}
                it = c.execute("SELECT MAX(t_ms) FROM interconnectors").fetchone()[0]
                ics = [dict(x) for x in c.execute("SELECT ic, flow, export_limit, import_limit FROM interconnectors WHERE t_ms=?",
                                                  (it,))] if it else []
                fetches = [dict(x) for x in c.execute("SELECT t_ms, ok, message FROM fetches ORDER BY id DESC LIMIT 5")]
                return {"now": dbm.now_ms(), "t": t, "regions": regions, "interconnectors": ics, "interconnectors_t": it,
                        "alerts": alerts.current(c, self.cfg, now, assess), "reserve": alerts.lor_status(c, now),
                        "fetches": fetches, "version": __version__, "price_spike": self.cfg["alerts"]["price_spike"]}
        return self.cached("state", 60, build, self.sig())

    def series(self, region, hours):
        def build():
            with self.ro() as c:
                now = self.latest_t(c) or dbm.now_ms()
                s = model.series(c, region, now, hours)
                since = now - hours * HOUR
                s["price"] = [[r[0], r[1]] for r in c.execute(
                    "SELECT t_ms, price FROM regional WHERE region=? AND t_ms>? AND price IS NOT NULL ORDER BY t_ms", (region, since))]
                mix = {}
                for r in c.execute("SELECT t_ms, fuel, mw FROM mix WHERE region=? AND t_ms>? ORDER BY t_ms", (region, since)):
                    mix.setdefault(r["fuel"], []).append([r["t_ms"], r["mw"]])
                s["mix"] = mix
                s["rooftop"] = [[r[0], r[1]] for r in c.execute(
                    "SELECT t_ms, mw FROM rooftop WHERE region=? AND t_ms>? ORDER BY t_ms", (region, since))]
                s["temp"] = [[r[0], r[1]] for r in c.execute(
                    "SELECT t_ms, temp FROM weather WHERE region=? AND t_ms>? AND t_ms<=? ORDER BY t_ms", (region, since, now))]
                return s
        return self.cached(("series", region, hours), 120, build, self.sig())

    def events(self, hours):
        def build():
            with self.ro() as c:
                now = self.latest_t(c) or dbm.now_ms()
                notices = [dict(r) for r in c.execute(
                    "SELECT id, t_ms, type, region, lor, kind, reference FROM notices WHERE t_ms>? ORDER BY t_ms DESC LIMIT 200",
                    (now - hours * HOUR,))]
                return {"notices": notices, "price_spikes": alerts.price_spikes(c, self.cfg, now, hours),
                        "trips": alerts.trips(c, self.cfg, now, min(hours, 48))}
        return self.cached(("events", hours), 120, build, self.sig())

    def datacentres(self):
        def build():
            fac = datacentres.registry(self.data_dir / "datacentres.toml")
            fc = datacentres.forecasts(self.data_dir / "forecasts.toml")
            with self.ro() as c:
                now = self.latest_t(c) or dbm.now_ms()
                pressure = datacentres.pressure(c, fac, fc["figures"], now)
                base = {r: datacentres.baseline(c, r, now) for r in ("NSW1", "VIC1")}
            return {"facilities": fac, "forecasts": fc, "pressure": pressure, "baseline": base,
                    "kinds": KINDS, "note": NOTE}
        return self.cached("dc", 6 * 3600, build, None)

    def summary(self):
        with self.ro() as c:
            return alerts.summary(c, self.cfg, self.latest_t(c))

    def config(self):
        return {"version": __version__, "regions": REGIONS, "price_spike": self.cfg["alerts"]["price_spike"],
                "departure_sigma": self.cfg["alerts"]["departure_sigma"]}

    # ---------- live updates ----------
    def wait_change(self, sig, timeout):
        with self._cond:
            self._cond.wait_for(lambda: self._sig != sig, timeout)
            return self._sig

    def start_watch(self):
        if self._watcher:
            return

        def watch():
            while True:
                try:
                    s = self.sig()
                    if s != self._sig:
                        with self._cond:
                            self._sig = s
                            self._cond.notify_all()
                except Exception:
                    pass
                time.sleep(5)
        self._watcher = threading.Thread(target=watch, daemon=True)
        self._watcher.start()


KINDS = {"published": "Copied from a cited report (AEMO's forecasts).",
         "announced": "An operator's stated capacity: a maximum, not what the site uses.",
         "estimated": "Our own inference from grid data, with a range and a method."}
NOTE = ("Public market data doesn't meter data centres separately, so nothing here is a live reading of any "
        "one facility, and nothing links a facility to a particular grid event.")


def _int(q, name, default, lo, hi):
    try:
        v = float(q.get(name, default))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number")
    return int(min(max(v, lo), hi))


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "GridDesk/" + __version__
        timeout = 60

        def log_message(self, *a):
            pass

        def _json(self, obj, status=200):
            body = json.dumps(finite(obj), separators=(",", ":"), allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if len(body) > 1400 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
                body = gzip.compress(body, 4)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path
            try:
                if p == "/api/config":
                    return self._json(app.config())
                if p == "/api/state":
                    return self._json(app.state())
                if p == "/api/series":
                    reg = q.get("region", "NSW1")
                    if reg not in REGIONS:
                        raise ValueError("unknown region")
                    return self._json(app.series(reg, _int(q, "hours", 48, 1, 24 * 7)))
                if p == "/api/events":
                    return self._json(app.events(_int(q, "hours", 24, 1, 24 * 7)))
                if p == "/api/datacentres":
                    return self._json(app.datacentres())
                if p == "/api/summary":
                    return self._json(app.summary())
                if p == "/api/stream":
                    return self._stream()
                if p.startswith("/api/"):
                    return self._json({"error": "not found"}, 404)
                return self._static(p)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                import traceback
                traceback.print_exc()
                return self._json({"error": "internal error (details in the server log)"}, 500)

        def do_POST(self):
            return self._json({"error": "this console is read-only"}, 405)

        def _stream(self):
            app.start_watch()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            sig = app._sig
            end = time.time() + 1800
            try:
                self.wfile.write(b"event: hello\ndata: {}\n\n")
                self.wfile.flush()
                while time.time() < end:
                    new = app.wait_change(sig, 20)
                    if new != sig:
                        sig = new
                        self.wfile.write(f"event: data\ndata: {json.dumps({'t': new[0]})}\n\n".encode())
                    else:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def _static(self, p):
            if p == "/":
                p = "/index.html"
            f = (WEB / p.lstrip("/")).resolve()
            if WEB not in f.parents or not f.is_file():
                return self._json({"error": "not found"}, 404)
            st = f.stat()
            key = (st.st_mtime_ns, st.st_size)
            hit = STATIC.get(str(f))
            if not hit or hit[0] != key:
                raw = f.read_bytes()
                hit = (key, raw, gzip.compress(raw, 9), '"' + hashlib.sha1(raw).hexdigest()[:16] + '"')
                STATIC[str(f)] = hit
            _, raw, gz, etag = hit
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            body = gz if "gzip" in (self.headers.get("Accept-Encoding") or "") else raw
            self.send_response(200)
            self.send_header("Content-Type", (mimetypes.guess_type(str(f))[0] or "application/octet-stream")
                             + ("; charset=utf-8" if f.suffix in (".html", ".js", ".css", ".json") else ""))
            if body is gz:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                             "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description="Grid Desk console server")
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    cfg = find_config(a.config)
    app = App(cfg)
    s = cfg["server"]
    httpd = ThreadingHTTPServer((s["host"], int(s["port"])), make_handler(app))
    print(f"Grid Desk {__version__} on http://{s['host']}:{s['port']}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
