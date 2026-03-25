"""
Smart Plug Temperature Controller
Controls a Tuya/Smart Life plug based on outside temperature in Saint Cloud, FL.
Turns ON when temp rises above threshold, OFF when it drops below.
"""

import os
import time
import logging
import requests
import tinytuya

# ─────────────────────────────────────────────
#  CONFIGURATION — set these as environment variables
#  (Railway dashboard → Variables, or a local .env file)
#
#  Required:
#    TUYA_API_KEY      — from iot.tuya.com project
#    TUYA_API_SECRET   — from iot.tuya.com project
#    TUYA_DEVICE_ID    — your plug's device ID
#
#  Optional (defaults shown):
#    TUYA_API_REGION   — us | eu | cn | in  (default: us)
#    TEMP_ON_THRESHOLD — °F threshold        (default: 65.0)
#    CHECK_INTERVAL    — seconds             (default: 1800)
# ─────────────────────────────────────────────

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}\n"
            f"Set it in Railway's Variables tab or your .env file."
        )
    return value

TUYA_API_KEY    = _require_env("TUYA_API_KEY")
TUYA_API_SECRET = _require_env("TUYA_API_SECRET")
TUYA_DEVICE_ID  = _require_env("TUYA_DEVICE_ID")
TUYA_API_REGION = os.environ.get("TUYA_API_REGION", "us")

TEMP_ON_THRESHOLD      = float(os.environ.get("TEMP_ON_THRESHOLD", "75.0"))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL", "1800"))

# Saint Cloud, FL coordinates (no weather API key needed)
LATITUDE  = 28.2489
LONGITUDE = -81.2823

# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def get_temperature_f() -> float:
    """Fetch current outdoor temperature in °F for Saint Cloud, FL via Open-Meteo."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "current": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": "America/New_York",
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return float(data["current"]["temperature_2m"])


def set_plug(cloud: tinytuya.Cloud, on: bool) -> None:
    """Turn the Tuya smart plug on or off."""
    commands = {"commands": [{"code": "switch_1", "value": on}]}
    result = cloud.sendcommand(TUYA_DEVICE_ID, commands)
    if result.get("success"):
        log.info("Plug turned %s", "ON" if on else "OFF")
    else:
        log.error("Failed to set plug: %s", result)


def get_plug_state(cloud: tinytuya.Cloud) -> bool | None:
    """Return current plug state (True=on, False=off, None=unknown)."""
    status = cloud.getstatus(TUYA_DEVICE_ID)
    for item in status.get("result", []):
        if item.get("code") == "switch_1":
            return bool(item["value"])
    return None


def main() -> None:
    log.info("Starting smart plug controller")
    log.info("  Location  : Saint Cloud, FL (%.4f, %.4f)", LATITUDE, LONGITUDE)
    log.info("  Threshold : %.1f°F  (plug ON when temp > threshold)", TEMP_ON_THRESHOLD)
    log.info("  Interval  : %d min", CHECK_INTERVAL_SECONDS // 60)

    cloud = tinytuya.Cloud(
        apiRegion=TUYA_API_REGION,
        apiKey=TUYA_API_KEY,
        apiSecret=TUYA_API_SECRET,
        apiDeviceID=TUYA_DEVICE_ID,
    )

    while True:
        try:
            temp = get_temperature_f()
            plug_is_on = get_plug_state(cloud)
            should_be_on = temp > TEMP_ON_THRESHOLD

            log.info(
                "Temp: %.1f°F  |  Threshold: %.1f°F  |  Plug: %s  |  Should be: %s",
                temp,
                TEMP_ON_THRESHOLD,
                "ON" if plug_is_on else ("OFF" if plug_is_on is False else "unknown"),
                "ON" if should_be_on else "OFF",
            )

            # Only send a command if the state needs to change
            if plug_is_on != should_be_on:
                set_plug(cloud, should_be_on)
            else:
                log.info("No change needed")

        except requests.RequestException as exc:
            log.error("Weather fetch failed: %s", exc)
        except Exception as exc:
            log.error("Unexpected error: %s", exc)

        log.info("Sleeping %d minutes...", CHECK_INTERVAL_SECONDS // 60)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
