#!/usr/bin/env python3
"""Light Scheduler — Home Assistant App (M1)."""
import json
import os
import sys
import time
import logging
import threading
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

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
# Configuration
# --------------------------------------------------------------------------- #
def load_options():
    """Load app options from /data/options.json (mounted by Supervisor)."""
    try:
        with open("/data/options.json", "r") as f:
            return json.load(f)
    except Exception:
        return {}

# --------------------------------------------------------------------------- #
# Home Assistant REST client (Supervisor core API proxy)
# --------------------------------------------------------------------------- #
def _ha_request(method, path, body=None):
    """Call the HA Core API through the Supervisor proxy.

    The proxy at http://supervisor/core/api/ forwards requests to
    Home Assistant Core.  The app needs homeassistant_api: true in
    config.yaml and reads the SUPERVISOR_TOKEN env var.
    """
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    url = "http://supervisor/core/api/" + path
    data = None
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace") if e.read else str(e)
        log("API %s %s failed: %s %s" % (method, path, e.code, body_text))
        raise
    return json.loads(raw) if raw else {}


def _post_service(domain, service, data):
    """Call a HA service through the Supervisor core API proxy.

    The proxy at /api/services/{domain}/{service} expects the payload
    wrapped in a 'data' key.
    """
    _ha_request("POST", "services/" + domain + "/" + service, {"data": data})


# --------------------------------------------------------------------------- #
# Ingress SPA (lightweight HTTP server)
# --------------------------------------------------------------------------- #

def ha_get_state(entity):
    """Return the state object for *entity*, or a sentinel on failure."""
    if not entity:
        return None
    try:
        return _ha_request("GET", "states/" + entity)
    except (urllib.error.URLError, ValueError, OSError) as exc:
        return {"_error": str(exc)}


def ha_set_light(entity, power, brightness_pct=None, kelvin=None, transition=0):
    """Send a single turn_on/turn_off with the given target and fade.

    Unsupported fields are dropped by HA, so this is safe across bulb types.
    Returns True on success, False on failure (already logged by caller).
    """
    if not entity:
        return False
    payload = {"entity_id": entity}
    if power:
        if transition:
            payload["transition"] = transition
        if brightness_pct is not None:
            payload["brightness_pct"] = brightness_pct
        if kelvin is not None:
            payload["kelvin"] = kelvin
        _post_service("light", "turn_on", payload)
    else:
        if transition:
            payload["transition"] = transition
        _post_service("light", "turn_off", payload)
    return True


# --------------------------------------------------------------------------- #
# Schedule model
# --------------------------------------------------------------------------- #
def parse_when(when, day):
    """Resolve a keyframe ``when`` to a concrete datetime on *day*.

    M1 supports clock time "HH:MM". Sun anchors (sunrise/sunset/solar_noon
    / solar_midnight) are documented for M2.
    """
    if isinstance(when, str) and when.startswith("sun"):
        # M2 placeholder — returns a sentinel that the scheduler handles.
        return None  # TODO: resolve sun anchor from geolocation
    parts = when.split(":")
    if len(parts) != 2:
        raise ValueError("Invalid when: %s (expected HH:MM)" % when)
    hour, minute = int(parts[0]), int(parts[1])
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def load_schedule(schedule_def):
    """Parse a schedule definition dict into a list of keyframe dicts.

    Each keyframe has:
      - when: str "HH:MM" (or sun-anchor placeholder)
      - power: bool
      - brightness_pct: int (optional)
      - kelvin: int (optional)
      - transition: int seconds (default 0)
    """
    keyframes = []
    for kf in schedule_def.get("keyframes", []):
        parsed = {
            "when": kf["when"],
            "power": kf.get("power", True),
            "brightness_pct": kf.get("brightness_pct"),
            "kelvin": kf.get("kelvin"),
            "transition": kf.get("transition", 0),
        }
        keyframes.append(parsed)
    return sorted(keyframes, key=lambda k: parse_when(k["when"], datetime.now()))


# --------------------------------------------------------------------------- #
# Scheduler loop
# --------------------------------------------------------------------------- #
class LightScheduler:
    """Main scheduler: reads config, runs tick loop, applies keyframes."""

    def __init__(self, light_entity, schedule_def, tick_seconds=30):
        self.light_entity = light_entity
        self.keyframes = load_schedule(schedule_def)
        self.tick_seconds = tick_seconds
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        log.info(
            "starting: light=%s schedule='%s' keyframes=%d tick=%ds",
            self.light_entity,
            self.keyframes[0]["when"] if self.keyframes else "N/A",
            len(self.keyframes),
            self.tick_seconds,
        )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        log.info("Light Scheduler stopping")
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                log.error("tick error: %s", exc)
            self._stop_event.wait(self.tick_seconds)

    def _tick(self):
        now = datetime.now()
        if not self.keyframes:
            return

        # Find the next keyframe to apply
        next_kf = None
        for kf in self.keyframes:
            try:
                dt = parse_when(kf["when"], now.replace(hour=0, minute=0, second=0, microsecond=0))
                if dt > now:
                    next_kf = kf
                    break
            except ValueError:
                continue

        # If no future keyframe, wrap to first one tomorrow
        if next_kf is None:
            next_kf = self.keyframes[0]
            try:
                dt = parse_when(next_kf["when"], (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
            except ValueError:
                return

        # Apply the keyframe
        try:
            ha_set_light(
                self.light_entity,
                power=next_kf["power"],
                brightness_pct=next_kf.get("brightness_pct"),
                kelvin=next_kf.get("kelvin"),
                transition=next_kf.get("transition", 0),
            )
            log.info(
                "fired keyframe '%s' -> %s (light=%s ok=%s)",
                next_kf["when"],
                {k: v for k, v in next_kf.items() if k != "when"},
                self.light_entity,
                True,
            )
        except Exception as exc:
            log.error("failed to apply keyframe '%s': %s", next_kf["when"], exc)


# --------------------------------------------------------------------------- #
# Ingress HTTP server
# --------------------------------------------------------------------------- #
class IngressHandler(BaseHTTPRequestHandler):
    """Minimal HTTP server for the ingress panel SPA."""

    scheduler = None  # set by main

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            with open("/usr/share/light_scheduler/index.html", "rb") as f:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f.read())
        elif self.path == "/api/state":
            self._handle_state()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_state(self):
        if not self.scheduler:
            self._json_response({"error": "no scheduler"})
            return
        state = ha_get_state(self.scheduler.light_entity)
        self._json_response(state or {})

    def _json_response(self, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress default logging


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    options = load_options()
    light_entity = os.environ.get("LS_LIGHT", "") or options.get("light", "")
    schedule_def = options.get("schedule", {"keyframes": []})
    tick_seconds = int(options.get("tick", 30))

    log.info("Light Scheduler starting (light option not set — will read from /data/options.json)")

    scheduler = LightScheduler(light_entity, schedule_def, tick_seconds)
    IngressHandler.scheduler = scheduler

    # Start scheduler thread
    scheduler.start()

    # Start ingress HTTP server
    server = ThreadingHTTPServer(("0.0.0.0", 8099), IngressHandler)
    log.info("ingress listening on 0.0.0.0:8099")

    # Graceful shutdown
    import signal

    def shutdown(sig, frame):
        scheduler.stop()
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server.serve_forever()


if __name__ == "__main__":
    main()
