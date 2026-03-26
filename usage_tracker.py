"""
usage_tracker.py
Add this file to your existing smart-plug-controller Railway project.

Deploys as a SECOND Railway service pointing to the same repo:
  Start command: python usage_tracker.py
  Port: set by Railway automatically via $PORT env var

Uses the same environment variables already set in your Railway project:
  TUYA_API_KEY    — same as smart_plug_controller.py
  TUYA_API_SECRET — same as smart_plug_controller.py
  TUYA_DEVICE_ID  — same as smart_plug_controller.py
  TUYA_API_REGION — optional, defaults to "us"

What it does:
  - Every hour: fetches the last 26 hours of device logs from Tuya,
    computes on-time and cycle counts per day, fetches Saint Cloud FL
    weather, and upserts rows into a local SQLite database.
  - Serves a live Chart.js dashboard at your Railway service URL.
"""

import os
import sqlite3
import time
import threading
import logging
from datetime import datetime, timezone, timedelta

import requests
import tinytuya
from flask import Flask, jsonify

# ── Config (matches existing Railway env vars) ─────────────────────────────
TUYA_API_KEY    = os.environ["TUYA_API_KEY"]
TUYA_API_SECRET = os.environ["TUYA_API_SECRET"]
TUYA_DEVICE_ID  = os.environ["TUYA_DEVICE_ID"]
TUYA_API_REGION = os.environ.get("TUYA_API_REGION", "us")

