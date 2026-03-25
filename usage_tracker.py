"""
Smart Plug Usage Tracker
Logs daily on-time (minutes), cycle counts, and temperature correlation.
Serves a Flask dashboard with Chart.js visualizations.
"""

import os
import time
import logging
import sqlite3
import threading
import requests
import tinytuya
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

TUYA_API_KEY    = _require_env("TUYA_API_KEY")
TUYA_API_SECRET = _require_env("TUYA_API_SECRET")
TUYA_DEVICE_ID  = _require_env("TUYA_DEVICE_ID")
TUYA_API_REGION = os.environ.get("TUYA_API_REGION", "us")

DB_PATH = os.environ.get("DB_PATH", "/data/plug_tracker.db")
PORT    = int(os.environ.get("PORT", "8080"))

LATITUDE  = 28.2489
LONGITUDE = -81.2823

COLLECT_INTERVAL_SECONDS = 3600  # 1 hour

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────

def init_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_usage (
                date        TEXT PRIMARY KEY,
                on_seconds  INTEGER NOT NULL DEFAULT 0,
                cycles      INTEGER NOT NULL DEFAULT 0,
                temp_high_f REAL,
                temp_low_f  REAL,
                updated_at  TEXT NOT NULL
            )
        """)
        conn.commit()


def upsert_day(db_path: str, date: str, on_seconds: int, cycles: int,
               temp_high_f: float | None, temp_low_f: float | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            INSERT INTO daily_usage (date, on_seconds, cycles, temp_high_f, temp_low_f, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                on_seconds  = excluded.on_seconds,
                cycles      = excluded.cycles,
                temp_high_f = COALESCE(excluded.temp_high_f, daily_usage.temp_high_f),
                temp_low_f  = COALESCE(excluded.temp_low_f, daily_usage.temp_low_f),
                updated_at  = excluded.updated_at
        """, (date, on_seconds, cycles, temp_high_f, temp_low_f, now))
        conn.commit()


