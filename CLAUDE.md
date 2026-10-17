# Grid Desk: brief for Claude

Working name. A live operations console for Australia's National Electricity Market (NEM). It also shows, honestly, how much pressure data centres are putting on the grid.

A sibling of Downlink Desk (github.com/archer7778/downlink-desk), so reuse its patterns: the polite fetch job, SQLite, the learned-normal model, alerts, morning summary, signed log, static demo on GitHub Pages and self-updating install.

## 1. What it's for

- **Live view:** demand, price and generation mix by region every 5 minutes. Flag when today departs from what's normal for this time of day, season and weather, and say why if the data can.
- **Data-centre pressure:** where data centres are, how much power they're estimated to draw and are planned to draw, and how that compares with the grid's headroom. Track it over months and years, not just live.
- **Audience:** energy-curious public, students, journalists, and people in Sydney and Melbourne wondering what the data-centre boom means for their grid.
- **Success:** someone opens it during a heatwave and understands in a minute how tight the grid is, what's carrying it, and how big the data-centre share is, with clear uncertainty.

## 2. Hard rules

1. **Never claim to measure an individual data centre's live usage.** Data centres are loads inside distribution and transmission networks, and public market data doesn't meter them separately. Every data-centre number is one of three kinds:
   - **Published:** a figure from an AEMO report, a network operator's planning report or a planning approval. Cite it.
   - **Announced:** the operator's stated capacity in megawatts. That's a maximum, not usage.
   - **Estimated:** our own inference, with a range and the method linked.

   The UI shows which kind each number is, every time.
2. **Never blame a specific facility for a specific event** (a price spike, a reserve shortfall). Show coincidences in time; don't assert cause.
3. **Respect data terms.**
   - AEMO's NEMweb data is public.
   - The Open Electricity API data is CC BY-NC 4.0: attribution, non-commercial only. Keep the project non-commercial while using it, or switch to AEMO directly.
   - Bureau of Meteorology data has its own terms, so check them before use.
   - Credit every source on the About tab.
4. **Be a polite client.** One server-side fetch per 5-minute interval, cached and shared. Browsers never call upstream APIs.
5. **No trading or investment signals.** Explain the market; don't advise anyone how to trade it.
6. **Verify before building on any data source** (formats change). Where something can't be verified, leave a TODO rather than a guess.

## 3. Data sources (verify each first)

**Live (5-minute)**
- **AEMO NEMweb** (public files): dispatch and pricing by region; output of every generating unit (SCADA); interconnector flows; rooftop-solar estimates; market notices, including Lack of Reserve (LOR1/2/3) notices, which are AEMO's official "the grid is tight" signal.
- **Open Electricity** (docs.openelectricity.org.au; Python and TypeScript clients; data CC BY-NC 4.0): a cleaner API over much of the same data, plus facility metadata. Good for prototyping; compare with NEMweb before relying on it.
- **Weather:** Bureau of Meteorology observations for capital cities. Temperature drives demand and is essential for the "normal" model.

**Slow (monthly or yearly), for the data-centre layer**
- **AEMO Electricity Statement of Opportunities (ESOO) and its forecasting inputs.**
  - These publish data-centre consumption forecasts. The 2026 ESOO projects growth from about 5 TWh now to about 34 TWh in a decade, concentrated in NSW and Victoria.
  - Extract the regional series and scenarios into versioned data files.
- **Transmission network planning reports** (TransGrid in NSW, AusNet and AEMO Victorian Planning in Victoria, and others): connection enquiries and committed large loads, often with data-centre totals.
- **Planning approvals:**
  - Sources: NSW Planning Portal (State Significant Development) and Victorian planning.
  - Data-centre applications usually state their IT load and grid connection in MW, with location.
- **Operator announcements and press**, for the facility registry (§4), always with a link.

## 4. How it works

### Live console (reuse Downlink Desk patterns)
- Fetch job every 5 minutes; SQLite storage; server-sent live updates to open consoles.
- **Regional tiles:** demand, price, mix (coal, gas, hydro, wind, solar, rooftop solar, battery), interconnectors, and reserve status from LOR notices.
- **Learned normal:** expected demand for each 5-minute slot. It's Downlink Desk's harmonic model with time of day in place of orbit phase, plus temperature, day type (weekday, weekend, holiday) and rooftop solar.
  - Show departures from normal with a 90% range.
  - Explain the likely cause from the data: "demand 9% above normal: 41 °C in Sydney".
