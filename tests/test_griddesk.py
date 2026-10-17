"""Grid Desk tests: parsing, the fetch pipeline against a mock NEMweb, the learned normal, the
baseline estimator, the data-centre registry and the API.

    python tests/test_griddesk.py      (or: python -m pytest tests -q)
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ["GRIDDESK_MIN_GAP"] = "0"

import synth  # noqa: E402
from griddesk import alerts, datacentres, fetch, mms, model  # noqa: E402
from griddesk import db as dbm  # noqa: E402
from griddesk.config import Config  # noqa: E402

DAY = 86_400_000


def cfg_for(tmp, port=None, **extra):
    data = {"storage": {"db": str(tmp / "data" / "g.db")}, **extra}
    if port:
        data["sources"] = {"nemweb": f"http://127.0.0.1:{port}", "weather": f"http://127.0.0.1:{port}/v1/forecast",
                           "history_days": 2, **extra.get("sources", {})}
    return Config(data=data)


def now5():
    return int(time.time() * 1000) // 300_000 * 300_000


def test_parse_mms_by_column_name():
    g = synth.Grid()
    t = now5()
    tables = mms.parse_csv(synth.dispatch_file(g, t))
    rs = tables["DISPATCH,REGIONSUM"]
    assert len(rs) == 6 and rs[0]["REGIONID"] == "NSW1" and float(rs[0]["TOTALDEMAND"]) > 0
    assert mms.nem_time(rs[0]["SETTLEMENTDATE"]) == t
    # reordered columns still read correctly
    text = synth.mms([("DISPATCH", "PRICE", 5, ["RRP", "REGIONID", "SETTLEMENTDATE", "INTERVENTION"], [[77.5, "SA1", synth.nemstr(t), 0]])])
    p = mms.parse_csv(text)["DISPATCH,PRICE"][0]
    assert p["RRP"] == "77.5" and p["REGIONID"] == "SA1"
    # zip of zips (daily archive)
    import io
    import zipfile
    inner = synth.zipped("a.CSV", synth.dispatch_file(g, t))
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("PUBLIC_DISPATCHIS_x.zip", inner)
        z.writestr("PUBLIC_DISPATCHIS_y.zip", synth.zipped("b.CSV", synth.dispatch_file(g, t + 300_000)))
    assert len(mms.read_any(b.getvalue())["DISPATCH,PRICE"]) == 10
    # market notices
    n = mms.parse_notice(synth.notice_text(123456, t, lor=2, region="NSW"))
    assert n["id"] == 123456 and n["lor"] == 2 and n["region"] == "NSW1" and n["kind"] == "actual", n
    assert n["t_ms"] == t // 1000 * 1000 and "capacity reserve requirement" in n["reason"], n   # day-first dates, full text
    c = mms.parse_notice(synth.notice_text(123457, t, lor=2, region="NSW", cancel=True))
    assert c["kind"] == "cancelled"
    assert mms.parse_notice("garbage") is None
    print("parse: ok")


def test_fetch_pipeline():
    tmp = Path(tempfile.mkdtemp())
    now = now5()
    synth.mock_now[0] = now
    g = synth.Grid(seed=2)
    notices = [{"id": 900001, "t": now - 3 * 3600_000, "lor": 1, "region": "SA", "kind": "Forecast"},
               {"id": 900002, "t": now - 2 * 3600_000, "lor": 2, "region": "NSW"},
               {"id": 900003, "t": now - 1 * 3600_000, "lor": 2, "region": "NSW", "cancel": True},
               {"id": 900004, "t": now - 30 * 60_000, "lor": 2, "region": "VIC"}]
    mock = synth.MockNEMweb(g, now, minutes=60, archive_days=2, notices=notices, trip_at=now - 5 * 60_000)
    httpd = mock.serve(8851)
    try:
        cfg = cfg_for(tmp, 8851)
        (tmp / "data").mkdir(parents=True, exist_ok=True)
        with open(tmp / "data" / "duids.csv", "w") as f:
            f.write("duid,region,fuel,station,capacity_mw\n")
            for duid, reg, fuel, cap in synth.UNITS:
                f.write(f"{duid},{reg},{fuel},{duid.title()} station,{cap}\n")
        cfg.root = tmp   # data/duids.csv is looked up next to the config
        msgs = []
        assert fetch.run(cfg, log=msgs.append) == 0, msgs
        conn = dbm.open_db(cfg)
        n_reg = conn.execute("SELECT COUNT(*) FROM regional WHERE t_ms>?", (now - 3600_000,)).fetchone()[0]
        print("fetch:", msgs[-1], "| regional rows (last hour):", n_reg)
        assert n_reg == 12 * 5   # first run takes the newest 12 files, five regions each
        assert conn.execute("SELECT MAX(demand) FROM regional").fetchone()[0] < 90000   # intervention row ignored
        assert conn.execute("SELECT COUNT(*) FROM interconnectors").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM mix WHERE fuel='coal'").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM rooftop").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM weather").fetchone()[0] > 100
        # history: two archived days arrive one per run; rooftop backfills from the current folder
        assert conn.execute("SELECT COUNT(*) FROM regional WHERE t_ms<?", (now - DAY,)).fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(DISTINCT t_ms) FROM rooftop").fetchone()[0] >= 2
        # a second run fetches only what's new (nothing), and doesn't refetch files
        before = len(mock.requests)
        fetch.run(cfg, log=msgs.append)
        fetched_again = [p for p in mock.requests[before:] if p.endswith(".zip") and "Archive" not in p]
        assert not fetched_again, fetched_again
        # reserve: NSW LOR2 was cancelled, VIC LOR2 is active, SA forecast LOR1 active
        lor = alerts.lor_status(conn, now)
        got = {(a["region"], a["level"], a["basis"]) for a in lor}
        print("reserve:", got)
        assert ("VIC1", 2, "actual") in got and ("SA1", 1, "forecast") in got and not any(r == "NSW1" for r, _, _ in got)
        # the trip of ERARING1 (576 MW to 0) is caught
        tr = alerts.trips(conn, cfg, now)
        assert any(t["duid"] == "ERARING1" for t in tr), tr
        # a battery stopping in an instant is normal, not a trip
        conn.execute("INSERT OR REPLACE INTO units(duid,t_ms,mw) VALUES('VBBG1',?,283),('VBBG1',?,0)", (now - 600_000, now - 300_000))
        conn.commit()
        assert not any(t["duid"] == "VBBG1" for t in alerts.trips(conn, cfg, now))
        al = alerts.current(conn, cfg, now)
        assert any(a["kind"] == "reserve" and a["sev"] == "crit" for a in al)
        s = alerts.summary(conn, cfg, now)
        assert "NSW: peak" in s["text"] and "Lack of Reserve" in s["text"], s
        print("alerts:", [a["msg"][:60] for a in al])
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp)


def test_rooftop_history():
    tmp = Path(tempfile.mkdtemp())
    now = now5()
    synth.mock_now[0] = now
    mock = synth.MockNEMweb(synth.Grid(seed=7), now, minutes=180, archive_days=21)
    httpd = mock.serve(8853)
    try:
        cfg = cfg_for(tmp, 8853, sources={"history_days": 30})
        conn = dbm.open_db(cfg)
        client = fetch.Client(cfg["sources"]["nemweb"], 0)
        got = fetch.fill_rooftop(conn, cfg, client, print, recent_per_run=100, weeks_per_run=5)
        span = conn.execute("SELECT (MAX(t_ms)-MIN(t_ms))/86400000.0, COUNT(*) FROM rooftop").fetchone()
        print(f"rooftop history: {got} files, {span[1]} rows over {span[0]:.1f} days")
        assert span[0] > 14 and got >= 3
        before = len(mock.requests)
        fetch.fill_rooftop(conn, cfg, client, print, recent_per_run=100, weeks_per_run=5)
        refetched = [p for p in mock.requests[before:] if p.endswith(".zip")]
        assert not refetched, refetched   # gaps are found from the data: nothing fetched twice
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp)


def test_normal_model_learns():
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = cfg_for(tmp)
        conn = dbm.open_db(cfg)
        now = now5()
        g = synth.Grid(seed=4)
        synth.fill_db(conn, g, now - 28 * DAY, now)
        # a week that never gets cold (no heating at all) still fits
        conn.execute("UPDATE weather SET temp=MAX(temp, 16.0) WHERE region='QLD1'")
        conn.commit()
        assert model.RegionModel(conn, "QLD1", now).coef is not None
        m = model.RegionModel(conn, "NSW1", now)
        # the synthetic truth: rooftop solar lowers demand 0.9 MW per MW; noise sd 60 MW
        roof = m.coef[-2]
        print(f"model: sigma {m.sigma:.1f} MW (noise 60), rooftop coefficient {roof:.2f} (truth -0.90)")
        assert m.sigma < 100 and -1.1 < roof < -0.7
        # a hot afternoon is explained by weather, not flagged as strange
        a = model.assess(conn, now)
        assert all(v["status"] == "ok" for v in a.values()), a
        assert all(abs(v["z"]) < 4 for v in a.values())
        # an unexplained 900 MW jump is flagged
        conn.execute("UPDATE regional SET demand=demand+900 WHERE region='NSW1' AND t_ms=?", (now,))
        conn.commit()
        a = model.assess(conn, now)["NSW1"]
        assert a["z"] > 5 and a["z_sustained"] < 5   # one interval: noted, not an alert
        conn.execute("UPDATE regional SET demand=demand+900 WHERE region='NSW1' AND t_ms IN (?,?)", (now - 300_000, now - 600_000))
        conn.commit()
        a = model.assess(conn, now)["NSW1"]
        print("model: +900 MW for 15 min ->", a["z_sustained"], a["reason"])
        assert a["z_sustained"] > 5 and "more than weather" in a["reason"]
    finally:
        shutil.rmtree(tmp)


def test_baseline_finds_flat_load():
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = cfg_for(tmp)
        now = int(time.time() * 1000) // 3600_000 * 3600_000
        start = now - 15 * 30 * DAY
        for growth, label in ((10.0, "growing"), (0.0, "none")):
            conn = dbm.open_db(cfg)
            conn.execute("DELETE FROM regional")
            conn.commit()
            g = synth.Grid(seed=5, flat_growth_mw_per_month=growth, start_ms=start)
            synth.fill_db(conn, g, start, now, regions=["NSW1"], step=900_000)
            b = datacentres.baseline(conn, "NSW1", now)
            lo, hi = b["trend_range_mw_per_year"]
            truth = growth * 12
            print(f"baseline ({label}): {b['trend_mw_per_year']} MW/yr, range {lo}..{hi}, truth {truth}")
            assert b["kind"] == "estimated" and b["status"] == "ok"
            assert lo <= truth <= hi, (lo, truth, hi)
        # too little data: says so
        conn.execute("DELETE FROM regional WHERE t_ms<?", (now - 40 * DAY,))
        conn.commit()
        b = datacentres.baseline(conn, "NSW1", now)
        assert b["status"] == "learning" and b["need_months"] >= 1
    finally:
        shutil.rmtree(tmp)


def test_registry_rules():
    fac = datacentres.registry(ROOT / "data" / "datacentres.toml")
    assert len(fac) >= 20 and all(f["sources"] and f["kind"] == "announced" for f in fac)
    fc = datacentres.forecasts(ROOT / "data" / "forecasts.toml")
    assert all(f["kind"] == "published" and f["sources"] for f in fc["figures"])
    tmp = Path(tempfile.mkdtemp())
    try:
        bad = tmp / "bad.toml"
        bad.write_text('[[facility]]\nname="X"\nregion="NSW1"\nstatus="operating"\ncapacity_mw=10\n')
        try:
            datacentres.registry(bad)
            raise AssertionError("unsourced facility accepted")
        except datacentres.RegistryError:
            pass
        bad.write_text('[[source]]\nid="a"\ntitle="t"\nurl="javascript:alert(1)"\n')
        try:
            datacentres.registry(bad)
            raise AssertionError("non-https source accepted")
        except datacentres.RegistryError:
            pass
    finally:
        shutil.rmtree(tmp)
    print(f"registry: {len(fac)} facilities, all sourced")


def test_api_and_export():
    from http.server import ThreadingHTTPServer
    from griddesk import server, export_static
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = cfg_for(tmp)
        conn = dbm.open_db(cfg)
        now = now5()
        synth.fill_db(conn, synth.Grid(seed=6), now - 10 * DAY, now)
        app = server.App(cfg)
        httpd = ThreadingHTTPServer(("127.0.0.1", 8852), server.make_handler(app))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def get(p):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8852" + p, timeout=60) as r:
                    return r.status, json.loads(r.read()) if p.startswith("/api") else r.read()
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        s, st = get("/api/state")
        assert s == 200 and st["regions"]["NSW1"]["normal"]["status"] == "ok"
        s, se = get("/api/series?region=VIC1&hours=24")
        assert s == 200 and len(se["points"]) > 200
        s, dc = get("/api/datacentres")
        assert s == 200 and len(dc["facilities"]) >= 20
        # every data-centre number says what kind it is
        assert all(f["kind"] == "announced" for f in dc["facilities"])
        assert all(f["kind"] == "published" for f in dc["forecasts"]["figures"])
        assert all(b["kind"] == "estimated" for b in dc["baseline"].values())
        assert all(p["kind"] in ("announced", "published") for p in dc["pressure"].values())
        for bad in ("/api/series?region=XX", "/api/series?hours=nan", "/api/series?hours=1e400", "/api/nope",
                    "/../../etc/passwd", "/api/events?hours=-5"):
            s, _ = get(bad)
            assert s in (200, 400, 404), (bad, s)
        req = urllib.request.Request("http://127.0.0.1:8852/api/state", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(req)
            raise AssertionError("POST accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 405
        s, page = get("/")
        assert s == 200 and b"Grid Desk" in page
        out = tmp / "site"
        export_static.export(cfg, out, log=lambda m: None)
        assert (out / "api" / "state.json").exists() and (out / "api" / "series-NSW1.json").exists()
        assert b"GD_STATIC" in (out / "index.html").read_bytes()
        httpd.shutdown()
        print("api + export: ok")
    finally:
        shutil.rmtree(tmp)


def _xlsx(rows):
    import io
    import zipfile
    def cell(ref, v):
        return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
    sheet = "".join(f'<row r="{i + 1}">' + "".join(cell(chr(65 + j) + str(i + 1), v) for j, v in enumerate(r)) + "</row>"
                    for i, r in enumerate(rows))
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   '<sheet name="PU and Scheduled Loads" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   f"<sheetData>{sheet}</sheetData></worksheet>")
    return b.getvalue()


def test_unit_list_and_fuzz():
    from griddesk import duids
    rows = [["NEM Registration and Exemption List"], [],
            ["Participant", "Station Name", "Region", "Dispatch Type", "Fuel Source - Primary", "Technology Type - Primary", "DUID", "Reg Cap (MW)"],
            ["Origin", "Eraring Power Station", "NSW1", "Generator", "Fossil", "Combustion - Black Coal", "ER01", "720"],
            ["Neoen", "Victorian Big Battery", "VIC1", "Bidirectional", "Battery storage", "Storage", "VBBG1", "300"],
            ["X", "Hornsdale Wind Farm", "SA1", "Generator", "Wind", "Renewable", "HDWF1", "102"],
            ["X", "Tumut 3", "NSW1", "Generator", "Hydro", "Renewable", "TUMUT3", "1800"]]
    units = {u["duid"]: u for u in duids.build(_xlsx(rows))}
    assert units["ER01"]["fuel"] == "coal" and units["VBBG1"]["fuel"] == "battery" and units["HDWF1"]["fuel"] == "wind"
    assert units["TUMUT3"]["fuel"] == "hydro" and units["ER01"]["capacity_mw"] == 720.0 and units["ER01"]["region"] == "NSW1"
    # parser fuzz: truncated, mangled and random inputs must never raise anything but ValueError
    import random
    rnd = random.Random(9)
    good = synth.zipped("a.CSV", synth.dispatch_file(synth.Grid(), now5())).hex()
    text = synth.dispatch_file(synth.Grid(), now5())
    bad = 0
    for i in range(400):
        kind = i % 4
        if kind == 0:
            data = bytes.fromhex(good)[: rnd.randrange(1, len(good) // 2)]
        elif kind == 1:
            b = bytearray(bytes.fromhex(good))
            for _ in range(5):
                b[rnd.randrange(len(b))] = rnd.randrange(256)
            data = bytes(b)
        elif kind == 2:
            lines = text.splitlines()
            rnd.shuffle(lines)
            data = "\n".join(l[: rnd.randrange(len(l) + 1)] for l in lines).encode()
        else:
            data = bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 400)))
        try:
            mms.read_any(data)
            mms.parse_notice(data.decode("utf-8", "replace"))
        except (ValueError, KeyError, EOFError):
            pass
        except Exception as e:   # zipfile.BadZipFile is a ValueError subclass? if not, it's counted
            import zipfile
            if isinstance(e, zipfile.BadZipFile):
                continue
            bad += 1
            print("fuzz:", type(e).__name__, e)
    assert bad == 0
    print("unit list + parser fuzz: ok")


if __name__ == "__main__":
    test_parse_mms_by_column_name()
    test_fetch_pipeline()
    test_rooftop_history()
    test_normal_model_learns()
    test_baseline_finds_flat_load()
    test_registry_rules()
    test_api_and_export()
    test_unit_list_and_fuzz()
    print("OK")
