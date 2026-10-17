# Decisions

One line per real decision and why.

- 2026-10-17 · **AEMO NEMweb directly, not the Open Electricity API**, for live data. Public, no key, no non-commercial restriction. Open Electricity stays an option for metadata.
- 2026-10-17 · **Open-Meteo for temperature instead of the Bureau of Meteorology.** Free, no key, CC BY 4.0, with past days in one request. BoM's terms for automated reuse are stricter.
- 2026-10-17 · **The registry is TOML (`data/datacentres.toml`), not YAML**, because Python reads TOML with no extra package. Same content as the brief describes.
- 2026-10-17 · **No numpy and no packages at all**, so it installs anywhere, Pi included. Fits are done on half-hour or hourly averages to keep pure-Python maths fast.
- 2026-10-17 · **The console is read-only:** no write endpoints, so no logins and no cross-site-request risk. Operator features (acks, notes) would come later with authentication.
- 2026-10-17 · **The baseline estimate's range resamples whole days within each month (bootstrap)**, conditional on the fitted daily shape and weather terms. Fast enough for a Pi, and it carries day-to-day noise into the range.
- 2026-10-17 · **The unit list (fuel per generator) is built from AEMO's registration spreadsheet** by a tiny no-dependency .xlsx reader. Units missing from it show as "unclassified" rather than guessed.
- 2026-10-17 · **Facility positions:** suburb coordinates only where the source names a suburb; otherwise the city, drawn dashed as approximate.
- 2026-10-17 · **Map without tile servers:** a Natural Earth coastline drawn in SVG, so there's no third-party tile dependency or usage-policy issue for a public site.
