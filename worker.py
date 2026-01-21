#!/usr/bin/env python3
"""
Background worker for sending daily weather-based clothing recommendations.
Fetches tomorrow's weather forecast for Pirkkala from FMI and sends SMS via Twilio.
Designed to run on Render.com as a background worker.
"""

import os
import time
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import google.generativeai as genai
from twilio.rest import Client as TwilioClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,e
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Environment variables
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
MY_PHONE_NUMBER = os.getenv("MY_PHONE_NUMBER")

# Constants
LOCATION = "Pirkkala"
FMI_API_URL = "https://opendata.fmi.fi/wfs"
TIMEZONE = ZoneInfo("Europe/Helsinki")

# Notification times
MORNING_SEND_HOUR = 9   # Lähetysaika: klo 9:00
EVENING_SEND_HOUR = 20  # Lähetysaika: klo 20:00

# Weather forecast target times
AFTERNOON_TARGET_HOUR = 16  # Töistä lähtö klo 16:00 (haetaan aamulla)
MORNING_TARGET_HOUR = 8     # Töihin meno klo 8:00 (haetaan illalla)

CHECK_INTERVAL_SECONDS = 300  # Tarkista 5 min välein


def validate_env_vars() -> bool:
    """Validate that all required environment variables are set."""
    required_vars = [
        ("GEMINI_API_KEY", GEMINI_API_KEY),
        ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
        ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
        ("TWILIO_FROM_NUMBER", TWILIO_FROM_NUMBER),
        ("MY_PHONE_NUMBER", MY_PHONE_NUMBER),
    ]
    
    missing = [name for name, value in required_vars if not value]
    
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        return False
    return True


def fetch_weather_forecast(days_ahead: int = 0, target_hour: int = 16) -> dict | None:
    """Fetch weather forecast for Pirkkala from FMI.
    
    Args:
        days_ahead: 0 for today, 1 for tomorrow
        target_hour: Hour of day to get forecast for (0-23)
    """
    try:
        # Calculate target day at target hour
        now = datetime.now(TIMEZONE)
        target_day = now + timedelta(days=days_ahead)
        target_time = target_day.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        
        # FMI API parameters
        start_time = target_time - timedelta(hours=1)
        end_time = target_time + timedelta(hours=1)
        
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "getFeature",
            "storedquery_id": "fmi::forecast::harmonie::surface::point::simple",
            "place": LOCATION,
            "starttime": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endtime": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "parameters": "Temperature,WindSpeedMS,WindDirection,Precipitation1h,TotalCloudCover,WeatherSymbol3",
        }
        
        logger.info(f"Fetching weather for {LOCATION} at {target_time.isoformat()}")
        
        response = requests.get(FMI_API_URL, params=params, timeout=30)
        response.raise_for_status()
        
        # Parse XML response
        root = ET.fromstring(response.content)
        
        # FMI uses namespaces
        ns = {
            "wfs": "http://www.opengis.net/wfs/2.0",
            "BsWfs": "http://xml.fmi.fi/schema/wfs/2.0",
            "gml": "http://www.opengis.net/gml/3.2",
        }
        
        weather_data = {
            "temperature": None,
            "wind_speed": None,
            "wind_direction": None,
            "precipitation": None,
            "cloud_cover": None,
            "time": target_time.strftime("%Y-%m-%d %H:%M"),
            "location": LOCATION,
        }
        
        # Extract values from XML
        for member in root.findall(".//BsWfs:BsWfsElement", ns):
            param_name = member.find("BsWfs:ParameterName", ns)
            param_value = member.find("BsWfs:ParameterValue", ns)
            
            if param_name is not None and param_value is not None:
                name = param_name.text
                try:
                    value = float(param_value.text) if param_value.text and param_value.text != "NaN" else None
                except ValueError:
                    value = None
                
                if name == "Temperature":
                    weather_data["temperature"] = value
                elif name == "WindSpeedMS":
                    weather_data["wind_speed"] = value
                elif name == "WindDirection":
                    weather_data["wind_direction"] = value
                elif name == "Precipitation1h":
                    weather_data["precipitation"] = value
                elif name == "TotalCloudCover":
                    weather_data["cloud_cover"] = value
        
        logger.info(f"Weather data: {weather_data}")
        return weather_data
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch weather from FMI: {e}")
        return None
    except ET.ParseError as e:
        logger.error(f"Failed to parse FMI response: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching weather: {e}")
        return None


