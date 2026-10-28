# FloodGuard AI - worldwide street-level risk + Telegram alerts (deploy notes)

## Files (same relative paths as your repo)
| Path | Status |
|---|---|
| app.py | EDITED (full file, CRLF like yours) |
| templates/index.html | EDITED (full file) |
| street_risk.py | NEW |
| telegram_alerts.py | NEW |
| official_sources.py | NEW |
| templates/admin_telegram.html | NEW (admin page at /admin/telegram) |
| tests/test_street_and_telegram.py | NEW (69 offline tests) |

Not changed: rainfall_engine.py, Procfile, .gitignore, run_server.ps1, community.db (new tables are created automatically; verified against a copy of your database).

## Dependencies
No new pip packages are required. Python 3.9+ (uses dict union and zoneinfo).
`tzdata` is already added to the included requirements.txt so local time zones work on Render's slim image
(without it times fall back to a fixed UTC offset, then UTC - never a wrong zone).

## Environment variables (Render -> Service -> Environment)
Required for the new features:
- GEOAPIFY_API_KEY - you already use it; global place search needs it.
- TELEGRAM_BOT_TOKEN - from @BotFather. Secret. Never commit it.
- TELEGRAM_WEBHOOK_SECRET - any long random string (e.g. `python -c "import secrets;print(secrets.token_urlsafe(32))"`).
- TELEGRAM_BOT_USERNAME - bot username without @ (optional; looked up via getMe if empty).
- TELEGRAM_MODE - `test` (default) or `live`. In test mode nothing is sent to subscribers; only admin chats receive "would notify N" messages.
- TELEGRAM_ADMIN_CHAT_IDS - comma-separated numeric chat ids (send /start to @userinfobot to find yours).
- SITE_BASE_URL - https://myfloodguard.online (used for the webhook URL and links).
Already needed by your app (check they are set): SECRET_KEY, ADMIN_PASSWORD_HASH, CRON_SECRET, OPENWEATHER_API_KEY.
Optional tuning (defaults in brackets):
- FLOOD_ALERT_THRESHOLD [30] - 0-100 FloodGuard score, NOT a probability.
- FLOOD_ALERT_THRESHOLD_BY_COUNTRY [empty] - e.g. `GB=35,BD=25`.
- TELEGRAM_COOLDOWN_HOURS [6], TELEGRAM_ESCALATION_DELTA [15], TELEGRAM_URGENT_MIN_GAP_MINUTES [30],
  TELEGRAM_MAX_DATA_AGE_MINUTES [90], TELEGRAM_CLEAR_MARGIN [5], TELEGRAM_MAX_LOCATIONS [5], TELEGRAM_MAX_EXTRA_AREAS.
- STREET_CACHE_MINUTES [180], STREET_SWEEP_MAX_PER_RUN [6].

## Database
No manual migration. Tables are created automatically on startup (`CREATE TABLE IF NOT EXISTS`):
street_risk_cache, telegram_subscribers, telegram_locations, telegram_pending, telegram_alert_log, telegram_updates.
Existing tables are untouched. Use a Render persistent disk for community.db or the data resets on redeploy.

## Deploy (GitHub + Render)
1. Copy the files above into your repo at the same paths (replace app.py and templates/index.html).
2. Run tests locally: `python -m unittest discover -s tests -v`.
3. Commit and push; Render redeploys.
4. Set the env vars above (start with TELEGRAM_MODE=test). Redeploy if Render asks.
5. Log in at /admin, open /admin/telegram, click "Set webhook", then "Webhook info" (should show your URL, no error).
6. In Telegram open your bot, send /start. Register a place from the website form or `/add Yaba, Lagos, Nigeria`.
7. In /admin/telegram use "Simulate" (no sending) then "Send test alert" to one admin chat.
8. Make sure your scheduler calls /api/refresh-watchlist and /api/refresh-street-risk with your CRON token (same mechanism you use now).
9. When satisfied, set TELEGRAM_MODE=live.

## Testing checklist
- [ ] Home page loads; existing sections (dashboard, routes, email alerts, map) still work.
- [ ] Search "Lisbon", then "Lekki": the map and list change to each place; no results from the earlier search remain.
- [ ] Search "Paris": you are asked which country; each choice loads only that place.
- [ ] Search a remote place with few mapped roads: you see "No street-level prediction" plus the area-level score, map centred on the place.
- [ ] Cards show country/region/city/neighbourhood, score + severity, factors, source, assessed time in the area's time zone, evidence label and "What to do".
- [ ] Telegram form: choose a country, city, neighbourhood; the link opens the bot and /list shows the place.
- [ ] /admin/telegram: webhook info OK; simulate shows matched locations; test alert arrives only on admin chat.
- [ ] Test mode: no subscriber receives anything; log shows `test_mode_skip`.
- [ ] Same alert is not sent twice within the cooldown; a rise of 15+ points sends one "urgent" update.
- [ ] /stop deletes your subscription; /pause and /resume work.

## Coverage, licensing, limits (be honest with users)
Data per area (shown in "Data used for this area" on each card):
- Rainfall/forecast: OpenWeather (free tier limits apply). Satellite rainfall: NASA GPM IMERG via Earth Engine (~60N-60S).
- Terrain: NASADEM (30 m, 60N-56S) via Earth Engine; elsewhere Copernicus DEM (90 m) via Open-Meteo.
- Roads/drains: OpenStreetMap via Overpass (ODbL; credit "(c) OpenStreetMap contributors"). Coverage and naming quality vary a lot by country; where fewer than a handful of named streets are mapped the site says so and shows no street predictions.
- River discharge: GloFAS via Open-Meteo (modelled, not gauges). Tide: WorldTides where configured.
- Official alerts: GDACS worldwide (events within ~150 km); US National Weather Service (public domain); England Environment Agency (Open Government Licence v3). Every other country: "no national feed connected" - add one function to official_sources.ADAPTERS per agency. Official alerts are area-level and never change a street score.
- The NWS and Environment Agency adapters follow the agencies' published formats but could NOT be tested against the live services from my build environment. They fail closed (show "unavailable", never "all clear"). Verify with a real US/England place after deploy.
- No source here can confirm that a particular street is flooded. "Confirmed flooding" is never shown. Visitor reports are labelled unverified.
- The 0-100 score is not a calibrated probability. Thresholds are configurable (global and per country).
Rate limits / costs (free tiers; check current terms): Geoapify free ~3,000 requests/day (each search or registration uses 1-2); OpenWeather free tier per-minute/day caps; Overpass is a shared public service (fair use - the app caches 3 h, limits 2 concurrent jobs and per-IP rate); Open-Meteo free non-commercial limits (check commercial terms if the site earns revenue); Telegram Bot API is free (~30 messages/second limit); Render free web service sleeps and its disk is ephemeral. Global scale is bounded by the OpenWeather/Geoapify quotas: the sweep only covers curated places plus subscriber places (cap TELEGRAM_MAX_EXTRA_AREAS).
Other limits: messages are English only; time zone comes from Geoapify; emergency numbers come from the app's own table (a generic "follow local authorities" line is used where none is known).