def upsert_temps_only(db_path: str, date: str, temp_high_f: float | None,
                      temp_low_f: float | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            INSERT INTO daily_usage (date, on_seconds, cycles, temp_high_f, temp_low_f, updated_at)
            VALUES (?, 0, 0, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                temp_high_f = COALESCE(excluded.temp_high_f, daily_usage.temp_high_f),
                temp_low_f  = COALESCE(excluded.temp_low_f, daily_usage.temp_low_f),
                updated_at  = excluded.updated_at
        """, (date, temp_high_f, temp_low_f, now))
        conn.commit()


def fetch_all_days(db_path: str, days: int = 90) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT date, on_seconds, cycles, temp_high_f, temp_low_f
            FROM daily_usage
            ORDER BY date DESC
            LIMIT ?
        """, (days,)).fetchall()
    return [dict(r) for r in rows]

# ─────────────────────────────────────────────
# Tuya log fetching
# ─────────────────────────────────────────────

def fetch_device_logs(cloud: tinytuya.Cloud, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch all switch events between start_ms and end_ms (Unix milliseconds)."""
    events = []
    last_row_key = ""

    while True:
        params = {
            "start_row_id": last_row_key,
            "start_time": start_ms,
            "end_time": end_ms,
            "type": 7,
            "size": 100,
        }

        try:
            path = f"/v1.0/devices/{TUYA_DEVICE_ID}/logs"
            result = cloud.cloudrequest(path, params=params)
        except AttributeError:
            # Fallback for older tinytuya versions
            result = cloud.getdevicelog(
                TUYA_DEVICE_ID,
                start=start_ms,
                end=end_ms,
                evtype=7,
            )

        if not result or not result.get("success"):
            log.warning("Log fetch failed or empty: %s", result)
            break

        data = result.get("result", {})
        logs = data.get("logs", [])

        for entry in logs:
            code = entry.get("code", "")
            if code.startswith("switch"):
                events.append({
                    "ts_ms": int(entry["event_time"]),
                    "value": entry["value"],
                })

        last_row_key = data.get("last_row_key", "")
        has_more = data.get("has_next", False)

        if not has_more or not last_row_key:
            break

    return sorted(events, key=lambda e: e["ts_ms"])


def compute_daily_usage(events: list[dict]) -> dict[str, dict]:
    """
    Given sorted switch events, compute on_seconds and cycles per calendar day.
    Duration is attributed to the day the ON event occurred.
    Uses America/New_York (UTC-4 EDT).
    """
    tz = timezone(timedelta(hours=-4))
    daily: dict[str, dict] = {}
    last_on_ms: int | None = None

    for event in events:
        ts_ms = event["ts_ms"]
        is_on = event["value"] == "true"
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=tz)
        date_str = dt.strftime("%Y-%m-%d")

        if date_str not in daily:
            daily[date_str] = {"on_seconds": 0, "cycles": 0}

        if is_on:
            last_on_ms = ts_ms
        else:
            if last_on_ms is not None:
                on_dt = datetime.fromtimestamp(last_on_ms / 1000, tz=tz)
                on_date = on_dt.strftime("%Y-%m-%d")
                if on_date not in daily:
                    daily[on_date] = {"on_seconds": 0, "cycles": 0}
                duration_s = (ts_ms - last_on_ms) / 1000
                daily[on_date]["on_seconds"] += int(duration_s)
                daily[on_date]["cycles"] += 1
                last_on_ms = None

    return daily

# ─────────────────────────────────────────────
# Open-Meteo temperature history
# ─────────────────────────────────────────────

def fetch_temp_history(days: int = 90) -> dict[str, dict]:
    """Fetch daily high/low temps in °F for the past N days."""
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "timezone": "America/New_York",
        "start_date": start_date,
        "end_date": end_date,
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    result = {}
    for date, high, low in zip(
        data["daily"]["time"],
        data["daily"]["temperature_2m_max"],
        data["daily"]["temperature_2m_min"],
    ):
        result[date] = {"high": high, "low": low}

    return result

# ─────────────────────────────────────────────
# Collection loop
# ─────────────────────────────────────────────

def collect_and_store(cloud: tinytuya.Cloud) -> None:
    log.info("Starting data collection run...")

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (26 * 3600 * 1000)  # 26-hour window

    try:
        events = fetch_device_logs(cloud, start_ms, now_ms)
        log.info("Fetched %d switch events", len(events))
    except Exception as exc:
        log.error("Failed to fetch device logs: %s", exc)
        return

    daily_usage = compute_daily_usage(events)

    try:
        temp_history = fetch_temp_history(days=90)
    except Exception as exc:
        log.warning("Failed to fetch temperature history: %s", exc)
        temp_history = {}

    for date, usage in daily_usage.items():
        temps = temp_history.get(date, {})
        upsert_day(DB_PATH, date, usage["on_seconds"], usage["cycles"],
                   temps.get("high"), temps.get("low"))
        log.info("Upserted %s: %ds on, %d cycles", date, usage["on_seconds"], usage["cycles"])

    for date, temps in temp_history.items():
        upsert_temps_only(DB_PATH, date, temps.get("high"), temps.get("low"))

    log.info("Collection run complete.")


def collection_loop(cloud: tinytuya.Cloud) -> None:
    while True:
        try:
            collect_and_store(cloud)
        except Exception as exc:
            log.error("Collection loop error: %s", exc)
        log.info("Next collection in %d minutes...", COLLECT_INTERVAL_SECONDS // 60)
        time.sleep(COLLECT_INTERVAL_SECONDS)

# ─────────────────────────────────────────────
# Flask dashboard
# ─────────────────────────────────────────────

app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Smart Plug Tracker</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 20px; background: #0f172a; color: #e2e8f0; }
  h1 { color: #38bdf8; margin-bottom: 24px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
  .card { background: #1e293b; border-radius: 12px; padding: 20px; }
  canvas { max-height: 280px; }
  @media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<h1>Smart Plug Usage Tracker — Saint Cloud, FL</h1>
<div class="grid">
  <div class="card"><canvas id="onTimeChart"></canvas></div>
  <div class="card"><canvas id="cyclesChart"></canvas></div>
  <div class="card"><canvas id="tempChart"></canvas></div>
  <div class="card"><canvas id="correlationChart"></canvas></div>
</div>
<script>
const axisStyle = {
  ticks: { color: '#94a3b8' },
  grid:  { color: '#334155' }
};
const legendStyle = { labels: { color: '#e2e8f0' } };

async function loadData() {
  const rows = await fetch('/api/data').then(r => r.json());
  rows.reverse(); // oldest first

  const labels = rows.map(r => r.date);
  const onMins = rows.map(r => (r.on_seconds / 60).toFixed(1));
  const cycles = rows.map(r => r.cycles);
  const highs  = rows.map(r => r.temp_high_f);
  const lows   = rows.map(r => r.temp_low_f);

  new Chart(document.getElementById('onTimeChart'), {
    type: 'bar',
    data: { labels, datasets: [{ label: 'On-Time (min)', data: onMins, backgroundColor: '#38bdf8' }] },
    options: {
      responsive: true,
      plugins: { legend: legendStyle, title: { display: true, text: 'Daily On-Time (minutes)', color: '#e2e8f0' } },
      scales: { x: { ...axisStyle, ticks: { ...axisStyle.ticks, maxRotation: 45 } }, y: axisStyle }
    }
  });

  new Chart(document.getElementById('cyclesChart'), {
    type: 'bar',
    data: { labels, datasets: [{ label: 'Cycles', data: cycles, backgroundColor: '#a78bfa' }] },
    options: {
      responsive: true,
      plugins: { legend: legendStyle, title: { display: true, text: 'Daily Cycle Count', color: '#e2e8f0' } },
      scales: { x: { ...axisStyle, ticks: { ...axisStyle.ticks, maxRotation: 45 } }, y: axisStyle }
    }
  });

  new Chart(document.getElementById('tempChart'), {
    type: 'line',
    data: { labels, datasets: [
      { label: 'High °F', data: highs, borderColor: '#f97316', backgroundColor: 'transparent', tension: 0.3 },
      { label: 'Low °F',  data: lows,  borderColor: '#60a5fa', backgroundColor: 'transparent', tension: 0.3 }
    ]},
    options: {
      responsive: true,
      plugins: { legend: legendStyle, title: { display: true, text: 'Daily Temperature', color: '#e2e8f0' } },
      scales: { x: { ...axisStyle, ticks: { ...axisStyle.ticks, maxRotation: 45 } }, y: axisStyle }
    }
  });

  const scatter = rows.filter(r => r.temp_high_f != null)
                      .map(r => ({ x: r.temp_high_f, y: (r.on_seconds / 60).toFixed(1) }));
  new Chart(document.getElementById('correlationChart'), {
    type: 'scatter',
    data: { datasets: [{ label: 'On-Time vs High Temp', data: scatter, backgroundColor: '#34d399' }] },
    options: {
      responsive: true,
      plugins: { legend: legendStyle, title: { display: true, text: 'On-Time vs Temperature', color: '#e2e8f0' } },
      scales: {
        x: { ...axisStyle, title: { display: true, text: 'High Temp (°F)', color: '#94a3b8' } },
        y: { ...axisStyle, title: { display: true, text: 'On-Time (min)', color: '#94a3b8' } }
      }
    }
  });
}

loadData();
</script>
</body>
</html>
"""

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)

@app.route("/api/data")
def api_data():
    rows = fetch_all_days(DB_PATH, days=90)
    return jsonify(rows)

# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main() -> None:
    log.info("Starting Smart Plug Usage Tracker")
    log.info("  DB: %s", DB_PATH)
    log.info("  Port: %d", PORT)

    init_db(DB_PATH)

    cloud = tinytuya.Cloud(
        apiRegion=TUYA_API_REGION,
        apiKey=TUYA_API_KEY,
        apiSecret=TUYA_API_SECRET,
        apiDeviceID=TUYA_DEVICE_ID,
    )

    t = threading.Thread(target=collection_loop, args=(cloud,), daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