def get_wind_direction_text(degrees: float | None) -> str:
    """Convert wind direction degrees to Finnish text."""
    if degrees is None:
        return "tuntematon"
    
    directions = [
        (0, "pohjoisesta"), (45, "koillisesta"), (90, "idästä"),
        (135, "kaakosta"), (180, "etelästä"), (225, "lounaasta"),
        (270, "lännestä"), (315, "luoteesta"), (360, "pohjoisesta")
    ]
    
    for i, (deg, name) in enumerate(directions[:-1]):
        next_deg = directions[i + 1][0]
        if deg <= degrees < next_deg:
            # Return closer direction
            if degrees - deg < next_deg - degrees:
                return name
            return directions[i + 1][1]
    return "pohjoisesta"


def generate_clothing_recommendation(gemini_model: genai.GenerativeModel, weather: dict) -> str | None:
    """Use Gemini to generate clothing recommendation based on weather."""
    try:
        temp = weather.get("temperature")
        wind = weather.get("wind_speed")
        wind_dir = get_wind_direction_text(weather.get("wind_direction"))
        precip = weather.get("precipitation", 0) or 0
        
        weather_desc = f"""
Sää Pirkkalassa:
- Lämpötila: {temp:.1f}°C
- Tuuli: {wind:.1f} m/s {wind_dir}
- Sademäärä (1h): {precip:.1f} mm
"""
        
        prompt = (
            "Olet pukeutumisneuvoja. Annan sinulle sääennusteen ja haluan LYHYEN (max 140 merkkiä) "
            "suosituksen mitä pukea päälle töihin/töistä lähtiessä. "
            "Vastaa suomeksi, ytimekkäästi, suoraan pukeutumisohjeella. "
            "Älä toista säätietoja, keskity vain vaatesuositukseen.\n\n"
            f"{weather_desc}"
        )
        
        response = gemini_model.generate_content(prompt)
        recommendation = response.text.strip()
        logger.info(f"Clothing recommendation: {recommendation}")
        return recommendation
        
    except Exception as e:
        logger.error(f"Failed to generate recommendation with Gemini: {e}")
        return None


def send_sms(twilio_client: TwilioClient, message: str) -> bool:
    """Send an SMS message via Twilio."""
    try:
        sms = twilio_client.messages.create(
            body=message,
            from_=TWILIO_FROM_NUMBER,
            to=MY_PHONE_NUMBER,
        )
        logger.info(f"SMS sent successfully. SID: {sms.sid}")
        return True
    except Exception as e:
        logger.error(f"Failed to send SMS: {e}")
        return False


def format_weather_sms(weather: dict, recommendation: str, when: str = "huomenna", hour: int = 16) -> str:
    """Format the weather SMS message.
    
    Args:
        weather: Weather data dict
        recommendation: Clothing recommendation from Gemini
        when: "tänään" or "huomenna"
        hour: Target hour (8 or 16)
    """
    temp = weather.get("temperature")
    wind = weather.get("wind_speed")
    wind_dir = get_wind_direction_text(weather.get("wind_direction"))
    precip = weather.get("precipitation", 0) or 0
    
    # Precipitation description
    if precip == 0:
        precip_text = "Ei sadetta"
    elif precip < 0.5:
        precip_text = "Heikkoa sadetta"
    elif precip < 2:
        precip_text = "Sadetta"
    else:
        precip_text = "Kovaa sadetta"
    
    # Context based on time
    if hour == 8:
        context = "🚗 Töihin"
    else:
        context = "🏠 Töistä"
    
    message = (
        f"{context} - Pirkkala {when} klo {hour}:\n"
        f"🌡️ {temp:.0f}°C | 💨 {wind:.0f} m/s {wind_dir}\n"
        f"🌧️ {precip_text}\n\n"
        f"👕 {recommendation}"
    )
    
    return message


