# FloodGuard AI

FloodGuard AI is a Flask web app for location-aware flood early warning. Instead of scoring flood risk from rainfall alone, it combines live weather with terrain, water proximity, soil, urbanization, and community-reported ground truth — so two places with the same rainfall (e.g. a low-lying coastal neighborhood vs. an elevated inland one) can land on very different risk levels, the way real forecasting systems (Copernicus EMS, GDACS, Google Flood Hub) work.

## How the flood score is built

```
Rainfall + Forecast Rain + Humidity + Pressure + Wind
        + Elevation + Slope
        + Distance to Water (river/lake/coast)
        + Soil Type (clay content)
        + Urbanization (building density)
        + Tide (optional, coastal)
        + Community-Reported Flood History
        + Live Ground-Truth Reports (override)
                    ↓
            AI Flood Risk Score
                    ↓
        LOW → WATCH → HIGH → SEVERE → CRITICAL
```

## Data sources used (all live, free)

| Factor | Source | Notes |
|---|---|---|
| Rainfall + forecast | OpenWeatherMap | Requires a free API key |
| Location geocoding | OpenWeatherMap Geocoding | Resolves neighborhoods, not just cities |
| Elevation | Open-Meteo Elevation API | No key required |
| Slope | Derived from a 5-point elevation grid (~300 m N/S/E/W) | No key required |
| Distance to river/lake/coast | OpenStreetMap (Overpass API) | No key required; coverage depends on OSM data density |
| Building density (urbanization proxy) | OpenStreetMap (Overpass API) | Counts buildings within 600 m |
| Soil type (clay content) | ISRIC SoilGrids v2.0 | No key required |
| Tide level | WorldTides | **Optional** — only runs if `TIDE_API_KEY` is set |
| Historical flood frequency | This app's own community reports table | A proxy, not a certified archive (see limitations) |
| Live ground truth | This app's own community reports table | Recent "flooding observed" reports can override the model's verdict |

Every external lookup fails independently and gracefully — if Overpass or SoilGrids is briefly down, that one factor is just marked "unavailable" and the rest of the score still comes through.

## Known limitations (please read before relying on this for safety decisions)

- **Geocoding accuracy**: works well for well-known neighborhoods (e.g. "Lekki, Lagos") but may not resolve every informal or unofficial place name. If a search fails, try the nearest larger, more commonly mapped area name.
- **Historical flood frequency is a proxy, not an archive.** True historical datasets (GDACS, Dartmouth Flood Observatory, EM-DAT) are static files that need to be downloaded and hosted, not queried live per-coordinate — that's real infrastructure work beyond what a REST-call-based app can do out of the box. Until that's built, the "historical" layer reflects only what visitors have reported through this app, which starts at zero for every new location and grows over time.
- **Not yet included** (genuinely needs heavier infrastructure than a live REST call can provide): live river gauge levels, satellite-based flood detection, soil moisture, population/building-density rasters (WorldPop/GHSL), and true digital-twin/ML prediction. These remain a Phase 3 roadmap, not a Phase 2 claim.
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

## Roadmap

**Phase 1 (done):** Weather — rainfall, forecast, humidity, pressure, wind.

**Phase 2 (done, this release):** Elevation, slope, water proximity, soil type, urbanization, optional tide, community-reported historical frequency, live ground-truth override.

**Phase 3 (not yet built — needs real infrastructure, not just an API call):** Live river gauge integration, satellite-based flood detection, soil moisture, population/building-density rasters, hosted historical flood archives, and machine-learning prediction trained on historical outcomes.

