"""A small, polite client for AEMO's public NEMweb file server and for weather.

NEMweb publishes each 5-minute report as a new zip file in a directory listing. We list the
directory, take only files newer than the last one we processed, and never fetch a file twice.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

UA = f"GridDesk/{__version__} (+github.com/archer7778/grid-desk)"

# Directory and file-name patterns. Checked against the live server by the data-spike workflow
# (docs/data-spike.md); kept here so a change on AEMO's side is a one-line fix.
FEEDS = {
    "dispatch": {"dir": "/Reports/Current/DispatchIS_Reports/", "pattern": r"PUBLIC_DISPATCHIS_(\d{12})_\d+\.zip"},
    "scada": {"dir": "/Reports/Current/Dispatch_SCADA/", "pattern": r"PUBLIC_DISPATCHSCADA_(\d{12})_\d+\.zip"},
    "rooftop": {"dir": "/Reports/Current/ROOFTOP_PV/ACTUAL/",
                "pattern": r"PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_(\d{12})\d*_\d+\.zip"},
    "notices": {"dir": "/Reports/Current/Market_Notice/", "pattern": r"NEMITWEB1_MKTNOTICE_(\d{8})\.R\d+"},
    "dispatch_archive": {"dir": "/Reports/Archive/DispatchIS_Reports/", "pattern": r"PUBLIC_DISPATCHIS_(\d{8})\.zip"},
    "rooftop_archive": {"dir": "/Reports/Archive/ROOFTOP_PV/ACTUAL/",
                        "pattern": r"PUBLIC_ROOFTOP_PV_ACTUAL_MEASUREMENT_(\d{8})\.zip"},
}

MAX_FILE = 200_000_000


class FetchError(Exception):
    pass


class Client:
    def __init__(self, base, min_gap_s=2.0):
        self.base = base.rstrip("/")
        self.host = urllib.parse.urlparse(self.base).netloc
        local = self.host.split(":")[0] in ("127.0.0.1", "localhost")
        self.gap = float(os.environ.get("GRIDDESK_MIN_GAP", min_gap_s)) if local else max(1.0, min_gap_s)
        self._last = 0.0
        self.requests = 0

    def get(self, path_or_url, timeout=60, limit=MAX_FILE):
        url = path_or_url if path_or_url.startswith("http") else self.base + path_or_url
        if urllib.parse.urlparse(url).netloc != self.host:
            raise FetchError("refusing a link off the server")
        wait = self.gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.requests += 1
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(limit + 1)
        except urllib.error.HTTPError as e:
            raise FetchError(f"HTTP {e.code} for {url}")
        except (urllib.error.URLError, OSError) as e:
            raise FetchError(f"could not reach {self.host}: {getattr(e, 'reason', e)}")
        if len(data) > limit:
            raise FetchError(f"{url} is larger than {limit} bytes")
        return data

    def listing(self, feed):
        """[(stamp, name, href)] for a feed's directory, oldest first."""
        f = FEEDS[feed]
        html = self.get(f["dir"], timeout=30, limit=20_000_000).decode("utf-8", "replace")
        pat = re.compile(f["pattern"], re.I)
        seen, out = set(), []
        for href in re.findall(r'href\s*=\s*"([^"]+)"', html, re.I):
            name = href.rsplit("/", 1)[-1]
            m = pat.fullmatch(name)
            if m and name not in seen:
                seen.add(name)
                full = href if href.startswith(("/", "http")) else f["dir"] + href
                out.append((m.group(1), name, full))
        out.sort()
        return out


def weather(api, lat, lon, past_days=7, timeout=30, start=None, end=None):
    """Hourly temperature (°C) from Open-Meteo: [(t_ms, temp)]. Free, no key, CC BY 4.0.
    Recent days come from the forecast endpoint (up to 92 days back); older ones from the
    archive endpoint with start and end dates (YYYY-MM-DD)."""
    p = {"latitude": lat, "longitude": lon, "hourly": "temperature_2m", "timezone": "UTC", "timeformat": "unixtime"}
    if start:
        p.update(start_date=start, end_date=end or start)
    else:
        p.update(past_days=max(0, min(92, int(past_days))), forecast_days=1)
    req = urllib.request.Request(f"{api}?{urllib.parse.urlencode(p)}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read(5_000_000))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise FetchError(f"weather unavailable: {e}")
    h = d.get("hourly") or {}
    out = []
    for t, v in zip(h.get("time") or [], h.get("temperature_2m") or []):
        if isinstance(t, (int, float)) and isinstance(v, (int, float)) and -60 < v < 70:
            out.append((int(t) * 1000, float(v)))
    return out