def main():
    """Main loop for the background worker."""
    logger.info("Starting Pirkkala weather notification worker...")
    
    if not validate_env_vars():
        logger.error("Exiting due to missing environment variables.")
        return
    
    # Initialize API clients
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel("gemini-1.5-flash")
    logger.info("Gemini client initialized")
    
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    logger.info("Twilio client initialized")
    
    # Track sent notifications: "YYYY-MM-DD-morning" or "YYYY-MM-DD-evening"
    sent_notifications: set = set()
    
    logger.info(f"Starting main loop. Notifications at {MORNING_SEND_HOUR}:00 and {EVENING_SEND_HOUR}:00...")
    
    while True:
        try:
            now = datetime.now(TIMEZONE)
            today_str = now.strftime("%Y-%m-%d")
            current_hour = now.hour
            
            morning_key = f"{today_str}-morning"
            evening_key = f"{today_str}-evening"
            
            logger.info(f"Current time: {now.strftime('%H:%M')}")
            
            # Morning notification at 9:00 - today's 16:00 weather (leaving work)
            if current_hour == MORNING_SEND_HOUR and morning_key not in sent_notifications:
                logger.info("☀️ Sending MORNING notification (today 16:00 - leaving work)...")
                
                # Fetch TODAY's 16:00 weather
                weather = fetch_weather_forecast(days_ahead=0, target_hour=AFTERNOON_TARGET_HOUR)
                
                if weather and weather.get("temperature") is not None:
                    recommendation = generate_clothing_recommendation(gemini_model, weather)
                    
                    if recommendation:
                        sms_message = format_weather_sms(weather, recommendation, "tänään", AFTERNOON_TARGET_HOUR)
                        if send_sms(twilio_client, sms_message):
                            sent_notifications.add(morning_key)
                            logger.info(f"Morning notification sent for {today_str}")
                    else:
                        sms_message = format_weather_sms(weather, "Pukeudu sään mukaan!", "tänään", AFTERNOON_TARGET_HOUR)
                        send_sms(twilio_client, sms_message)
                        sent_notifications.add(morning_key)
                else:
                    logger.error("Could not fetch weather data for morning notification")
            
            # Evening notification at 20:00 - tomorrow's 8:00 weather (going to work)
            elif current_hour == EVENING_SEND_HOUR and evening_key not in sent_notifications:
                logger.info("🌙 Sending EVENING notification (tomorrow 08:00 - going to work)...")
                
                # Fetch TOMORROW's 08:00 weather
                weather = fetch_weather_forecast(days_ahead=1, target_hour=MORNING_TARGET_HOUR)
                
                if weather and weather.get("temperature") is not None:
                    recommendation = generate_clothing_recommendation(gemini_model, weather)
                    
                    if recommendation:
                        sms_message = format_weather_sms(weather, recommendation, "huomenna", MORNING_TARGET_HOUR)
                        if send_sms(twilio_client, sms_message):
                            sent_notifications.add(evening_key)
                            logger.info(f"Evening notification sent for {today_str}")
                    else:
                        sms_message = format_weather_sms(weather, "Pukeudu sään mukaan!", "huomenna", MORNING_TARGET_HOUR)
                        send_sms(twilio_client, sms_message)
                        sent_notifications.add(evening_key)
                else:
                    logger.error("Could not fetch weather data for evening notification")
            
            else:
                next_send = MORNING_SEND_HOUR if current_hour < MORNING_SEND_HOUR else (
                    EVENING_SEND_HOUR if current_hour < EVENING_SEND_HOUR else MORNING_SEND_HOUR
                )
                logger.info(f"Waiting... Next notification at {next_send}:00")
            
            # Clean up old entries (keep only today's)
            sent_notifications = {k for k in sent_notifications if k.startswith(today_str)}
                    
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
        
        logger.info(f"Sleeping for {CHECK_INTERVAL_SECONDS} seconds...")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
