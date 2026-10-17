# Grid Desk

**A live operations console for Australia's National Electricity Market, with an honest look at how much power data centres use and plan to use.**

Every 5 minutes Grid Desk reads AEMO's public market data: demand, prices, what every power station is generating, interconnector flows, rooftop solar, and AEMO's own reserve notices. For each region it learns what demand normally looks like for the time of day, day of the week, temperature and rooftop sunshine, and says when today departs from that, and why.

**Live demo:** <https://archer7778.github.io/grid-desk/> (rebuilt about every 30 minutes from real AEMO data by GitHub Actions).

## What it does

**The grid, live**
- **Region cards** for Queensland, New South Wales, Victoria, South Australia and Tasmania: demand, price, temperature, rooftop solar and the generation mix.
- **Learned normal:** a model per region (daily shape for weekdays and weekends, heating and cooling with temperature, rooftop solar, a slow drift) fitted to the last four weeks. Each region shows *expected* demand given today's weather, a *typical day* for comparison, and the 90% band of normal days, plus one plain sentence: "Demand is 9% above a typical day for this time; mostly explained by weather (41 °C in Sydney)."
- **Alerts:** AEMO Lack of Reserve notices (LOR1–3, actual and forecast, cleared when AEMO cancels them), price spikes, generating units dropping out in a single interval (likely trips), and demand far from its learned normal.
- **Generation mix** by fuel over 48 hours, with a table view; rooftop solar from AEMO's estimate.
- **Daily summary** to your phone through the free ntfy app.

**Data centres, honestly**
Public market data doesn't meter data centres separately, so Grid Desk keeps three kinds of number apart and labels every one:
- **Published:** AEMO's forecasts. The 2026 Electricity Statement of Opportunities projects data-centre use growing from about 5 TWh a year to about 34 TWh within a decade, mostly in NSW and Victoria.
- **Announced:** a registry of 22 facilities in NSW and Victoria with their stated capacity, status (operating, under construction, approved, proposed) and a source link for each. Capacity is a maximum, not use.
- **Estimated:** the *flat baseline*. A data centre draws almost the same power day and night, so after removing time of day, weekends, temperature and rooftop solar, what's left in each month's demand should rise as data centres connect. Grid Desk fits that trend with a 90% range. Other flat loads move it too, so it's an upper bound, never a measurement.

It never claims to measure one facility's live use and never links a facility to a particular grid event.

## How it works

```
AEMO NEMweb (5-minute files) ──► fetch job ──► SQLite ──► console (live updates)
Open-Meteo temperatures ───────►    │                       read-only JSON API
AEMO registration list ────────►    └─► learned normal, alerts ──► ntfy ──► phone
```

- `griddesk/mms.py` reads AEMO's MMS CSV format by column name, so new or reordered columns don't break it; it handles zips of zips (daily archives) and market-notice text.
- `griddesk/fetch.py` lists each NEMweb directory and downloads only files it hasn't seen, at least 2 seconds apart. History fills in one archived day per run.
- `griddesk/model.py` is the learned normal: a robust (outlier-resistant) harmonic regression fitted to half-hour averages.
- `griddesk/datacentres.py` loads the registry and forecasts (refusing any entry without a source) and fits the flat-baseline trend with a day-resampling bootstrap.
- `griddesk/server.py` serves the page and a read-only API (no write endpoints at all). Standard library only: no packages to install.

## Install

Any Linux machine with Python 3.11 or newer (a desktop, a server or a Raspberry Pi 4):

```bash
git clone https://github.com/archer7778/grid-desk.git
cd grid-desk
./deploy/install.sh
```

It sets up the console at boot and a fetch every 5 minutes. Optional settings are in `config.toml` (phone alerts, thresholds).

## Develop and test

```bash
python3 tests/test_griddesk.py
```

The tests run the whole pipeline against a mock NEMweb serving synthetic data in AEMO's format. They check that the model recovers known patterns and that the baseline estimator finds a flat load hidden in synthetic demand (a 120 MW/year trend comes back as 117–122 MW/year) and reports no trend when there isn't one. They also check that every registry entry has a source and that every data-centre number says what kind it is.

## Limitations

- **Formats:** AEMO's file layouts are checked by `tools/data_spike.py` (run by the *Data spike* workflow), not by the test suite.
- **Holidays:** public holidays look like unusual weekdays.
- **Weather:** one capital city's temperature stands in for a whole region.
- **Baseline trend:** it needs a year of data before seasons stop leaking into it, and it can't tell data centres from other flat loads.
- **Registry:** compiled from public aggregators and operator pages. Capacity figures may mix IT load and grid connection. Corrections with sources are welcome.

## Sources and licences

- Market data: [AEMO NEMweb](https://nemweb.com.au) (public).
- Temperatures: [Open-Meteo](https://open-meteo.com) (CC BY 4.0).
- Coastline: Natural Earth (public domain).
- Registry and forecast sources are linked entry by entry in `data/`.

Grid Desk is an independent project, not affiliated with AEMO or any operator, and nothing here is trading or investment advice.

## Licence

MIT