LATITUDE  = 28.2489
LONGITUDE = -81.2823
DB_PATH   = os.environ.get("DB_PATH", "plug_tracker.db")
PORT      = int(os.environ.get("PORT", 8080))
COLLECT_INTERVAL = int(os.environ.get("COLLECT_INTERVAL", 3600))  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                date        TEXT PRIMARY KEY,
                on_seconds  REAL    DEFAULT 0,
                cycles      INTEGER DEFAULT 0,
                temp_max_f  REAL,
                temp_min_f  REAL,
                updated_at  TEXT
            )
        """)
        conn.commit()
    log.info("Database ready: %s", DB_PATH)


# ── Tuya log fetching (uses tinytuya.Cloud, same library as controller) ───────

def fetch_tuya_logs(start_ms: int, end_ms: int) -> list[dict]:
    """
    Pull switch-state events from Tuya Cloud for the given time window.
    tinytuya.Cloud wraps the same /v1.0/devices/{id}/logs endpoint.
    """
    cloud = tinytuya.Cloud(
        apiRegion=TUYA_API_REGION,
        apiKey=TUYA_API_KEY,
        apiSecret=TUYA_API_SECRET,
        apiDeviceID=TUYA_DEVICE_ID,
    )

    all_events = []
    last_row_key = ""

    while True:
        params = {
            "start_time": start_ms,
            "end_time":   end_ms,
            "type":       7,
            "size":       100,
        }
        if last_row_key:
            params["last_row_key"] = last_row_key

        # tinytuya.Cloud.cloudrequest is the raw authenticated GET helper
        result = cloud.cloudrequest(
            f"/v1.0/devices/{TUYA_DEVICE_ID}/logs",
            query=params,
        )

        if not result.get("success"):
            log.error("Tuya log error: %s", result)
            break

        logs = result.get("result", {}).get("logs", [])
        for entry in logs:
            if str(entry.get("code", "")).startswith("switch"):
                all_events.append({
                    "event_time": entry["event_time"],
                    "value":      str(entry.get("value", "")).lower(),
                })

        last_row_key = result.get("result", {}).get("last_row_key", "")
        if not last_row_key or len(logs) < 100:
            break

    return all_events


def compute_daily_stats(events: list[dict]) -> dict[str, dict]:
    """
    Walk ON/OFF events and accumulate per-day on-time + cycles.
    Like reading a punch-in/punch-out timecard: each ON starts a session,
    each OFF ends it and adds the duration to that day's total.
    """
    events = sorted(events, key=lambda e: e["event_time"])
    stats: dict[str, dict] = {}
    pending_on: int | None = None

    def _day(ts_ms: int) -> str:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    def _ensure(d: str) -> None:
        if d not in stats:
            stats[d] = {"on_seconds": 0.0, "cycles": 0}

    for evt in events:
        ts  = evt["event_time"]
        val = evt["value"]
        if val in ("true", "1", "on"):
            pending_on = ts
        elif val in ("false", "0", "off") and pending_on is not None:
            d = _day(pending_on)
            _ensure(d)
            stats[d]["on_seconds"] += (ts - pending_on) / 1000
            stats[d]["cycles"]     += 1
            pending_on = None

    # If still ON right now, count up to the current moment
    if pending_on is not None:
        now_ms = int(time.time() * 1000)
        d = _day(pending_on)
        _ensure(d)
        stats[d]["on_seconds"] += (now_ms - pending_on) / 1000

    return stats


# ── Weather (Open-Meteo, same service as existing controller) ─────────────────

def fetch_weather(start_date: str, end_date: str) -> dict[str, dict]:
    """Fetch daily high/low °F for Saint Cloud FL. Returns dict keyed by date."""
    params = {
        "latitude":         LATITUDE,
        "longitude":        LONGITUDE,
        "daily":            "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "timezone":         "America/New_York",
        "start_date":       start_date,
        "end_date":         end_date,
    }
    resp = requests.get("https://api.open-meteo.com/v1/forecast",
                        params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    result = {}
    for d, hi, lo in zip(data["daily"]["time"],
                          data["daily"]["temperature_2m_max"],
                          data["daily"]["temperature_2m_min"]):
        if hi is not None and lo is not None:
            result[d] = {"temp_max_f": hi, "temp_min_f": lo}
    return result


# ── Collector loop ────────────────────────────────────────────────────────────

def collect_once() -> None:
    now     = datetime.now(timezone.utc)
    end_ms  = int(now.timestamp() * 1000)
    start_ms= int((now - timedelta(hours=26)).timestamp() * 1000)

    log.info("Collecting Tuya logs (%s → now)...", (now - timedelta(hours=26)).strftime("%Y-%m-%d %H:%M"))
    try:
        events = fetch_tuya_logs(start_ms, end_ms)
    except Exception as exc:
        log.error("Failed to fetch Tuya logs: %s", exc)
        return
    log.info("Got %d switch events", len(events))

    daily = compute_daily_stats(events)

    today_str = now.strftime("%Y-%m-%d")
    all_dates = set(daily.keys()) | {today_str}
    min_date, max_date = min(all_dates), max(all_dates)

    try:
        weather = fetch_weather(min_date, max_date)
    except Exception as exc:
        log.warning("Weather fetch failed: %s", exc)
        weather = {}

    with get_db() as conn:
        for date_str in sorted(all_dates):
            s = daily.get(date_str, {"on_seconds": 0.0, "cycles": 0})
            w = weather.get(date_str, {})
            conn.execute("""
                INSERT INTO daily_stats (date, on_seconds, cycles, temp_max_f, temp_min_f, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    on_seconds = excluded.on_seconds,
                    cycles     = excluded.cycles,
                    temp_max_f = COALESCE(excluded.temp_max_f, daily_stats.temp_max_f),
                    temp_min_f = COALESCE(excluded.temp_min_f, daily_stats.temp_min_f),
                    updated_at = excluded.updated_at
            """, (
                date_str,
                round(s["on_seconds"], 1),
                s["cycles"],
                w.get("temp_max_f"),
                w.get("temp_min_f"),
                now.isoformat(),
            ))
        conn.commit()
    log.info("Upserted %d days into database", len(all_dates))


def collector_loop() -> None:
    """Background thread: collect immediately, then every COLLECT_INTERVAL seconds."""
    while True:
        try:
            collect_once()
        except Exception as exc:
            log.error("Collector error: %s", exc)
        log.info("Next collection in %d min", COLLECT_INTERVAL // 60)
        time.sleep(COLLECT_INTERVAL)


# ── Flask dashboard ───────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route("/api/data")
def api_data():
    """Return last 90 days of stats as JSON for the dashboard."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT date, on_seconds,
                   ROUND(on_seconds / 60.0, 2) AS on_minutes,
                   cycles, temp_max_f, temp_min_f, updated_at
            FROM daily_stats
            ORDER BY date DESC
            LIMIT 90
        """).fetchall()
    return jsonify([dict(r) for r in reversed(rows)])


@app.route("/")
def dashboard():
    return DASHBOARD_HTML


# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Shed AC Usage — Saint Cloud, FL</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:#0f172a; --card:#1e293b; --border:#334155;
    --accent:#38bdf8; --green:#4ade80; --orange:#fb923c;
    --red:#f87171; --text:#e2e8f0; --muted:#94a3b8;
  }
  *, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text);
         font-family:"Inter","Segoe UI",system-ui,sans-serif;
         min-height:100vh; padding:2rem 1rem; }
  h1 { font-size:1.6rem; font-weight:700; margin-bottom:.25rem; }
  .subtitle { color:var(--muted); font-size:.875rem; margin-bottom:2rem; }
  .stats-grid {
    display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
    gap:1rem; margin-bottom:2rem;
  }
  .stat-card { background:var(--card); border:1px solid var(--border);
               border-radius:12px; padding:1.25rem; }
  .stat-label { color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.05em; }
  .stat-value { font-size:2rem; font-weight:700; margin-top:.25rem; }
  .stat-sub   { color:var(--muted); font-size:.8rem; margin-top:.15rem; }
  .charts-grid {
    display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:1.5rem;
  }
  .chart-card { background:var(--card); border:1px solid var(--border);
                border-radius:12px; padding:1.5rem; }
  .chart-card h2 { font-size:1rem; font-weight:600; margin-bottom:1rem; }
  canvas { max-height:280px; }
  footer { text-align:center; color:var(--muted); font-size:.75rem; margin-top:2.5rem; }
  .loading { text-align:center; color:var(--muted); padding:4rem; }
</style>
</head>
<body>
<h1>&#x26A1; Shed AC Usage</h1>
<p class="subtitle" id="subtitle">Saint Cloud, FL &nbsp;&middot;&nbsp; Loading&hellip;</p>

<div class="stats-grid" id="statsGrid">
  <div class="stat-card"><div class="loading">Loading data&hellip;</div></div>
</div>
<div class="charts-grid" id="chartsGrid"></div>
<footer>Data collected hourly via Railway &middot; Weather: Open-Meteo &middot; Plug: Tuya Cloud API</footer>

<script>
const GRID   = { color:"rgba(148,163,184,0.12)" };
const FONT   = { color:"#94a3b8" };
const LEGEND = { labels:{ color:"#e2e8f0", boxWidth:12 } };

async function load() {
  const rows = await fetch("/api/data").then(r => r.json());
  if (!rows.length) {
    document.getElementById("statsGrid").innerHTML =
      "<div class='stat-card'><div class='loading'>No data yet — first collection runs on startup.</div></div>";
    return;
  }

  const last = rows[rows.length - 1];
  const dates    = rows.map(r => r.date);
  const onMin    = rows.map(r => +(r.on_minutes || 0));
  const cycles   = rows.map(r => +(r.cycles     || 0));
  const tempMax  = rows.map(r => r.temp_max_f != null ? +r.temp_max_f : null);
  const tempMin  = rows.map(r => r.temp_min_f != null ? +r.temp_min_f : null);
  const scatter  = rows
    .filter(r => r.temp_max_f != null)
    .map(r => ({ x: +r.temp_max_f, y: +r.on_minutes, label: r.date }));

  const peakIdx  = onMin.indexOf(Math.max(...onMin));
  const lastUpd  = new Date(last.updated_at || "")
    .toLocaleString("en-US", {timeZone:"America/New_York", hour12:true,
      month:"numeric", day:"numeric", year:"numeric",
      hour:"numeric", minute:"2-digit"}) + " EST";

  document.getElementById("subtitle").textContent =
    `Saint Cloud, FL · Last updated: ${lastUpd}`;

  document.getElementById("statsGrid").innerHTML = `
    <div class="stat-card">
      <div class="stat-label">Today — On Time</div>
      <div class="stat-value" style="color:var(--green)">${Math.round(+last.on_minutes)}<span style="font-size:1rem"> min</span></div>
      <div class="stat-sub">${(+last.on_minutes/60).toFixed(1)} hours</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Today — Cycles</div>
      <div class="stat-value" style="color:var(--accent)">${last.cycles}</div>
      <div class="stat-sub">on &rarr; off events</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Today — High Temp</div>
      <div class="stat-value" style="color:var(--orange)">${last.temp_max_f != null ? Math.round(+last.temp_max_f)+"&deg;F" : "&mdash;"}</div>
      <div class="stat-sub">Saint Cloud, FL</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Peak Day (90d)</div>
      <div class="stat-value" style="color:var(--red)">${Math.round(onMin[peakIdx])}<span style="font-size:1rem"> min</span></div>
      <div class="stat-sub">${dates[peakIdx]}</div>
    </div>`;

  document.getElementById("chartsGrid").innerHTML = `
    <div class="chart-card"><h2>Daily On-Time (minutes)</h2><canvas id="c1"></canvas></div>
    <div class="chart-card"><h2>Daily Cycles (on&rarr;off count)</h2><canvas id="c2"></canvas></div>
    <div class="chart-card"><h2>Temperature High / Low (&deg;F)</h2><canvas id="c3"></canvas></div>
    <div class="chart-card"><h2>On-Time vs Temperature Correlation</h2><canvas id="c4"></canvas></div>`;

  new Chart(document.getElementById("c1"), {
    type:"bar", data:{ labels:dates, datasets:[{
      label:"On-Time (min)", data:onMin,
      backgroundColor:"rgba(74,222,128,0.7)", borderRadius:4
    }]},
    options:{ plugins:{legend:LEGEND}, scales:{
      x:{ticks:FONT,grid:GRID}, y:{ticks:FONT,grid:GRID,
        title:{display:true,text:"minutes",color:"#94a3b8"}}
    }}
  });

  new Chart(document.getElementById("c2"), {
    type:"bar", data:{ labels:dates, datasets:[{
      label:"Cycles", data:cycles,
      backgroundColor:"rgba(56,189,248,0.7)", borderRadius:4
    }]},
    options:{ plugins:{legend:LEGEND}, scales:{
      x:{ticks:FONT,grid:GRID}, y:{ticks:FONT,grid:GRID,beginAtZero:true,
        title:{display:true,text:"cycles",color:"#94a3b8"}}
    }}
  });

  new Chart(document.getElementById("c3"), {
    type:"line", data:{ labels:dates, datasets:[
      { label:"High °F", data:tempMax, borderColor:"#fb923c",
        backgroundColor:"rgba(251,146,60,0.15)", tension:0.3, fill:false, pointRadius:2 },
      { label:"Low °F",  data:tempMin, borderColor:"#7dd3fc",
        backgroundColor:"rgba(125,211,252,0.1)", tension:0.3, fill:false, pointRadius:2 }
    ]},
    options:{ plugins:{legend:LEGEND}, scales:{
      x:{ticks:FONT,grid:GRID}, y:{ticks:FONT,grid:GRID,
        title:{display:true,text:"°F",color:"#94a3b8"}}
    }}
  });

  new Chart(document.getElementById("c4"), {
    type:"scatter", data:{ datasets:[{
      label:"On-Time vs High Temp", data:scatter,
      backgroundColor:"rgba(251,146,60,0.7)", pointRadius:5, pointHoverRadius:7
    }]},
    options:{ plugins:{ legend:LEGEND,
      tooltip:{ callbacks:{ label: ctx => `${ctx.raw.label}: ${ctx.raw.y} min @ ${ctx.raw.x}°F` }}
    }, scales:{
      x:{ticks:FONT,grid:GRID,title:{display:true,text:"Daily High °F",color:"#94a3b8"}},
      y:{ticks:FONT,grid:GRID,title:{display:true,text:"On-Time (min)",color:"#94a3b8"}}
    }}
  });
}

load();
</script>
</body>
</html>"""


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    # Start background collector in a daemon thread
    t = threading.Thread(target=collector_loop, daemon=True)
    t.start()
    log.info("Dashboard running on port %d", PORT)
    # Use Flask dev server (Railway handles the reverse proxy)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
