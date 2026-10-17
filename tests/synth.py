"""A synthetic National Electricity Market for tests: demand with a daily rhythm, weather, rooftop
solar and an optional flat load growing over time (a stand-in for data centres), written out as
files in AEMO's MMS CSV layout, plus a mock NEMweb server and a mock weather API.

The real layouts are checked against the live server by the data-spike workflow; these fixtures
follow the same column names so the parsers are exercised the same way.
"""
import datetime as dt
import io
import json
import math
import random
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

NEM = dt.timezone(dt.timedelta(hours=10))
REG = ["NSW1", "QLD1", "VIC1", "SA1", "TAS1"]
SCALE = {"NSW1": 1.0, "QLD1": 0.85, "VIC1": 0.65, "SA1": 0.2, "TAS1": 0.15}
STEP = 300_000


def nemstr(t):
    return dt.datetime.fromtimestamp(t / 1000, NEM).strftime("%Y/%m/%d %H:%M:%S")


def stamp(t, n=12):
    return dt.datetime.fromtimestamp(t / 1000, NEM).strftime("%Y%m%d%H%M%S")[:n]


class Grid:
    """Deterministic synthetic truth. flat_mw(t) is extra flat load (for the baseline test)."""
    def __init__(self, seed=1, flat_growth_mw_per_month=0.0, start_ms=None):
        self.rnd = random.Random(seed)
        self.g = flat_growth_mw_per_month
        self.start = start_ms or 0
        self._wx = {}

    def temp(self, reg, t):
        """Hourly temperature: season + daily cycle + slow weather systems."""
        h = t // 3_600_000
        key = (reg, h)
        if key not in self._wx:
            d = dt.datetime.fromtimestamp(h * 3600, NEM)
            doy = d.timetuple().tm_yday
            season = 18 - 7 * math.cos(2 * math.pi * (doy - 15) / 365.25)   # southern hemisphere: warm in Jan
            daily = 5 * math.sin(2 * math.pi * (d.hour - 9) / 24)
            systems = 6 * math.sin(h / 61.0 + REG.index(reg)) * math.sin(h / 23.0)
            self._wx[key] = round(season + daily + systems, 1)
        return self._wx[key]

    def rooftop(self, reg, t):
        d = dt.datetime.fromtimestamp(t / 1000, NEM)
        h = d.hour + d.minute / 60
        sun = max(0.0, math.sin(math.pi * (h - 6) / 13))
        cloud = 0.6 + 0.4 * abs(math.sin(t / 86_400_000 * 1.7 + REG.index(reg)))
        return round(3000 * SCALE[reg] * sun * cloud, 1)

    def flat(self, t):
        return self.g * max(0.0, (t - self.start) / (30.44 * 86_400_000))

    def demand(self, reg, t, noise=True):
        d = dt.datetime.fromtimestamp(t / 1000, NEM)
        h = d.hour + d.minute / 60
        shape = 1500 * math.exp(-((h - 8) / 2) ** 2) + 2500 * math.exp(-((h - 18.5) / 2.5) ** 2) - 900 * math.cos(2 * math.pi * h / 24)
        wk = -600 if d.weekday() >= 5 else 0
        T = self.temp(reg, (t // 3_600_000) * 3_600_000)
        weather = 150 * max(0.0, T - 20) + 4 * max(0.0, T - 20) ** 2 + 90 * max(0.0, 15 - T)
        base = 7500 + shape + wk + weather
        v = SCALE[reg] * base - 0.9 * self.rooftop(reg, t) + self.flat(t) * (1 if reg == "NSW1" else 0)
        if noise:
            v += self.rnd.gauss(0, 60 * SCALE[reg])
        return round(v, 2)

    def price(self, reg, t):
        return round(60 + 0.01 * self.demand(reg, t, False) + self.rnd.gauss(0, 5), 2)


def mms(tables):
    """tables: [(group, table, version, cols, rows)] -> MMS CSV text."""
    out = io.StringIO()
    out.write('C,NEMP.WORLD,DISPATCHIS,AEMO,PUBLIC,2026/10/17,14:00:00,0000000001,DISPATCHIS,0000000001\n')
    for g, tb, v, cols, rows in tables:
        out.write(f"I,{g},{tb},{v}," + ",".join(cols) + "\n")
        for r in rows:
            out.write(f"D,{g},{tb},{v}," + ",".join(f'"{x}"' if isinstance(x, str) and "/" in x else str(x) for x in r) + "\n")
    out.write("C,\"END OF REPORT\",99\n")
    return out.getvalue()


def zipped(name, text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, text)
    return b.getvalue()


def dispatch_file(grid, t):
    reg_rows = [[nemstr(t), 1, r, 1, 0, grid.demand(r, t), round(grid.demand(r, t, False) * 1.3), 120.0, 0, "X"] for r in REG]
    price_rows = [[nemstr(t), 1, r, 1, 0, grid.price(r, t)] for r in REG]
    ic_rows = [[nemstr(t), 1, "NSW1-QLD1", 1, 0, -350.5, 800, -1100], [nemstr(t), 1, "V-SA", 1, 0, 120.0, 500, -600]]
    # an intervention re-run row that must be ignored
    reg_rows.append([nemstr(t), 1, "NSW1", 1, 1, 99999, 0, 0, 0, "X"])
    return mms([
        ("DISPATCH", "REGIONSUM", 8, ["SETTLEMENTDATE", "RUNNO", "REGIONID", "DISPATCHINTERVAL", "INTERVENTION",
                                     "TOTALDEMAND", "AVAILABLEGENERATION", "NETINTERCHANGE", "UIGF", "EXTRA_COL"], reg_rows),
        ("DISPATCH", "PRICE", 5, ["SETTLEMENTDATE", "RUNNO", "REGIONID", "DISPATCHINTERVAL", "INTERVENTION", "RRP"], price_rows),
        ("DISPATCH", "INTERCONNECTORRES", 3, ["SETTLEMENTDATE", "RUNNO", "INTERCONNECTORID", "DISPATCHINTERVAL",
                                             "INTERVENTION", "MWFLOW", "EXPORTLIMIT", "IMPORTLIMIT"], ic_rows),
    ])


UNITS = [("BAYSW1", "NSW1", "coal", 660), ("ERARING1", "NSW1", "coal", 720), ("TALWA1", "NSW1", "gas", 400),
         ("BLOWERNG", "TAS1", "hydro", 300), ("HORNSDALE", "SA1", "wind", 300), ("VBBG1", "VIC1", "battery", 300),
         ("LOYYB1", "VIC1", "coal", 560), ("DARLSF1", "NSW1", "solar", 230)]


def scada_file(t, trip_at=None):
    rows = []
    for duid, reg, fuel, cap in UNITS:
        mw = cap * 0.8
        if fuel == "solar":
            h = dt.datetime.fromtimestamp(t / 1000, NEM).hour
            mw = cap * max(0, math.sin(math.pi * (h - 6) / 13))
        if fuel == "battery":
            mw = -50.0
        if trip_at and duid == "ERARING1" and t >= trip_at:
            mw = 0.0
        rows.append([nemstr(t), duid, round(mw, 2), "2026/10/17 14:00:05"])
    return mms([("DISPATCH", "UNIT_SCADA", 1, ["SETTLEMENTDATE", "DUID", "SCADAVALUE", "LASTCHANGED"], rows)])


def rooftop_file(grid, t):
    rows = [[nemstr(t), r, grid.rooftop(r, t), 1, "MEASUREMENT"] for r in REG] + [[nemstr(t), "QLDC", 10, 1, "MEASUREMENT"]]
    return mms([("ROOFTOP", "ACTUAL", 2, ["INTERVAL_DATETIME", "REGIONID", "POWER", "QI", "TYPE"], rows)])


def notice_text(nid, t, lor=None, region="NSW", kind="Actual", cancel=False):
    ref = (f"{'Cancellation of ' if cancel else ''}{kind} Lack Of Reserve Level {lor} (LOR{lor}) in the {region} Region"
           if lor else "Inter-regional transfer limit variation")
    return (f"-------------------------------------------------------------------\n"
            f"                       MARKET NOTICE\n"
            f"-------------------------------------------------------------------\n\n"
            f"From :              AEMO\nTo   :              NEMITWEB1\nCreation Date :     {nemstr(t)}\n\n"
            f"Notice ID :               {nid}\nNotice Type ID :          {'RESERVE NOTICE' if lor else 'INTER-REGIONAL TRANSFER'}\n"
            f"Notice Type Description : MT PASA/STPASA/Reserve Notice\nIssue Date :              {nemstr(t)}\n"
            f"External Reference :      {ref}\n\n"
            f"-------------------------------------------------------------------\n\nReason :\n\n"
            f"AEMO ELECTRICITY MARKET NOTICE\n\n{ref}.\nThe capacity reserve requirement is 650 MW.\n\n"
            f"Manager NEM Real Time Operations\n"
            f"-------------------------------------------------------------------\nEND OF REPORT\n"
            f"-------------------------------------------------------------------\n")


class MockNEMweb:
    """Serves directory listings and files like nemweb.com.au, plus Open-Meteo-style weather."""
    def __init__(self, grid, now, minutes=60, archive_days=0, notices=None, trip_at=None):
        self.files = {}
        self.dirs = {}
        self.requests = []
        t0 = now - minutes * 60_000
        t = (t0 // STEP + 1) * STEP
        while t <= now:
            self._add("/Reports/Current/DispatchIS_Reports/", f"PUBLIC_DISPATCHIS_{stamp(t)}_0000000{t // STEP % 10**9:09d}.zip",
                      zipped(f"PUBLIC_DISPATCHIS_{stamp(t)}.CSV", dispatch_file(grid, t)))
            self._add("/Reports/Current/Dispatch_SCADA/", f"PUBLIC_DISPATCHSCADA_{stamp(t)}_0000000{t // STEP % 10**9:09d}.zip",
                      zipped(f"PUBLIC_DISPATCHSCADA_{stamp(t)}.CSV", scada_file(t, trip_at)))
            if t % 1_800_000 == 0:
                self._add("/Reports/Current/ROOFTOP_PV/ACTUAL/",
                          f"PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_{stamp(t, 14)}_0000000{t // STEP % 10**9:09d}.zip",
                          zipped("ROOFTOP.CSV", rooftop_file(grid, t)))
            t += STEP
        for i, n in enumerate(notices or []):
            self._add("/Reports/Current/Market_Notice/", f"NEMITWEB1_MKTNOTICE_{stamp(n['t'], 8)}.R{n['id']}",
                      notice_text(n["id"], n["t"], n.get("lor"), n.get("region", "NSW"), n.get("kind", "Actual"),
                                  n.get("cancel", False)).encode())
        day = (now // 86_400_000) * 86_400_000
        for k in range(1, archive_days + 1):
            d0 = day - k * 86_400_000
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as z:
                tt = d0
                while tt < d0 + 86_400_000:
                    z.writestr(f"PUBLIC_DISPATCHIS_{stamp(tt)}.zip", zipped("X.CSV", dispatch_file(grid, tt)))
                    tt += STEP
            self._add("/Reports/Archive/DispatchIS_Reports/", f"PUBLIC_DISPATCHIS_{stamp(d0, 8)}.zip", inner.getvalue())
        self.grid = grid

    def _add(self, d, name, data):
        self.files[d + name] = data
        self.dirs.setdefault(d, []).append(name)

    def serve(self, port):
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlparse(self.path)
                mock.requests.append(u.path)
                if u.path == "/v1/forecast":
                    q = {k: v[0] for k, v in parse_qs(u.query).items()}
                    lat = float(q["latitude"])
                    reg = min(REG, key=lambda r: abs(LATS[r] - lat))
                    days = int(q.get("past_days", 7))
                    end = (max(mock_now[0], 0) // 3_600_000 + 24) * 3_600_000
                    ts = list(range(end - (days + 1) * 86_400_000, end, 3_600_000))
                    body = json.dumps({"hourly": {"time": [t // 1000 for t in ts],
                                                  "temperature_2m": [mock.grid.temp(reg, t) for t in ts]}}).encode()
                    return self._send(200, body, "application/json")
                if u.path in mock.dirs:
                    links = "".join(f'<A HREF="{u.path}{n}">{n}</A><br>' for n in mock.dirs[u.path])
                    return self._send(200, f"<html><body><pre>{links}</pre></body></html>".encode(), "text/html")
                if u.path in mock.files:
                    return self._send(200, mock.files[u.path], "application/zip")
                return self._send(404, b"not found", "text/plain")

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd


LATS = {"NSW1": -33.87, "QLD1": -27.47, "VIC1": -37.81, "SA1": -34.93, "TAS1": -42.88}
mock_now = [0]


def fill_db(conn, grid, start, end, regions=REG, step=STEP, rooftop=True):
    """Write synthetic truth straight into the database (fast path for model tests)."""
    rows, wx, roof = [], [], []
    t = start
    while t <= end:
        for r in regions:
            rows.append((r, t, grid.demand(r, t), grid.price(r, t)))
        t += step
    t = (start // 3_600_000) * 3_600_000 - 3 * 3_600_000
    while t <= end + 3 * 3_600_000:
        for r in regions:
            wx.append((r, t, grid.temp(r, t)))
        t += 3_600_000
    if rooftop:
        t = (start // 1_800_000) * 1_800_000
        while t <= end:
            for r in regions:
                roof.append((r, t, grid.rooftop(r, t)))
            t += 1_800_000
    conn.executemany("INSERT OR REPLACE INTO regional(region,t_ms,demand,price) VALUES(?,?,?,?)", rows)
    conn.executemany("INSERT OR REPLACE INTO weather(region,t_ms,temp) VALUES(?,?,?)", wx)
    conn.executemany("INSERT OR REPLACE INTO rooftop(region,t_ms,mw) VALUES(?,?,?)", roof)
    conn.commit()
