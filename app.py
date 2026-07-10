import os
from datetime import datetime

import requests
from flask import Flask, render_template, request


app = Flask(__name__)

API_KEY = os.getenv("OPENWEATHER_API_KEY")
OPENWEATHER_URL = "https://api.openweathermap.org/data/2.5"


def fetch_openweather(endpoint, params):
    if not API_KEY:
        print("Missing OPENWEATHER_API_KEY environment variable.")
        return None

    try:
        response = requests.get(f"{OPENWEATHER_URL}/{endpoint}", params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as error:
        print(f"OpenWeather request failed: {error}")
        return None


def weather_scene(weather_id, description):
    description = (description or "").lower()

    if 200 <= weather_id < 300 or "thunder" in description:
        return {
            "code": "storm",
            "label": "Thunderstorm conditions",
            "summary": "Electrical storm signals detected. Avoid exposed routes and flooded roads.",
        }
    if 300 <= weather_id < 600 or "rain" in description or "drizzle" in description:
        return {
            "code": "rain",
            "label": "Rainfall conditions",
            "summary": "Rainfall is active or expected. Watch drainage channels and low-lying roads.",
        }
    if 600 <= weather_id < 700 or "snow" in description:
        return {
            "code": "snow",
            "label": "Cold precipitation",
            "summary": "Cold precipitation can reduce visibility and increase travel risk.",
        }
    if 700 <= weather_id < 800 or "mist" in description or "fog" in description or "haze" in description:
        return {
            "code": "mist",
            "label": "Low visibility",
            "summary": "Visibility may be reduced. Flood monitoring remains active.",
        }
    if weather_id == 800 or "clear" in description:
        return {
            "code": "clear",
            "label": "Clear conditions",
            "summary": "Current sky condition is clear. FloodGuard continues monitoring forecast changes.",
        }
    return {
        "code": "clouds",
        "label": "Cloudy conditions",
        "summary": "Cloud cover is present. Forecast rainfall is included in the flood score.",
    }


def classify_risk(score):
    if score >= 70:
        return {
            "level": "CRITICAL",
            "color": "critical",
            "map_color": "#7f1d1d",
            "advice": "Severe flood conditions are possible. Avoid low bridges, move valuables upward, and prepare to evacuate.",
        }
    if score >= 50:
        return {
            "level": "HIGH",
            "color": "high",
            "map_color": "#dc2626",
            "advice": "High flood risk. Stay away from drainage channels and monitor official emergency updates.",
        }
    if score >= 30:
        return {
            "level": "MEDIUM",
            "color": "medium",
            "map_color": "#f59e0b",
            "advice": "Moderate flood risk. Watch rainfall updates and avoid unnecessary travel through low areas.",
        }
    return {
        "level": "LOW",
        "color": "low",
        "map_color": "#16a34a",
        "advice": "No immediate flood signal, but continue monitoring local weather conditions.",
    }


def estimate_environment(city, weather):
    rainfall = weather["rainfall"]
    humidity = weather["humidity"]
    wind = weather["wind"]

    terrain_score = 8 if rainfall >= 20 else 5 if humidity >= 80 else 2
    drainage_score = 8 if rainfall >= 30 else 6 if rainfall >= 10 else 3
    construction_score = 5
    traffic_score = 7 if rainfall >= 20 or wind >= 8 else 4 if rainfall >= 5 else 2

    return {
        "terrain": {
            "label": "Urban and lowland sensitivity",
            "score": terrain_score,
            "status": "Ready for GIS elevation and topography data.",
        },
        "drainage": {
            "label": "Drainage overload estimate",
            "score": drainage_score,
            "status": "Estimated from current rainfall intensity.",
        },
        "construction": {
            "label": "Construction and land-use impact",
            "score": construction_score,
            "status": "Ready for roads, bridges, dams, and construction datasets.",
        },
        "traffic": {
            "label": "Traffic disruption estimate",
            "score": traffic_score,
            "status": "Ready for live traffic API integration.",
        },
        "summary": f"{city} is being evaluated with weather signals now. GIS, demography, construction, and traffic layers are prepared for future data feeds.",
    }


def calculate_flood_score(weather, forecast):
    score = 0
    factors = []

    rainfall = weather["rainfall"]
    humidity = weather["humidity"]
    pressure = weather["pressure"]
    wind_speed = weather["wind"]
    forecast_rain_total = sum(day["rain"] for day in forecast)
    max_forecast_rain = max([day["rain"] for day in forecast], default=0)

    if rainfall >= 50:
        score += 40
        factors.append("Extreme current rainfall")
    elif rainfall >= 20:
        score += 28
        factors.append("Heavy current rainfall")
    elif rainfall >= 5:
        score += 12
        factors.append("Active rainfall")

    if forecast_rain_total >= 80:
        score += 25
        factors.append("Very wet 5-day forecast")
    elif forecast_rain_total >= 35:
        score += 15
        factors.append("Sustained rainfall expected")
    elif max_forecast_rain >= 10:
        score += 8
        factors.append("One or more rainy forecast periods")

    if humidity >= 90:
        score += 15
        factors.append("Very high humidity")
    elif humidity >= 75:
        score += 8
        factors.append("High humidity")

    if pressure <= 995:
        score += 12
        factors.append("Low atmospheric pressure")
    elif pressure <= 1005:
        score += 6
        factors.append("Falling pressure signal")

    if wind_speed >= 12:
        score += 8
        factors.append("Strong wind may worsen storm impact")
    elif wind_speed >= 8:
        score += 4
        factors.append("Moderate wind")

    score = min(score, 100)
    risk = classify_risk(score)

    return {
        "score": score,
        "confidence": min(95, 68 + score // 3),
        "risk": risk["level"],
        "risk_color": risk["color"],
        "map_color": risk["map_color"],
        "advice": risk["advice"],
        "factors": factors or ["No major flood trigger detected from current weather"],
    }


def get_forecast(city):
    data = fetch_openweather(
        "forecast",
        {"q": city, "appid": API_KEY, "units": "metric"},
    )
    if not data:
        return []

    forecast = []
    seen_dates = set()

    for item in data.get("list", []):
        forecast_time = datetime.strptime(item["dt_txt"], "%Y-%m-%d %H:%M:%S")
        date_key = forecast_time.strftime("%Y-%m-%d")

        if date_key in seen_dates:
            continue

        seen_dates.add(date_key)
        rainfall = item.get("rain", {}).get("3h", 0)
        weather_id = item["weather"][0]["id"]
        description = item["weather"][0]["description"].title()
        scene = weather_scene(weather_id, description)

        if rainfall >= 20:
            day_risk = "HIGH RISK"
            day_color = "high-risk"
        elif rainfall >= 5:
            day_risk = "WATCH"
            day_color = "medium-risk"
        else:
            day_risk = "SAFE"
            day_color = "low-risk"

        forecast.append(
            {
                "day": forecast_time.strftime("%A"),
                "date": forecast_time.strftime("%d %b"),
                "time": forecast_time.strftime("%I:%M %p"),
                "temp": round(item["main"]["temp"], 1),
                "rain": rainfall,
                "weather": description,
                "humidity": item["main"]["humidity"],
                "wind": item["wind"]["speed"],
                "risk": day_risk,
                "risk_color": day_color,
                "scene": scene["code"],
            }
        )

        if len(forecast) == 5:
            break

    return forecast


def get_weather(city):
    data = fetch_openweather(
        "weather",
        {"q": city, "appid": API_KEY, "units": "metric"},
    )
    if not data:
        return None

    description = data["weather"][0]["description"].title()
    weather_id = data["weather"][0]["id"]
    rainfall = data.get("rain", {}).get("1h", data.get("rain", {}).get("3h", 0))
    scene = weather_scene(weather_id, description)

    return {
        "city": data["name"],
        "country": data.get("sys", {}).get("country", ""),
        "description": description,
        "weather_id": weather_id,
        "scene": scene,
        "temperature": round(data["main"]["temp"], 1),
        "feels_like": round(data["main"]["feels_like"], 1),
        "humidity": data["main"]["humidity"],
        "pressure": data["main"]["pressure"],
        "wind": data["wind"]["speed"],
        "rainfall": rainfall,
        "latitude": data["coord"]["lat"],
        "longitude": data["coord"]["lon"],
    }


def build_prediction(city):
    weather = get_weather(city)
    if not weather:
        return None, []

    forecast = get_forecast(city)
    flood_model = calculate_flood_score(weather, forecast)
    environment = estimate_environment(weather["city"], weather)

    return {**weather, **flood_model, "environment": environment}, forecast


@app.route("/", methods=["GET", "POST"])
def home():
    prediction = None
    forecast = []
    error = None

    if request.method == "POST":
        city = request.form.get("city", "").strip()
        if not city:
            error = "Please enter a city name."
        elif not API_KEY:
            error = "Weather API key is missing. Add OPENWEATHER_API_KEY to your hosting environment variables."
        else:
            prediction, forecast = build_prediction(city)
            if not prediction:
                error = "City not found or weather service unavailable."

    return render_template("index.html", prediction=prediction, forecast=forecast, error=error)


@app.route("/health")
def health():
    return {"status": "ok", "service": "FloodGuard AI"}


if __name__ == "__main__":
    app.run(debug=True)
