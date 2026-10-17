"""Configuration. Paths in the config file are relative to the file's folder."""
import os
import tomllib
from pathlib import Path

REGIONS = {   # NEM regions, with the capital whose weather drives most of their demand
    "NSW1": {"name": "New South Wales", "short": "NSW", "city": "Sydney", "lat": -33.87, "lon": 151.21},
    "QLD1": {"name": "Queensland", "short": "QLD", "city": "Brisbane", "lat": -27.47, "lon": 153.03},
    "VIC1": {"name": "Victoria", "short": "VIC", "city": "Melbourne", "lat": -37.81, "lon": 144.96},
    "SA1": {"name": "South Australia", "short": "SA", "city": "Adelaide", "lat": -34.93, "lon": 138.60},
    "TAS1": {"name": "Tasmania", "short": "TAS", "city": "Hobart", "lat": -42.88, "lon": 147.33},
}

DEFAULTS = {
    "sources": {
        "nemweb": "https://nemweb.com.au",
        "weather": "https://api.open-meteo.com/v1/forecast",
        "min_gap_s": 2.0,             # between requests to the same host
        "max_files_per_run": 24,      # 2 hours of 5-minute files at most per run
        "history_days": 400,          # the growth estimate needs a year; filled a day or so per run
        "history_per_run": 1,         # archived days per run (each about 6 MB)
        "weather_archive": "https://archive-api.open-meteo.com/v1/archive",
    },
    "storage": {"db": "data/griddesk.db", "unit_detail_hours": 48},
    "alerts": {"price_spike": 1000.0, "departure_sigma": 3.0, "trip_mw": 200.0},
    "notify": {"ntfy_topic": "", "ntfy_server": "https://ntfy.sh", "summary_hour": 7, "timezone": "Australia/Sydney"},
    "server": {"host": "0.0.0.0", "port": 8090},
}


def _coerce(section, key, value, default):
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() in ("true", "yes", "1", "on"):
            return True
        if str(value).strip().lower() in ("false", "no", "0", "off"):
            return False
    elif isinstance(default, (int, float)):
        try:
            x = float(value)
            if x == x and abs(x) != float("inf"):
                return int(x) if isinstance(default, int) and x == int(x) else x
        except (TypeError, ValueError):
            pass
    else:
        return value if value is not None else default
    raise SystemExit(f"config.toml: {key} under [{section}] should be "
                     f"{'true or false' if isinstance(default, bool) else 'a number'}, not {value!r}.")


class Config:
    def __init__(self, path=None, data=None):
        self.path = Path(path).resolve() if path else Path.cwd() / "config.toml"
        self.root = self.path.parent
        raw = data if data is not None else (tomllib.loads(self.path.read_text()) if path else {})
        self.data = {}
        for k, v in DEFAULTS.items():
            got = raw.get(k, {})
            if not isinstance(got, dict):
                raise SystemExit(f"config.toml: [{k}] should be a section.")
            self.data[k] = {**v, **got}
            for key, default in v.items():
                self.data[k][key] = _coerce(k, key, self.data[k][key], default)

    def __getitem__(self, k):
        return self.data[k]

    def resolve(self, rel):
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self):
        return self.resolve(self["storage"]["db"])


def find_config(arg=None):
    for cand in [arg, os.environ.get("GRIDDESK_CONFIG"), "config.toml"]:
        if cand and Path(cand).is_file():
            return Config(cand)
    return Config(data={})   # all defaults: works out of the box
