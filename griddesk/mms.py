"""Readers for AEMO's public files.

Most NEMweb reports are zipped CSV in AEMO's "MMS" layout: each row starts with a record type.
  C  comment / file header and footer
  I  column header for a table:   I,DISPATCH,REGIONSUM,7,SETTLEMENTDATE,RUNNO,REGIONID,...
  D  data row for the last I row:  D,DISPATCH,REGIONSUM,7,"2026/10/17 14:00:00",1,NSW1,...
A file can hold several tables. Columns are read by name from the I row, never by position,
so AEMO adding or reordering columns doesn't break anything. Daily archives are zips of zips.
"""
import csv
import datetime as _dt
import io
import re
import zipfile
import zlib

NEM_TZ = _dt.timezone(_dt.timedelta(hours=10))   # market time: AEST all year, no daylight saving
MAX_UNZIPPED = 400_000_000                        # refuse zip bombs
MAX_DEPTH = 3


def nem_time(s):
    """'2026/10/17 14:00:00' (market time) -> ms since epoch, or None."""
    if not s:
        return None
    s = " ".join(str(s).strip().strip('"').split())   # market notices pad with runs of spaces
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return int(_dt.datetime.strptime(s, fmt).replace(tzinfo=NEM_TZ).timestamp() * 1000)
        except ValueError:
            continue
    return None


def _num(s):
    try:
        x = float(s)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def parse_csv(text, want=None):
    """Tables in one MMS CSV text: {'DISPATCH,REGIONSUM': [ {col: value, ...}, ... ], ...}.
    want: optional set of table keys to keep (the rest are skipped cheaply)."""
    out, cols, key = {}, None, None
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=False))
    except csv.Error as e:
        raise ValueError(f"not a readable CSV file: {e}")
    for row in rows:
        if not row:
            continue
        kind = row[0].strip().upper()
        if kind == "I" and len(row) > 4:
            key = f"{row[1].strip().upper()},{row[2].strip().upper()}"
            cols = [c.strip().upper() for c in row[4:]]
        elif kind == "D" and cols is not None and len(row) > 4:
            k = f"{row[1].strip().upper()},{row[2].strip().upper()}"
            if k != key or (want and k not in want):
                continue
            vals = row[4:]
            if len(vals) < len(cols):
                vals = vals + [""] * (len(cols) - len(vals))
            out.setdefault(k, []).append(dict(zip(cols, vals)))
    return out


def read_any(data, want=None, depth=0, budget=None):
    """Tables from raw bytes that may be CSV, a zip of CSVs, or a zip of zips (daily archives).
    Anything wrong with the file (a truncated download, corrupt compression) is a ValueError."""
    try:
        return _read_any(data, want, depth, budget)
    except ValueError:
        raise
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, EOFError, OSError, csv.Error, RuntimeError) as e:
        raise ValueError(f"damaged file: {type(e).__name__}: {e}")


def _read_any(data, want=None, depth=0, budget=None):
    budget = budget if budget is not None else [MAX_UNZIPPED]
    if data[:2] != b"PK":
        return parse_csv(data.decode("utf-8", "replace"), want)
    if depth >= MAX_DEPTH:
        raise ValueError("zip nested too deeply")
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            budget[0] -= info.file_size
            if budget[0] < 0:
                raise ValueError("archive too large when unzipped")
            inner = z.read(info)
            name = info.filename.lower()
            if name.endswith(".zip") or inner[:2] == b"PK":
                tables = _read_any(inner, want, depth + 1, budget)
            elif name.endswith((".csv", ".txt")) or b",\"" in inner[:200] or inner[:2] in (b"C,", b"I,"):
                tables = parse_csv(inner.decode("utf-8", "replace"), want)
            else:
                continue
            for k, rows in tables.items():
                out.setdefault(k, []).extend(rows)
    return out


# ---------- market notices (plain text) ----------

_LOR = re.compile(r"\b(?:actual|forecast)?\s*\(?\s*LOR\s*([123])\s*\)?", re.I)
_REGION = re.compile(r"\b(NSW|QLD|VIC|SA|TAS)(?:1)?\b(?:\s+region)?", re.I)


