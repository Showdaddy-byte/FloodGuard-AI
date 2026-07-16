# FloodGuard AI

FloodGuard AI is a Flask web app for location-aware flood early warning. Instead of scoring flood risk from rainfall alone, it combines live weather with terrain, water proximity, soil, urbanization, and community-reported ground truth — so two places with the same rainfall (e.g. a low-lying coastal neighborhood vs. an elevated inland one) can land on very different risk levels, the way real forecasting systems (Copernicus EMS, GDACS, Google Flood Hub) work.

## How the flood score is built

```
Rainfall + Forecast Rain + Humidity + Pressure + Wind
        + Elevation + Slope
        + Distance to Water (river/lake/coast)
        + Soil Type (clay content) + Real-Time Soil Moisture
        + River Discharge (GloFAS hydrological model)
        + Urbanization (building density)
        + Tide (optional, coastal)
        + Community-Reported Flood History
        + Live Ground-Truth Reports (override)
                    ↓
            AI Flood Risk Score
                    ↓
        LOW → WATCH → HIGH → SEVERE → CRITICAL
```

## Worldwide live coverage

The homepage now has two independent, always-on monitoring layers, so visitors see real flood risk immediately on load — no search required:

### 1. Live Global Flood Alerts (GDACS)

A direct feed from [GDACS](https://www.gdacs.org) (Global Disaster Alert and Coordination System) — the same system used by UN OCHA and humanitarian agencies to track active flood events worldwide. This is genuine, real-time, anywhere-on-Earth coverage, because running FloodGuard AI's own terrain model against every location on the planet isn't possible with free, rate-limited REST APIs — real global coverage has to come from a source built for exactly that.

- Needs **no API key** — works even before `OPENWEATHER_API_KEY` is configured.
- Refreshes automatically every `GLOBAL_ALERTS_REFRESH_MINUTES` (default 10) on page load.
- `GET /api/global-alerts` — current cached GDACS alerts, polled live every 60 seconds.
- `POST /api/refresh-global-alerts` — for an external cron scheduler, same pattern as the watchlist refresh below.

### 2. Monitored Locations (FloodGuard AI's own model)

The homepage also watches a curated list continuously with the full terrain-aware model (elevation, slope, water proximity, soil, urbanization, coastal thresholds) — this is deeper analysis than GDACS provides, but limited to the locations in the list plus anywhere visitors search.

- **Edit the static watchlist**: change `MONITORED_LOCATIONS` near the top of `app.py`. It ships with major flood-vulnerable cities across Africa, Asia, Europe, North America, South America, and Oceania, plus the original Lagos neighborhoods.
- **Auto-expanding**: any location a visitor searches is automatically added to permanent monitoring afterward — and its cache entry updates immediately at search time, not just on the next scheduled sweep. Checking a place once means it stays watched going forward.
- **Best-effort auto refresh**: every homepage load checks if the cached data is older than `WATCHLIST_REFRESH_MINUTES` (default 15) and, if so, kicks off a non-blocking background refresh.
- **True always-fresh (recommended for production)**: set up a free external scheduler to hit `POST /api/refresh-watchlist` every 10-15 minutes even with zero traffic — for example a scheduled GitHub Actions workflow or a free cron-job.org account.
- **Staleness is shown, never hidden**: if the cache hasn't refreshed in over 2x the refresh interval, the banner says so explicitly instead of confidently claiming "no active alerts." Real cached alerts still display (with an "this data may be outdated" note attached) rather than being replaced by a generic message.
- `GET /api/watchlist-status` — current cached alert state, polled live every 45 seconds.

**A note on scale**: only weather, tide, and river discharge are re-fetched on every refresh — elevation, slope, water proximity, soil type, and building density are cached per location for `GEO_CONTEXT_TTL_HOURS` (default 24h), since terrain doesn't meaningfully change hour to hour. This is what keeps a multi-location watchlist from re-hammering Overpass and SoilGrids on every 15-minute sweep; those two are public, rate-limited services, and repeatedly re-querying static data was the actual cause of `406`/timeout errors seen in production logs. The first sweep after a fresh deploy (or after adding a new location) still needs to populate that cache, so it's staggered with a 1.5s delay between locations rather than firing all requests at once.

**If you see `/health` and `openweather_configured`/`tide_configured`/`mapbox_configured` don't match what you expect**: environment variables are read once when the app process starts. Adding or changing one in Render's dashboard usually triggers an automatic redeploy, but if it doesn't, trigger one manually (Manual Deploy → Deploy latest commit) — the running process won't pick up the change otherwise. `GET /health` reports which optional integrations are actually live without exposing the key values themselves, so it's the fastest way to confirm a token actually took effect.

**Cross-process sweep locking**: a rolling deploy briefly running two instances, or multiple worker processes each handling an early request, could otherwise each kick off their own full monitoring sweep at the same moment — combined, that's enough simultaneous requests to trip Overpass/Open-Meteo's rate limits even with per-worker staggering, since an in-memory "already refreshing" flag only guards within a single process. Sweeps now take a database-backed lock (`refresh_locks` table) that's visible across all worker processes, so only one sweep runs at a time no matter how many workers or overlapping deploy instances are running. A stale lock (e.g. from a worker that crashed mid-sweep) is automatically reclaimed after 60 minutes.

## Coastal-adjusted alert thresholds

Coastal regions flood at rainfall levels that wouldn't trouble inland terrain — storm surge, tidal backflow, and lagoon/estuary effects mean the same numeric score should read as more urgent near a coastline. A location is detected as coastal (anywhere in the world, via live OpenStreetMap coastline data — not a hardcoded list) when it's within `COASTAL_ZONE_KM` (default 10 km) of an ocean/sea coastline.

For coastal locations, every risk threshold shifts down by 20 points:

| Score | Inland | Coastal |
|---|---|---|
| 0-4 | LOW | — |
| 5-24 | LOW / WATCH | WATCH |
| 25-44 | WATCH | **HIGH** |
| 45-64 | HIGH | SEVERE |
| 65-84 | SEVERE | CRITICAL |
| 85-100 | CRITICAL | CRITICAL |

This applies identically to the 5-day forecast: each forecast day is scored with the same terrain/slope/water-proximity/soil/urbanization/coastal model as "right now," not rainfall alone — so a coastal, low-lying location can show HIGH or CRITICAL days ahead of time even when the forecast rainfall number alone looks unremarkable.

## Map features

The location map (shown after a search) now includes:

- **Flood-prone risk zone** — a shaded circle sized by the flood score, labeled with a tooltip.
- **Nearest water body marker** — the actual river/lake/coastline point closest to the searched location (from the same Overpass lookup used for scoring), connected with a dashed line and labeled with the real distance. This is what visually shows *why* a location is flood-prone, not just a risk color.
- **Community-reported flooding marker** — appears when visitors have submitted "flooding observed" reports for that location. Its position is an approximation near the location center; this app doesn't collect precise per-report coordinates, so the marker is intentionally not presented as a pinpoint address.
- **Live traffic layer (optional)** — a real, live traffic tile overlay (Mapbox Traffic), toggleable via the layer control in the map's top-right corner. Requires a free `MAPBOX_ACCESS_TOKEN` (see `.env.example`); without one, the map shows a note instead of silently omitting the feature.
- **Legend** — bottom-right control labeling every marker/color so the map is self-explanatory for visitors, not just for whoever built it.

**Honest scope note on "flood-induced traffic":** no free API reports traffic congestion *caused by* flooding specifically — that causal link doesn't exist as a queryable data feed anywhere, paid or free. What this app shows is a real, live, general traffic layer that a visitor can visually cross-reference against the flood risk zone and community reports next to it, not a certified "this jam was caused by this flood" signal.

## Data sources used (all live, free)

| Factor | Source | Notes |
|---|---|---|
| Rainfall + forecast | OpenWeatherMap | Requires a free API key |
| Location geocoding | OpenWeatherMap Geocoding | Resolves neighborhoods, not just cities |
| Elevation | Open-Meteo Elevation API | No key required |
| Slope | Derived from a 5-point elevation grid (~300 m N/S/E/W) | No key required |
| Distance to river/lake/coast (+ name) | OpenStreetMap (Overpass API) | No key required; coverage depends on OSM data density |
| Building density (urbanization proxy) | OpenStreetMap (Overpass API) | Counts buildings within 600 m |
| Soil type (clay content) | ISRIC SoilGrids v2.0 | No key required |
| **River discharge (real hydrological model)** | **Open-Meteo Flood API — GloFAS** | No key required; only returns data on a modeled river reach |
| **Soil moisture (real-time saturation)** | **Open-Meteo Forecast API (ERA5-based)** | No key required |
| Tide level + next high/low tide | WorldTides | **Optional** — only runs if `TIDE_API_KEY` is set |
| Live traffic layer | Mapbox Traffic tiles | **Optional** — only shown if `MAPBOX_ACCESS_TOKEN` is set |
| Global flood alerts | GDACS (UN OCHA feed) | No key required; independent of location search |
| **Satellite flood extent** | **Google Earth Engine: Sentinel-1 GRD** | Optional Earth Engine integration; compares recent radar backscatter with a prior baseline |
| **Surface-water index** | **Google Earth Engine: Sentinel-2 SR Harmonized** | Optional Earth Engine integration; computes NDWI/NDVI where cloud-free imagery is available |
| **Raster elevation/slope** | **Google Earth Engine: Copernicus DEM GLO-30** | Optional fallback/augmentation for terrain and slope |
| **Historical surface water** | **Google Earth Engine: JRC Global Surface Water** | Optional recurring-water and seasonality signal |
| **Satellite rainfall** | **Google Earth Engine: CHIRPS Daily** | Optional 7-day and 30-day gridded rainfall totals |
| **Land cover** | **Google Earth Engine: Dynamic World V1** | Optional land-cover signal for built area, water, and flooded vegetation |
| Historical flood frequency | This app's own community reports table | A proxy, not a certified archive (see limitations) |
| Live ground truth | This app's own community reports table | Recent "flooding observed" reports can override the model's verdict |

Every external lookup fails independently and gracefully — if Overpass or SoilGrids is briefly down, that one factor is just marked "unavailable" and the rest of the score still comes through.

## Hydrological modeling

Two factors now come from real hydrology rather than weather alone:

- **River discharge (GloFAS)** — the Global Flood Awareness System is the same Copernicus/ECMWF hydrological model used by professional flood forecasters. It routes rainfall through upstream catchments and river networks to model actual discharge (m3/s), which this app compares against the 30-year historical average for that day of year. A river running at 3x its normal flow scores very differently from one at a normal level, even under identical local rainfall — this is genuine upstream catchment behavior, not something inferred from today's weather at a single point. Not every coordinate sits on a modeled river reach; a clean "no data" is an expected, normal result at many points, not a failure.
- **Soil moisture (real-time saturation)** — distinct from the static soil-type/clay-content factor above. This measures how saturated the ground actually is right now. Already-saturated soil can't absorb more rain regardless of its clay content, so this catches a risk factor static soil type alone would miss.

**A note on what "hydrological modeling" does and doesn't mean here**: this app *consumes* a real hydrological model (GloFAS) rather than running its own rainfall-runoff simulation, watershed delineation, or flow routing. Building an independent hydrological model from scratch would require raster DEM processing, catchment delineation, and calibration against historical discharge — infrastructure well beyond a REST-call-based Flask app. Plugging into GloFAS is the honest way to get real hydrological science into the score without overstating what's computed in-house.

**A testing caveat, in the interest of transparency**: the sandbox this app was built in has restricted network egress, so the GloFAS and soil moisture integrations could not be verified against live responses during development — they were built carefully against Open-Meteo's documented, stable API schema, with defensive error handling so a schema mismatch fails safely (shows "data unavailable," never crashes or shows wrong data). Worth a quick check against real output once deployed.

## Tide integration (WorldTides)

A full WorldTides integration has four parts, and an earlier version of this app only completed the first two:

1. `TIDE_API_KEY` in environment variables.
2. `requests.get(...)` to the WorldTides API.
3. **Processing the JSON response** — extracting current height *and* the next high/low tide events (`extremes`), not just a single number.
4. **Displaying those values** — both folded into the score/factors, and as a dedicated "🌊 Tide Forecast" box on the result page showing the next high and low tide with times, whenever that data is available.

All four are implemented now. When `TIDE_API_KEY` isn't set, the tide card explicitly says "Tide monitoring not configured" rather than silently disappearing from the page — every other factor in this app behaves this way, and tide should too.

## Known limitations (please read before relying on this for safety decisions)

- **Geocoding accuracy**: works well for well-known neighborhoods (e.g. "Lekki, Lagos") but may not resolve every informal or unofficial place name. If a search fails, try the nearest larger, more commonly mapped area name.
- **Weather forecasting has real limits.** This app uses the same free public forecast data available to anyone — it cannot guarantee catching a genuinely unprecedented, highly localized convective storm before it happens. What it can do, and now does: read the *same* forecast number very differently depending on terrain, coastline proximity, and historical pattern, so a modest-looking forecast rainfall figure for a vulnerable coastal spot triggers a real warning instead of being read the same as it would be for solid inland terrain.
- **GDACS covers significant/large-scale flood events, not hyperlocal ones.** It's a real, live, worldwide feed — but it's built to track disaster-scale flooding, so a flash flood in one neighborhood that hasn't been classified as a GDACS "event" yet won't appear there. It's a complement to FloodGuard AI's own per-location model, not a replacement for it — this is exactly why both layers run independently on the homepage.
- **Historical flood frequency (for a specific searched location) is a proxy, not an archive.** True historical datasets (Dartmouth Flood Observatory, EM-DAT, GDACS's own historical archive) are static files that need to be downloaded and hosted, not queried live per-coordinate — that's real infrastructure work beyond what a REST-call-based app can do out of the box. Until that's built, the per-location "historical" layer reflects only what visitors have reported through this app, which starts at zero for every new location and grows over time.
- **Partly included via Earth Engine when configured**: satellite-based flood detection, gridded rainfall, land cover, historical surface water, and raster terrain. Direct country-level river gauge telemetry, population/building-density rasters (WorldPop/GHSL), and true digital-twin/ML prediction trained on historical outcomes remain future work.
- **Drainage quality** has no free automated global data source, so it's estimated from rainfall intensity and nudged by community reports rather than measured directly.
- **This is a decision-support tool, not an emergency alert system.** Always follow official emergency services and local authority guidance over any single app.

## Community Data

Visitor contributions are stored locally in a SQLite file, `community.db`, created automatically on first run. It's excluded from git via `.gitignore`.

- `POST /api/contribute` — submit `{city, category, rating, comment}` as JSON.
- `GET /api/contributions/<city>` — fetch live stats and recent reports for a city.
- Recent (last 12h) high-severity "flooding observed" reports override the model's risk level to at least HIGH, with a visible alert banner.
- All-time flooding reports for a location feed the "community-reported flood history" factor.

Note: on hosts with an ephemeral filesystem (e.g. some free tiers on Heroku/Render), `community.db` resets on redeploy. For persistent community data in production, swap `DB_PATH` for a hosted database (Postgres, etc.) or a mounted volume.

## Local Setup

```bash
pip install -r requirements.txt
```

Set your OpenWeather key:

```bash
set OPENWEATHER_API_KEY=your_openweather_key
```

Optionally, enable tidal scoring for coastal locations:

```bash
set TIDE_API_KEY=your_worldtides_key
```

Optionally, enable Google Earth Engine satellite/raster scoring. The app
defaults to `credentials/floodguard-ai-502609-81e725f17c81.json` when that
file exists, or you can point to another service-account JSON key:

```bash
set EARTH_ENGINE_ENABLED=1
set GEE_PRIVATE_KEY_PATH=credentials/floodguard-ai-502609-81e725f17c81.json
set GEE_PROJECT=your_google_cloud_project_id
```

The service account must be registered for Earth Engine and have access to
the Google Cloud project used for Earth Engine requests. If Earth Engine is
missing or authentication fails, FloodGuard AI continues using OpenWeather,
Open-Meteo, Overpass, SoilGrids, GDACS, and community reports.

Run the app:

```bash
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## Live Deployment

Add these environment variables on your host:

```text
OPENWEATHER_API_KEY=your_openweather_key
TIDE_API_KEY=your_worldtides_key   # optional
```

Use this start command:

```bash
gunicorn app:app
```

Do not commit your real API keys to GitHub. Keep them only in your hosting platform environment variables.

For true always-fresh monitoring with zero visitor traffic, set up a free external scheduler (GitHub Actions cron, cron-job.org, etc.) to call both:

```text
POST https://your-app.onrender.com/api/refresh-global-alerts   # every 10 min, no key needed
POST https://your-app.onrender.com/api/refresh-watchlist        # every 10-15 min, needs OPENWEATHER_API_KEY set
```

## Roadmap

**Phase 1 (done):** Weather — rainfall, forecast, humidity, pressure, wind.

**Phase 2 (done, this release):** Elevation, slope, water proximity, soil type, urbanization, optional tide, community-reported historical frequency, live ground-truth override.

**Phase 3 (not yet built — needs real infrastructure, not just an API call):** Live river gauge integration, satellite-based flood detection, soil moisture, population/building-density rasters, hosted historical flood archives, and machine-learning prediction trained on historical outcomes.
