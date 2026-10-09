#!/usr/bin/python3
"""Light Scheduler — M1 skeleton.

Pure standard library (no pip). Talks to Home Assistant over the
Supervisor-proxied REST API and serves the ingress panel.

M1 implements the **step-and-fade** ramp model (scope decision D5): at each
keyframe's time the target is sent together with a ``transition`` and the lamp
is trusted to fade to it; between keyframes the light holds its last value. HA
drops service fields a lamp does not support, so one code path works across
mixed bulb types.

What is NOT in M1 (see SCOPE.md):
  * the visual timeline editor (M2) — the schedule is read-only here;
  * per-track independent keyframes, sun anchors, and easings (M2/M3);
  * WebSocket override detection — we only *apply*, never *observe* manual
    changes (resume policy is M2);
  * multiple lights per schedule and the capability matrix (M2/M3).

The on-disk config format is intentionally simple and not yet user-facing, so it
is safe to evolve as the editor lands.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# Paths & environment
# --------------------------------------------------------------------------- #
DATA_DIR = os.environ.get("LS_DATA_DIR", "/data")
OPTIONS_PATH = os.path.join(DATA_DIR, "options.json")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
INGRESS_HOST = os.environ.get("LS_INGRESS_HOST", "0.0.0.0")
INGRESS_PORT = int(os.environ.get("LS_INGRESS_PORT", "8099"))
TICK_SECONDS = int(os.environ.get("LS_TICK", "30"))

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def log(msg):
    print("[light-scheduler] %s %s" % (datetime.now().isoformat(timespec="seconds"), msg), flush=True)


# --------------------------------------------------------------------------- #
# Home Assistant REST client (Supervisor-proxied core API)
# --------------------------------------------------------------------------- #
def _ha_request(method, path, body=None):
    """Call the core HA API through the Supervisor and return parsed JSON."""
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
    """Call a HA service.

    The Supervisor core API proxy at /api/ forwards service calls to
    /api/services/{domain}/{service}.  The body is the raw service data
    (not wrapped in a 'data' key).
    """
    _ha_request("POST", "services/" + domain + "/" + service, data)


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
    +/- offset) are a later milestone and will extend this function.
    """
    if isinstance(when, str) and ":" in when:
        hh, mm = when.split(":")[:2]
        return day.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    raise ValueError("Unsupported keyframe 'when': %r" % (when,))


def default_schedule():
    """Built-in fixed demo used when /data/config.json is absent.

    08:00 -> on  30%  4000K   (5 min wake ramp)
    18:00 -> on  10%  2700K   (5 min evening dim)
    23:00 -> off          (30 s fade)
    """
    return {
        "id": "demo",
        "name": "Demo (fixed 2-ramp)",
        "mode": "daily",
        "keyframes": [
            {"when": "08:00", "power": True, "brightness_pct": 30, "kelvin": 4000, "ramp": 300},
            {"when": "18:00", "power": True, "brightness_pct": 10, "kelvin": 2700, "ramp": 300},
            {"when": "23:00", "power": False, "brightness_pct": None, "kelvin": None, "ramp": 30},
        ],
    }


def load_schedule():
    """Load the schedule from /data/config.json, else the built-in demo."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        sched = cfg.get("schedule", cfg)
        if sched.get("keyframes"):
            return sched
    except (OSError, ValueError):
        pass
    return default_schedule()


def resolve_light():
    """Target light: the exported LS_LIGHT option, else /data/options.json."""
    light = os.environ.get("LS_LIGHT", "").strip()
    if light:
        return light
    try:
        with open(OPTIONS_PATH, "r", encoding="utf-8") as fh:
            return (json.load(fh).get("light") or "").strip()
    except (OSError, ValueError):
        return ""


# --------------------------------------------------------------------------- #
# Shared application state (scheduler thread writes, HTTP handler reads)
# --------------------------------------------------------------------------- #
class AppState:
    """Holds the current schedule and the last applied keyframe index."""

    def __init__(self, light, schedule):
        self.light = light
        self.schedule = schedule
        self.next_kf_idx = 0
        self.running = True

    def next_keyframe(self, now):
        """Advance to the next keyframe whose time is <= *now*.

        Returns the keyframe dict, or None if no more keyframes apply today.
        """
        kfs = self.schedule["keyframes"]
        while self.next_kf_idx < len(kfs):
            kf = kfs[self.next_kf_idx]
            target = parse_when(kf["when"], now.date())
            if target <= now:
                self.next_kf_idx += 1
                return kf
            self.next_kf_idx += 1
        return None

    def reset(self):
        """Reset for the next day."""
        self.next_kf_idx = 0


# --------------------------------------------------------------------------- #
# Scheduler loop
# --------------------------------------------------------------------------- #
def scheduler_loop(app):
    """Main scheduler: fires keyframes at their scheduled times."""
    while app.running:
        try:
            now = datetime.now()
            kf = app.next_keyframe(now)
            if kf is not None:
                power = kf.get("power", True)
                brightness = kf.get("brightness_pct")
                kelvin = kf.get("kelvin")
                transition = kf.get("ramp", 0)
                ok = ha_set_light(app.light, power, brightness, kelvin, transition)
                log(
                    "fired keyframe '%s' -> %s (light=%s ok=%s)"
                    % (kf["when"], {"power": power, "brightness_pct": brightness, "kelvin": kelvin}, app.light, ok)
                )
            else:
                # No more keyframes today — reset for tomorrow
                app.reset()
            time.sleep(TICK_SECONDS)
        except Exception as exc:
            log("tick error: %s" % exc)
            time.sleep(TICK_SECONDS)


# --------------------------------------------------------------------------- #
# Ingress HTTP server
# --------------------------------------------------------------------------- #
class IngressHandler(BaseHTTPRequestHandler):
    """Minimal HTTP server for the ingress panel.

    Serves index.html on / and exposes /api/state for the SPA to poll.
    """

    app: AppState  # type: ignore

    def log_message(self, fmt, *args):
        """Suppress per-request logs."""

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            try:
                with open(INDEX_PATH, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError as exc:
                self._send_json(500, {"error": str(exc)})
        elif self.path == "/api/state":
            state = {
                "light": self.app.light,
                "schedule": self.app.schedule.get("name", ""),
                "next_keyframe_index": self.app.next_kf_idx,
                "keyframes_count": len(self.app.schedule.get("keyframes", [])),
            }
            self._send_json(200, state)
        else:
            self.send_response(404)
            self.end_headers()


def start_ingress(app):
    """Start the ingress HTTP server."""
    IngressHandler.app = app  # type: ignore
    server = ThreadingHTTPServer((INGRESS_HOST, INGRESS_PORT), IngressHandler)
    log("ingress listening on %s:%d" % (INGRESS_HOST, INGRESS_PORT))
    server.serve_forever()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    """Entry point: resolve config, start scheduler + ingress."""
    light = resolve_light()
    schedule = load_schedule()
    app = AppState(light, schedule)

    log(
        "starting: light=%s schedule=%r keyframes=%d tick=%ds"
        % (light, schedule.get("name", ""), len(schedule.get("keyframes", [])), TICK_SECONDS)
    )

    if not light:
        log("WARNING: no light configured — set the app 'light' option. Scheduling is idle until then.")

    # Start scheduler thread
    sched_thread = threading.Thread(target=scheduler_loop, args=(app,), name="scheduler")
    sched_thread.start()

    # Start ingress (blocks)
    start_ingress(app)


if __name__ == "__main__":
    main()