def parse_notice(text):
    """A NEMweb market notice file -> dict, or None if it doesn't look like one.
    Notices are 'Key : value' lines (Notice ID, Notice Type ID, Issue Date, External Reference,
    Reason). Lack of Reserve (LOR) notices are AEMO's official signal that supply is getting tight."""
    fields, reason, in_reason = {}, [], False
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z][A-Za-z ]{2,40}?)\s*:\s*(.*)$", line)
        if m and not in_reason:
            k = m.group(1).strip().lower()
            fields[k] = m.group(2).strip()
            in_reason = k == "reason"
            if in_reason and fields[k]:
                reason.append(fields[k])
        elif in_reason:
            if line.strip() and set(line.strip()) <= set("-_="):
                if reason:
                    in_reason = False
                continue
            reason.append(line.rstrip())
    nid = fields.get("notice id")
    try:
        nid = int(re.sub(r"\D", "", nid or ""))
    except ValueError:
        return None
    ref = fields.get("external reference", "")
    body = " ".join(x.strip() for x in reason if x.strip())[:2000]
    t = nem_time(fields.get("creation date")) or nem_time(fields.get("issue date"))   # creation has the time of day
    blob = f"{ref} {body}"
    lor = _LOR.search(blob)
    reg = _REGION.search(ref) or _REGION.search(body)
    low = blob.lower()
    kind = "cancelled" if "cancel" in low else "actual" if "actual" in low else "forecast" if "forecast" in low else None
    return {"id": nid, "t_ms": t, "type": (fields.get("notice type id") or "").strip()[:60],
            "region": (reg.group(1).upper() + "1") if reg else None,
            "lor": int(lor.group(1)) if lor else None, "kind": kind,
            "reference": ref[:300], "reason": body}


_PERIOD = re.compile(r"from\s+(\d{3,4})\s*hrs\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+(?:to|until)\s+(\d{3,4})\s*hrs\s+"
                     r"(\d{1,2})/(\d{1,2})/(\d{4})", re.I)
_REQUIRED = re.compile(r"reserve requirement is\s+([\d,]+(?:\.\d+)?)\s*MW", re.I)
_AVAILABLE = re.compile(r"reserve available is\s+([\d,]+(?:\.\d+)?)\s*MW", re.I)
_ON_DAY = re.compile(r"\bon\s+(\d{1,2})/(\d{1,2})/(\d{4})\b")


def _hhmm(hhmm, d, m, y):
    """'0600', 20, 10, 2026 (market time) -> ms; '2400' is midnight at the end of that day."""
    hh, mm = divmod(int(hhmm), 100)
    if hh > 24 or mm > 59:
        raise ValueError(hhmm)
    t = _dt.datetime(int(y), int(m), int(d), tzinfo=NEM_TZ) + _dt.timedelta(hours=hh, minutes=mm)
    return int(t.timestamp() * 1000)


def lor_details(reason, reference=""):
    """What a Lack of Reserve notice says about when and how much. AEMO writes, for example:
    'for the following period[s]: [1.] From 0600 hrs 20/10/2026 to 0900 hrs 20/10/2026. The forecast
    capacity reserve requirement is 524 MW. The minimum capacity reserve available is 487 MW.'
    Returns {periods: [[start_ms, end_ms]], day: 'YYYY-MM-DD' or None, required_mw, available_mw}."""
    text = f"{reference or ''} {reason or ''}"
    periods = []
    for g in _PERIOD.findall(text):
        try:
            a, b = _hhmm(g[0], *g[1:4]), _hhmm(g[4], *g[5:8])
        except (ValueError, OverflowError):
            continue
        if a < b:
            periods.append([a, b])
    day = None
    m = _ON_DAY.search(reference or "") or _ON_DAY.search(reason or "")
    try:
        if m:
            day = _dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        elif periods:
            day = _dt.datetime.fromtimestamp(periods[0][0] / 1000, NEM_TZ).date().isoformat()
    except ValueError:
        day = None
    req = [float(x.replace(",", "")) for x in _REQUIRED.findall(text)]
    av = [float(x.replace(",", "")) for x in _AVAILABLE.findall(text)]
    return {"periods": periods, "day": day, "required_mw": max(req) if req else None,
            "available_mw": min(av) if av else None}


def num(s):
    return _num(s)
