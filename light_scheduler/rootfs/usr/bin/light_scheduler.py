#!/usr/bin/env python3
"""Light Scheduler — Home Assistant App engine (M1).

Schedules brightness/temperature keyframes for a single light entity
through the Supervisor core API proxy.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="[light-scheduler] %(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration helpers
# --------------------------------------------------------------------------- #

DATA_DIR = "/data"
CONFIG_FILE = os.path.join(DATA_DIR, "options.json")
SCHEDULE_FILE = os.path.join(DATA_DIR, "config.json")


def load_options() -> Dict[str, Any]:
    """Load user options from /data/options.json."""
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_schedules() -> List[Dict[str, Any]]:
    """Load schedule configs from /data/config.json."""
    try:
        with open(SCHEDULE_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


# --------------------------------------------------------------------------- #
# Home Assistant API client (Supervisor core API proxy)
# --------------------------------------------------------------------------- #

import urllib.request
import urllib.error

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
API_BASE = "http://supervisor/core/api/"


def _ha_request(method: str, path: str, body: Optional[Dict] = None) -> Dict:
    """Make a request to the Home Assistant core API via Supervisor proxy."""
    url = API_BASE + path
    headers = {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        log.warning("API %s %s failed: %s", method, path, e)
        raise


def _get_state(entity_id: str) -> Dict[str, Any]:
    """Get the state of a specific entity."""
    return _ha_request("GET", f"states/{entity_id}")


def _post_service(domain: str, service: str, service_data: Dict) -> None:
    """Call a HA service through the Supervisor core API proxy.

    The proxy at /api/services/{domain}/{service} expects the payload
    with service fields directly (not wrapped in 'data').
    """
    _ha_request("POST", "services/" + domain + "/" + service, service_data)


# --------------------------------------------------------------------------- #
# Schedule model
# --------------------------------------------------------------------------- #

class Keyframe:
    """A single point in the schedule timeline."""

    def __init__(self, data: Dict[str, Any]):
        self.id: str = data.get("id", "")
        self.time: str = data.get("time", "00:00")
        self.power: bool = data.get("power", False)
        self.brightness_pct: Optional[int] = data.get("brightness_pct")
        self.kelvin: Optional[int] = data.get("kelvin")
        self.ramp: int = data.get("ramp", 0)  # seconds to reach target

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "time": self.time,
            "power": self.power,
            "brightness_pct": self.brightness_pct,
            "kelvin": self.kelvin,
            "ramp": self.ramp,
        }


def parse_keyframes(raw: List[Dict]) -> List[Keyframe]:
    return [Keyframe(k) for k in raw]


def time_to_minutes(time_str: str) -> int:
    """Convert 'HH:MM' to minutes since midnight."""
    parts = time_str.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def minutes_to_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# --------------------------------------------------------------------------- #
# Application state
# --------------------------------------------------------------------------- #

class AppState:
    """Holds the current light state and schedule info."""

    def __init__(self):
        self.light_id: str = "<none>"
        self.schedule_name: str = "N/A"
        self.keyframes: List[Keyframe] = []
        self.current_keyframe_index: int = 0
        self.last_applied: Dict[str, Any] = {}
        self.powered: bool = False
        self.brightness: Optional[int] = None
        self.color_temp: Optional[int] = None


# --------------------------------------------------------------------------- #
# Scheduler loop
# --------------------------------------------------------------------------- #

TICK_INTERVAL = 30  # seconds
app_state = AppState()
stop_event = threading.Event()


def apply_keyframe(kf: Keyframe, light_id: str) -> bool:
    """Apply a single keyframe to the light."""
    data: Dict[str, Any] = {"entity_id": light_id}
    if kf.power:
        data["brightness_pct"] = kf.brightness_pct or 50
        if kf.kelvin:
            data["kelvin"] = kf.kelvin
        if kf.ramp > 0:
            data["transition"] = kf.ramp
    else:
        data["brightness_pct"] = 0  # turn off
    try:
        _post_service("light", "turn_on", data)
        return True
    except Exception as e:
        log.error("Failed to apply keyframe '%s': %s", kf.id, e)
        return False


def scheduler_loop():
    """Main scheduler loop — fires keyframes based on current time."""
    log.info(
        "starting: light=%s schedule='%s' keyframes=%d tick=%ds",
        app_state.light_id,
        app_state.schedule_name,
        len(app_state.keyframes),
        TICK_INTERVAL,
    )

    while not stop_event.is_set():
        try:
            now = datetime.now()
            current_minutes = now.hour * 60 + now.minute

            # Find the next keyframe to fire
            next_kf = None
            next_idx = 0
            for i, kf in enumerate(app_state.keyframes):
                kf_minutes = time_to_minutes(kf.time)
                if kf_minutes <= current_minutes:
                    next_kf = kf
                    next_idx = i

            if next_kf and next_idx != app_state.current_keyframe_index:
                log.info(
                    "fired keyframe '%s' -> %s (light=%s ok=%s)",
                    next_kf.id,
                    next_kf.as_dict(),
                    app_state.light_id,
                    apply_keyframe(next_kf, app_state.light_id),
                )
                app_state.current_keyframe_index = next_idx
                app_state.last_applied = next_kf.as_dict()

            # Update state from HA
            if app_state.light_id != "<none>":
                state = _get_state(app_state.light_id)
                if state:
                    attrs = state.get("attributes", {})
                    app_state.powered = state.get("state") == "on"
                    app_state.brightness = attrs.get("brightness")
                    app_state.color_temp = attrs.get("color_temp")

        except Exception as e:
            log.error("tick error: %s", e)

        stop_event.wait(TICK_INTERVAL)


# --------------------------------------------------------------------------- #
# Ingress HTTP server
# --------------------------------------------------------------------------- #

class IngressHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler for ingress panel."""

    def log_message(self, format, *args):
        pass  # suppress default logging

    def do_GET(self):
        if self.path == "/api/state":
            self._send_json(200, self._state_snapshot())
        elif self.path == "/":
            self._serve_index()
        else:
            self._send_json(404, {"error": "not found"})

    def _state_snapshot(self) -> Dict[str, Any]:
        return {
            "light_id": app_state.light_id,
            "schedule_name": app_state.schedule_name,
            "keyframes": [kf.as_dict() for kf in app_state.keyframes],
            "current_index": app_state.current_keyframe_index,
            "last_applied": app_state.last_applied,
            "powered": app_state.powered,
            "brightness": app_state.brightness,
            "color_temp": app_state.color_temp,
        }

    def _serve_index(self):
        index_path = "/usr/bin/index.html"
        try:
            with open(index_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self._send_json(404, {"error": "index.html not found"})

    def _send_json(self, status: int, data: Dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))


def run_ingress_server(port: int = 8099):
    """Start the ingress HTTP server."""
    server = HTTPServer(("0.0.0.0", port), IngressHandler)
    log.info("ingress listening on 0.0.0.0:%d", port)
    server.serve_forever()


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def main():
    """Initialize and start the Light Scheduler."""
    # Load configuration
    options = load_options()
    light_id = options.get("light", "")
    if not light_id:
        log.warning("no light configured — set the app 'light' option. Scheduling is idle until then.")
    else:
        app_state.light_id = light_id

    schedules = load_schedules()
    if schedules:
        first_schedule = schedules[0]
        app_state.schedule_name = first_schedule.get("name", "N/A")
        app_state.keyframes = parse_keyframes(first_schedule.get("keyframes", []))
    else:
        log.warning("no schedules loaded — create at least one in the UI.")

    # Start scheduler thread
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()

    # Start ingress server
    run_ingress_server()


if __name__ == "__main__":
    main()
