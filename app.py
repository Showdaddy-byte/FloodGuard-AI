from flask import Flask, render_template, request
import requests
from datetime import datetime

app = Flask(__name__)

API_KEY = "e443cad40ef88cadb782fc7464da644e"

def get_forecast(city):
    url = f"https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={API_KEY}&units=metric"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return []

        data = response.json()
        forecast = []
        seen_dates = set()

        for item in data["list"]:
            forecast_time = datetime.strptime(item["dt_txt"], "%Y-%m-%d %H:%M:%S")
            date_key = forecast_time.strftime("%Y-%m-%d")

            if date_key in seen_dates:
                continue

            seen_dates.add(date_key)

            rainfall = item.get("rain", {}).get("3h", 0)

            forecast.append({
                "day": forecast_time.strftime("%A"),
                "date": forecast_time.strftime("%d %b"),
                "time": forecast_time.strftime("%I:%M %p"),
                "temp": round(item["main"]["temp"], 1),
                "rain": rainfall,
                "weather": item["weather"][0]["description"].title(),
                "humidity": item["main"]["humidity"],
                "wind": item["wind"]["speed"]
            })

            if len(forecast) == 5:
                break

        return forecast

    except Exception as e:
        print("Forecast Error:", e)
        return []

def get_weather(city):
    url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={API_KEY}&units=metric"

    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return None

        data = response.json()

        rainfall = data.get("rain", {}).get("1h", 0)
        humidity = data["main"]["humidity"]
        pressure = data["main"]["pressure"]
        temperature = data["main"]["temp"]
        feels_like = data["main"]["feels_like"]
        wind_speed = data["wind"]["speed"]

        score = 0

        if rainfall >= 50:
            score += 40
        elif rainfall >= 20:
            score += 25
        elif rainfall >= 5:
            score += 10

        if humidity >= 90:
            score += 20
        elif humidity >= 80:
            score += 15
        elif humidity >= 70:
            score += 10

        if pressure <= 995:
            score += 20
        elif pressure <= 1005:
            score += 10

        if wind_speed >= 12:
            score += 10
        elif wind_speed >= 8:
            score += 5

        if temperature < 22:
            score += 5

        if score >= 60:
            risk = "HIGH"
            risk_color = "red"
            advice = "High flood risk. Prepare to evacuate if necessary."
        elif score >= 35:
            risk = "MEDIUM"
            risk_color = "orange"
            advice = "Moderate flood risk. Monitor forecasts closely."
        else:
            risk = "LOW"
            risk_color = "green"
            advice = "No immediate flood risk."

        return {
            "city": data["name"],
            "description": data["weather"][0]["description"].title(),
            "temperature": round(temperature, 1),
            "feels_like": round(feels_like, 1),
            "humidity": humidity,
            "pressure": pressure,
            "wind": wind_speed,
            "rainfall": rainfall,
            "latitude": data["coord"]["lat"],
            "longitude": data["coord"]["lon"],
            "risk": risk,
            "risk_color": risk_color,
            "score": score,
            "advice": advice
        }

    except Exception as e:
        print("Weather Error:", e)
        return None

@app.route("/", methods=["GET", "POST"])
def home():
    prediction = None
    forecast = []
    error = None

    if request.method == "POST":
        city = request.form.get("city", "").strip()
        if city:
            prediction = get_weather(city)
            if prediction:
                forecast = get_forecast(city)
            else:
                error = "City not found or weather service unavailable."

    return render_template(
        "index.html",
        prediction=prediction,
        forecast=forecast,
        error=error
    )

if __name__ == "__main__":
    app.run(debug=True)
