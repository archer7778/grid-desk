# Data spike (milestone 0)

The build machine can't reach AEMO, so the checks run on GitHub's runners: `tools/data_spike.py`, run by the **Data spike** workflow, which publishes `spike/report.json` and sample files to the `data-spike` branch.

What it checks:
- **NEMweb directory listings and file-name patterns:** DispatchIS, Dispatch SCADA, rooftop PV actuals, market notices and the DispatchIS daily archive.
- **Tables and columns** in the newest file of each feed.
- **Market notices:** the format of a recent notice and of a Lack of Reserve notice, through the same parser the app uses.
- **Archive size:** the size of a daily archive.
- **Open-Meteo:** that it answers in the expected shape.
- **AEMO's registration spreadsheet:** that it downloads, and how many units the unit-list builder finds, by fuel.

## Findings

_Filled in from the first run._
