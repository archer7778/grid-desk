"""Write a read-only snapshot of the console as static files, for the public demo on GitHub Pages.

    python -m griddesk.export_static --out site
"""
import argparse
import json
import shutil
import time
from pathlib import Path

from . import __version__
from .config import REGIONS, find_config
from .server import WEB, App, finite


def _w(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(finite(obj), separators=(",", ":"), allow_nan=False), encoding="utf-8")


def export(cfg, out, log=print):
    out = Path(out)
    api = out / "api"
    if api.exists():
        shutil.rmtree(api)
    t0 = time.time()
    app = App(cfg)
    _w(api / "config.json", app.config())
    _w(api / "state.json", app.state())
    for reg in REGIONS:
        _w(api / f"series-{reg}.json", app.series(reg, 48))
    _w(api / "events.json", app.events(24))
    _w(api / "datacentres.json", app.datacentres())
    _w(api / "summary.json", app.summary())
    snap = int(time.time() * 1000)
    page = (WEB / "index.html").read_text(encoding="utf-8")
    inject = f'<script>window.GD_STATIC={{"base":"api/","snapshot":{snap}}};</script>\n'
    page = page.replace("<script>\n(()=>{", inject + "<script>\n(()=>{", 1)
    (out / "index.html").write_text(page, encoding="utf-8")
    shutil.copy(WEB / "au.json", out / "au.json")
    (out / ".nojekyll").write_text("")
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    log(f"Exported snapshot to {out} ({size / 1e6:.1f} MB) in {time.time() - t0:.1f}s, Grid Desk {__version__}.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export a read-only static demo of the console")
    ap.add_argument("--config")
    ap.add_argument("--out", default="site")
    a = ap.parse_args(argv)
    export(find_config(a.config), a.out)


if __name__ == "__main__":
    main()
