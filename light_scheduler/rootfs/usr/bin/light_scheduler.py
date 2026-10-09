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
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


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
    if power:
        fields = {"entity_id": entity, "transition": transition}
        if brightness_pct is not None:
            fields["brightness_pct"] = brightness_pct
        if kelvin is not None:
            fields["kelvin"] = kelvin
        _ha_request("POST", "services/light/turn_on", fields)
    else:
        _ha_request("POST", "services/light/turn_off", {"entity_id": entity, "transition": transition})
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
    def __init__(self, light, schedule):
        self.lock = threading.Lock()
        self.light = light
        self.schedule = schedule
        self.day = None              # current calendar date (str)
        self.applied = set()         # keyframe indices already fired for the day
        self.started = False
        self.last_applied = None     # {when, value, at}
        self.next = None
        self.active = bool(light)

    def keyframe_times(self, day):
        kfs = self.schedule.get("keyframes", [])
        out = []
        for i, kf in enumerate(kfs):
            try:
                out.append((i, parse_when(kf.get("when"), day)))
            except ValueError:
                continue
        return out

    def tick(self, now):
        with self.lock:
            day = now.date().isoformat()
            times = self.keyframe_times(now)

            # New day (or first run): catch up without spurious bursts — fire
            # only the most recent elapsed keyframe, mark the rest stale.
            if not self.started or self.day != day:
                self.day = day
                self.started = True
                past = [i for i, t in times if t <= now]
                self.applied = set(past)
                if past:
                    self._fire(past[-1], times, now)
            else:
                for i, t in times:
                    if i not in self.applied and t <= now:
                        self.applied.add(i)
                        self._fire(i, times, now)

            # Refresh the "next keyframe" hint for the panel.
            upcoming = [(i, t) for i, t in times if t > now]
            self.next = {
                "when": self.schedule["keyframes"][upcoming[0][0]].get("when"),
                "value": self._value(upcoming[0][0]),
            } if upcoming else None

    def _fire(self, idx, times, now):
        kf = self.schedule["keyframes"][idx]
        ok = ha_set_light(
            self.light,
            power=bool(kf.get("power")),
            brightness_pct=kf.get("brightness_pct"),
            kelvin=kf.get("kelvin"),
            transition=int(kf.get("ramp", 0)),
        )
        self.last_applied = {"when": kf.get("when"), "value": self._value(idx), "at": now.isoformat(timespec="seconds"), "ok": ok}
        log("fired keyframe %r -> %s (light=%s ok=%s)" % (kf.get("when"), self._value(idx), self.light or "<none>", ok))

    def _value(self, idx):
        kf = self.schedule["keyframes"][idx]
        return {k: kf.get(k) for k in ("power", "brightness_pct", "kelvin")}

    def current_from_ha(self):
        st = ha_get_state(self.light)
        if not st:
            return None
        if "_error" in st:
            return {"state": "error", "detail": st["_error"]}
        attrs = st.get("attributes", {})
        brightness = attrs.get("brightness")
        return {
            "state": st.get("state"),
            "brightness_pct": round(brightness / 255 * 100) if brightness else None,
            "kelvin": attrs.get("color_temp_kelvin"),
            "color_mode": attrs.get("color_mode"),
        }

    def get_state(self):
        with self.lock:
            return {
                "light": self.light or None,
                "active": self.active,
                "schedule": {k: self.schedule.get(k) for k in ("id", "name", "mode")},
                "keyframes": self.schedule.get("keyframes", []),
                "last_applied": self.last_applied,
                "next": self.next,
                "current": self.current_from_ha(),
            }

    def get_config(self):
        with self.lock:
            return {"light": self.light or None, "schedule": self.schedule}


# --------------------------------------------------------------------------- #
# Ingress HTTP server
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    app = None  # set to the shared AppState

    def log_message(self, *args):  # keep the app log clean
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._json({"error": "index.html not found"}, 500)
        elif path == "/api/state":
            self._json(self.app.get_state())
        elif path == "/api/config":
            self._json(self.app.get_config())
        else:
            self._json({"error": "not found"}, 404)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    light = resolve_light()
    schedule = load_schedule()
    state = AppState(light, schedule)
    Handler.app = state

    log("starting: light=%s schedule=%r keyframes=%d tick=%ds"
        % (light or "<none>", schedule.get("name"), len(schedule.get("keyframes", [])), TICK_SECONDS))
    if not light:
        log("WARNING: no light configured — set the app 'light' option. Scheduling is idle until then.")

    # Ingress server runs in a daemon thread; the scheduler owns the main loop.
    server = ThreadingHTTPServer((INGRESS_HOST, INGRESS_PORT), Handler)
    threading.Thread(target=server.serve_forever, name="ingress", daemon=True).start()
    log("ingress listening on %s:%d" % (INGRESS_HOST, INGRESS_PORT))

    try:
        while True:
            try:
                state.tick(datetime.now())
            except Exception as exc:  # never let one bad tick kill the app
                log("tick error: %r" % (exc,))
            time.sleep(TICK_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        log("shutting down")


if __name__ == "__main__":
    main()
