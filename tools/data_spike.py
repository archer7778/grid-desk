"""Milestone 0: check every data source against the live servers and write what was found.
Run by .github/workflows/data-spike.yml (GitHub's runners can reach AEMO; the build machine can't).

    python tools/data_spike.py --out spike
"""
import argparse
import json
import sys
import time
import traceback
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from griddesk import mms  # noqa: E402
from griddesk.nemweb import FEEDS, Client  # noqa: E402

UA = {"User-Agent": "GridDesk data spike (+github.com/archer7778/grid-desk)"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="spike")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rep = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "feeds": {}}
    c = Client("https://nemweb.com.au", 1.5)
    for feed, f in FEEDS.items():
        r = rep["feeds"][feed] = {"dir": f["dir"]}
        try:
            html = c.get(f["dir"], limit=20_000_000).decode("utf-8", "replace")
            (out / f"listing_{feed}.html").write_text(html[:20000])
            ls = c.listing(feed)
            r.update(files_matching=len(ls), newest=ls[-1][1] if ls else None, oldest=ls[0][1] if ls else None)
            if not ls:
                r["note"] = "pattern matched nothing; see listing_*.html"
                continue
            if feed == "dispatch_archive":
                data = c.get(ls[-1][2])
                r["archive_newest_size_bytes"] = len(data)
                t = mms.read_any(data, {"DISPATCH,REGIONSUM", "DISPATCH,PRICE"})
                rs = t.get("DISPATCH,REGIONSUM", [])
                r["archive_regionsum_rows"] = len(rs)
                r["archive_first_last"] = [rs[0].get("SETTLEMENTDATE"), rs[-1].get("SETTLEMENTDATE")] if rs else None
                continue
            data = c.get(ls[-1][2])
            r["size_bytes"] = len(data)
            if feed == "notices":
                text = data.decode("utf-8", "replace")
                (out / "sample_notice.txt").write_text(text[:6000])
                r["parsed"] = mms.parse_notice(text)
                # find a reserve notice among the recent ones
                for _, name, href in reversed(ls[-200:]):
                    t = c.get(href).decode("utf-8", "replace")
                    if "LOR" in t:
                        (out / "sample_lor_notice.txt").write_text(t[:6000])
                        r["sample_lor"] = mms.parse_notice(t)
                        break
            else:
                tables = mms.read_any(data)
                r["tables"] = {k: {"rows": len(v), "columns": list(v[0].keys()) if v else []} for k, v in tables.items()}
                r["first_rows"] = {k: v[:2] for k, v in tables.items()}
        except Exception as e:
            r["error"] = f"{type(e).__name__}: {e}"
            r["trace"] = traceback.format_exc()[-1500:]
    # weather
    try:
        u = "https://api.open-meteo.com/v1/forecast?latitude=-33.87&longitude=151.21&hourly=temperature_2m&past_days=2&forecast_days=1&timezone=UTC&timeformat=unixtime"
        d = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30).read())
        rep["weather"] = {"hours": len(d["hourly"]["time"]), "first": d["hourly"]["time"][0], "sample": d["hourly"]["temperature_2m"][:3]}
    except Exception as e:
        rep["weather"] = {"error": str(e)}
    try:
        u = ("https://archive-api.open-meteo.com/v1/archive?latitude=-33.87&longitude=151.21&hourly=temperature_2m"
             "&start_date=2025-10-01&end_date=2025-10-31&timezone=UTC&timeformat=unixtime")
        d = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=60).read())
        rep["weather_archive"] = {"hours": len(d["hourly"]["time"]), "sample": d["hourly"]["temperature_2m"][:3]}
    except Exception as e:
        rep["weather_archive"] = {"error": str(e)}
    # unit list
    try:
        from griddesk.duids import URL, build
        data = urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 GridDesk"}), timeout=120).read()
        units = build(data)
        from collections import Counter
        rep["duids"] = {"units": len(units), "fuels": dict(Counter(u["fuel"] for u in units)), "sample": units[:5]}
    except Exception as e:
        rep["duids"] = {"error": f"{type(e).__name__}: {e}"}
    (out / "report.json").write_text(json.dumps(rep, indent=2, default=str))
    print(json.dumps({k: (v if k != "feeds" else {f: {kk: vv for kk, vv in x.items() if kk in ("files_matching", "newest", "error", "size_bytes")} for f, x in v.items()}) for k, v in rep.items()}, indent=1, default=str)[:6000])


if __name__ == "__main__":
    main()
