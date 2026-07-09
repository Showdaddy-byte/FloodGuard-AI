# FloodGuard AI

FloodGuard AI is a Flask-based flood early warning web app. It combines live weather data, rainfall forecasts, an interactive map, and a first-pass flood risk scoring model.

## Current Features

- Search any city and fetch live weather data.
- Analyze rainfall, humidity, pressure, wind, and forecast patterns.
- Produce a flood risk level: LOW, MEDIUM, HIGH, or CRITICAL.
- Show risk confidence and a 0-100 flood score.
- Display a Leaflet/OpenStreetMap location map with an estimated risk zone.
- Show 5-day forecast cards with daily rain-risk labels.
- Include placeholders for topography, construction, demography, drainage, and live traffic layers.

## Project Structure

```text
app.py
requirements.txt
Procfile
.env.example
templates/
  index.html
static/
  css/
    style.css
```

## Run Locally

```bash
pip install -r requirements.txt
python app.py
```

Then open:

```text
http://127.0.0.1:5000
```

## API Key

The app reads the OpenWeather key from:

```text
OPENWEATHER_API_KEY
```

For development, you can set it as an environment variable. The current code still includes your uploaded key as a fallback, but for production you should remove that fallback and keep the key private.

## Next Development Steps

1. Add a GIS elevation/topography API or local DEM dataset.
2. Add demography and vulnerable-population layers.
3. Add construction and drainage infrastructure datasets.
4. Add a live traffic provider for blocked roads and evacuation guidance.
5. Add user accounts, saved locations, SMS/email/push alerts, and admin monitoring.
6. Store searches and alert history in a database instead of browser local storage.
