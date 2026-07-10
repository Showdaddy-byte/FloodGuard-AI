# FloodGuard AI

FloodGuard AI is a Flask web app for weather-aware flood early warning. It shows live weather, animated condition scenes, flood risk scoring, a map risk zone, forecast cards, and future-ready layers for topography, demography, construction, drainage, and traffic monitoring.

## Features

- Live weather lookup by city.
- Google-weather-style animated condition panel.
- 5-day forecast with rain risk labels.
- Flood score from rainfall, humidity, pressure, wind, and forecast rain.
- Interactive Leaflet/OpenStreetMap risk map.
- GitHub-safe API key handling through environment variables.
- Ready for live traffic, GIS, topography, demography, and construction data integrations.

## Local Setup

```bash
pip install -r requirements.txt
```

Set your OpenWeather key:

```bash
set OPENWEATHER_API_KEY=your_openweather_key
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

Add this environment variable on your host:

```text
OPENWEATHER_API_KEY=your_openweather_key
```

Use this start command:

```bash
gunicorn app:app
```

Do not commit your real API key to GitHub. Keep it only in your hosting platform environment variables.
