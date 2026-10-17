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
- 2026-10-17 · **Batteries are never "trips":** they stop or reverse in one interval by design (found on real data: "Orana BESS dropped from 283 to 0 MW").
- 2026-10-17 · **Demand alerts need three intervals in a row** beyond the threshold. Single intervals reach 3–4σ from noise alone.
- 2026-10-17 · **The explanation sentence only blames "something other than weather" when the gap is at least 2σ.** Below that, it says the gap is within ordinary variation.
- 2026-10-17 · **Rooftop solar is a model input only when its history covers 80% of the window.** Otherwise the model drops it instead of throwing away the intervals that have no rooftop reading.
- 2026-10-17 · **Reserve conditions are keyed by the day they're for and expire when their stated period ends.** Forecast LOR notices are often days ahead ("in the SA Region on 20/10/2026 … From 0600 hrs to 0900 hrs"); showing them as current, or letting a cancellation for one day cancel another, misled readers (found by an outside review of the demo).
- 2026-10-17 · **"Grid demand", not "operational demand":** the figure is AEMO's dispatch TOTALDEMAND (scheduled and semi-scheduled supply), which leaves out rooftop solar and small non-scheduled generators. Underlying demand is shown separately as an approximation.
- 2026-10-17 · **Interconnectors are shown as named directions** ("VIC → SA"), using AEMO's per-link sign convention, rather than signed numbers.
- 2026-10-17 · **Every age on the page is computed from one clock every second,** and the demo says plainly that it's a snapshot, not live.