- **Alerts:** LOR notices, price spikes, large units tripping (output falling sharply), and demand far from normal. Push to phone via ntfy and include in the morning summary.

### Data-centre layer
- **Facility registry:** a hand-curated, versioned file (`data/datacentres.yaml`).
  - Each facility has: name, operator, location (suburb-level is enough), status (operating / under construction / approved / proposed), stated capacity in MW, connection point if known, and source links.
  - The UI and tests check every entry has a source.
- **Map:** NSW and Victoria with transmission lines and major substations (from public network maps), and facilities sized by MW and coloured by status.
- **Growth estimate (the clever bit):**
  - Data centres draw almost flat power around the clock. After removing temperature, time of day, day type and rooftop solar, the remaining flat baseline of regional demand should grow as data centres connect.
  - Fit it monthly. Report the baseline trend with a range, then compare it against the ESOO data-centre series and the registry's operating MW.
  - Say plainly that other flat loads (industry, electrification) also move this baseline, so it's an upper-bound indicator, not a measurement.
- **Pressure view:**
  - Data centres' estimated and forecast share of regional demand.
  - Announced MW against the region's forecast reliability margin from the ESOO.
  - A timeline of announcements against grid events.
  - Each chart labelled published, announced or estimated.

## 5. Build

- **Stack, matching Downlink Desk:** Python, SQLite, standard-library server, a single-page web UI. Map with MapLibre or Leaflet from a pinned CDN version. Tiles from a provider whose free tier allows public sites.
- **Runs on modest hardware:** a Pi 4, or a small server for the public version. 5-minute data for the NEM is small, but keep retention and storage figures in mind.
- **Repo layout:** `griddesk/` (ingest, model, datacentres, alerts, server), `web/`, `data/` (registry, forecast extracts), `tests/`, `docs/`.

### Milestones
Finish and demo each before the next.

0. **Data spike (one day):** confirm NEMweb file formats, the Open Electricity API access and limits, the LOR notice format, and what the ESOO and network planning reports actually publish about data centres. Write `docs/data-spike.md` with go/no-go notes.
1. **Live console:** regional demand, price, mix and reserve status, updating every 5 minutes, with a static GitHub Pages demo.
2. **Learned normal and alerts:** the demand model with temperature; LOR, price-spike and trip alerts; morning summary.
3. **Data-centre registry and map:** at least the 20 largest NSW and Victorian facilities, each with a source.
4. **Growth estimate and pressure view:** baseline trend against ESOO forecasts and the registry, with ranges and the method page.
5. **Polish and public launch.**

## 6. Quality bar (definition of done for every milestone)

- **Tests:**
  - NEMweb parsing against recorded files.
  - The normal model recovers known patterns from synthetic data.
  - The baseline estimator finds a flat load added to synthetic demand, and reports a range that contains it.
  - Every registry entry has a source.
  - Every data-centre number in the API carries its kind (published, announced or estimated).
- **Fuzzing:** fuzz the parsers with malformed and truncated files; no crashes.
- **Browser check:** a scripted session through every screen with no JavaScript errors or failed requests.
- **Security:**
  - Writes are authenticated and protected against cross-site requests.
  - Input is validated.
  - No secrets in the repo or logs.
  - API keys are kept in a file with 600 permissions.
- **Honesty:**
  - Every number has a unit, a source and a plain explanation.
  - Estimates show ranges.
  - Nothing claims more certainty than the data supports.
- **Stopping rule:** stop iterating when two review rounds in a row find nothing above cosmetic.

## 7. How to work with me

- Start each milestone with a short plan, then build. Ask only when a choice is expensive to undo or genuinely mine.
- Small commits with clear messages. Never force-push. Never commit secrets.
- Keep `docs/decisions.md`: one line per real decision and why.
- End each session with what changed, what's verified, what isn't, and the next step.
