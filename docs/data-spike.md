# Data spike (milestone 0)

The build machine can't reach AEMO, so the checks run on GitHub's runners: `tools/data_spike.py`, run by the **Data spike** workflow, which publishes `spike/report.json` and sample files to the `data-spike` branch.

What it checks:
- **NEMweb directory listings and file-name patterns:** DispatchIS, Dispatch SCADA, rooftop PV actuals, market notices and the DispatchIS daily archive.
- **Tables and columns** in the newest file of each feed.
- **Market notices:** the format of a recent notice and of a Lack of Reserve notice, through the same parser the app uses.
- **Archive size:** the size of a daily archive.
- **Open-Meteo:** that it answers in the expected shape.
- **AEMO's registration spreadsheet:** that it downloads, and how many units the unit-list builder finds, by fuel.

## Findings (17 October 2026)

All sources reachable from GitHub's runners. Go.

| Source | Result |
|---|---|
| DispatchIS (`/Reports/Current/DispatchIS_Reports/`) | 576 files (two days), ~22 KB each. Tables `DISPATCH,REGIONSUM` (TOTALDEMAND, AVAILABLEGENERATION, NETINTERCHANGE…), `DISPATCH,PRICE` (RRP), `DISPATCH,INTERCONNECTORRES` (MWFLOW, EXPORTLIMIT, IMPORTLIMIT), plus constraint and case tables we skip. |
| Dispatch SCADA (`/Reports/Current/Dispatch_SCADA/`) | 575 files, ~4 KB each; `DISPATCH,UNIT_SCADA` with ~517 units (DUID, SCADAVALUE). |
| Rooftop PV actual (`/Reports/Current/ROOFTOP_PV/ACTUAL/`) | Half-hourly, 14 days kept; `ROOFTOP,ACTUAL` (INTERVAL_DATETIME, REGIONID, POWER, TYPE). |
| Market notices (`/Reports/Current/Market_Notice/`) | ~800 files over two months. Dates are day-first (`17/10/2026     09:24:24`), the reason text has blank lines, and LOR notices look like `STPASA - Forecast Lack Of Reserve Level 2 (LOR2) in the SA Region on 20/10/2026`. Found the first two by running the parser on a real notice; both fixed. |
| DispatchIS daily archive (`/Reports/Archive/DispatchIS_Reports/`) | 375 days (a year), ~6.3 MB each, zips of zips; 1,440 regional rows per day. This makes a year of history practical, so `history_days` defaults to 400. |
| Open-Meteo forecast API | Hourly temperature, up to 92 past days. |
| Open-Meteo archive API | A month of hourly temperature per request; used for history older than 80 days. |
| AEMO registration list (.xlsx) | Downloads; 574 units. The fuel is in the descriptor columns (the primary says "Fossil"), so the builder reads all fuel and technology columns: coal 64, gas 122, wind 109, solar 133, hydro 68, battery 75, other 3. |

Not checked yet: the ESOO workbook's regional data-centre series (milestone 4 uses the published headline figures for now), and the Open Electricity API (not needed: NEMweb covers the live data).
