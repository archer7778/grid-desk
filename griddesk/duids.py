"""Build data/duids.csv (unit -> region, fuel, station, capacity) from AEMO's NEM Registration and
Exemption List spreadsheet. No dependencies: an .xlsx file is a zip of XML.

    python -m griddesk.duids [path-or-url-to-xlsx] [--out data/duids.csv]

The fetch job runs this by itself when the list is missing or a month old.
"""
import argparse
import csv
import io
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

URL = ("https://www.aemo.com.au/-/media/files/electricity/nem/participant_information/"
       "nem-registration-and-exemption-list.xlsx")
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def sheets(data):
    """{sheet name: [[cell text, ...], ...]} from xlsx bytes."""
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels}
    out = {}
    for s in wb.find("m:sheets", NS):
        rid = s.get(f"{{{NS['r']}}}id")
        path = "xl/" + target[rid].lstrip("/").removeprefix("xl/")
        rows = []
        for row in ET.fromstring(z.read(path)).iter(f"{{{NS['m']}}}row"):
            vals = {}
            for c in row.findall("m:c", NS):
                col = re.match(r"[A-Z]+", c.get("r", "A")).group(0)
                idx = 0
                for ch in col:
                    idx = idx * 26 + ord(ch) - 64
                v = c.find("m:v", NS)
                if c.get("t") == "s" and v is not None:
                    text = shared[int(v.text)]
                elif c.get("t") == "inlineStr":
                    text = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
                else:
                    text = v.text if v is not None else ""
                vals[idx - 1] = (text or "").strip()
            if vals:
                rows.append([vals.get(i, "") for i in range(max(vals) + 1)])
        out[s.get("name")] = rows
    return out


def build(data):
    from .ingest import fuel_of
    units = {}
    for name, rows in sheets(data).items():
        for hi, row in enumerate(rows[:15]):
            low = [c.lower() for c in row]
            if "duid" in low and any("region" in c for c in low):
                col = {c: i for i, c in enumerate(low)}

                def pick(*keys):
                    for k in keys:
                        for c, i in col.items():
                            if k in c:
                                return i
                    return None
                i_duid, i_reg = col["duid"], pick("region")
                # the primary column says "Fossil"; the descriptor says which fossil: read them all
                i_text = [i for c, i in col.items() if "fuel" in c or "technology" in c]
                i_st = pick("station name", "station")
                i_cap = pick("reg cap", "max cap", "capacity")
                for r in rows[hi + 1:]:
                    g = lambda i: r[i] if i is not None and i < len(r) else ""
                    duid = g(i_duid)
                    if not duid or duid in units:
                        continue
                    text = " ".join(g(i) for i in i_text)
                    fuel = "battery" if "battery" in text.lower() or "storage" in text.lower() else fuel_of(text)
                    try:
                        cap = float(re.sub(r"[^0-9.]", "", g(i_cap)) or "nan")
                    except ValueError:
                        cap = None
                    units[duid] = {"duid": duid, "region": g(i_reg), "fuel": fuel, "station": g(i_st),
                                   "capacity_mw": "" if cap is None or cap != cap else cap}
                break
    return list(units.values())


def download(url=URL, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 GridDesk (+github.com/archer7778/grid-desk)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(60_000_000)


def write(units, out):
    tmp = Path(out).with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, ["duid", "region", "fuel", "station", "capacity_mw"])
        w.writeheader()
        w.writerows(sorted(units, key=lambda u: u["duid"]))
    tmp.replace(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default=URL)
    ap.add_argument("--out", default="data/duids.csv")
    a = ap.parse_args(argv)
    if a.src.startswith("http"):
        data = download(a.src)
    else:
        data = Path(a.src).read_bytes()
    units = build(data)
    if len(units) < 50:
        raise SystemExit(f"Only {len(units)} units found; the spreadsheet layout may have changed.")
    write(units, a.out)
    from collections import Counter
    print(f"{len(units)} units -> {a.out}:", dict(Counter(u["fuel"] for u in units)))


if __name__ == "__main__":
    main()
