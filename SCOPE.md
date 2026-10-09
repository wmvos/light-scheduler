# Light Scheduler — Project Scope

**Version:** 0.2 (draft) · **Date:** 2026-10-08 · **Status:** awaiting review

A Home Assistant **App** (formerly "add-on") that drives your lights through the
day — on/off, brightness, color temperature, and (later) color — using a
**visual timeline editor** that works like a DAW/video NLE.

> **Terminology.** We use *App* (Home Assistant renamed add-ons to apps,
> Oct 2026). "Add-on" and "app" are the same thing. Distribution: a community
> app repository (GitHub); the user is on **Home Assistant OS**, where Apps are
> available via **Settings → Apps**.

---

## 1. The problem & the gap we beat

Setting lights by hand each morning/evening is a small daily chore, and the
"right" light depends on the time of day and the season. **Adaptive Lighting**
(basnijholt) does the "sun-synchronized" part well, but its model is a
*global integration*: a few global settings, an optional per-light
"brightness mode," a `set_manual_control` service, and **no per-room, per week
editor and no UI timeline**. Its config is YAML you must hand-maintain.

**Light Scheduler takes the same goal and makes it tangible:**

- **A visual, NLE-style editor** (top track = on/off, then one row each for
  brightness, temperature, color) — you *see* the day as a waveform and drag
  keyframes, instead of reading YAML.
- **Per-schedule keyframes** on a **daily or weekly** grid — one lamp can be in
  several schedules as long as their windows don't overlap *for the same
  characteristic* (so a bedroom lamp can have an independent *on/off* schedule
  and an independent *brightness* schedule).
- **Real, per-property time control**: we send the lamp a *target value plus a
  ramp time*, so the lamp itself glides smoothly (like ESPHome) — no staccato
  "set the lamp every 20 s" chatter. This is the key "better than AL" behavior.
- **Capability-aware**: on/off everywhere; brightness on most; temperature on
  CTT-capable bulbs; color on RGB-capable ones. Each characteristic degrades
  gracefully instead of assuming everything is a Hue.

**Positioning (one line):** *Adaptive Lighting automates a "sun curve" for you;
Light Scheduler gives you an editor to design any curve, per room, per week, and
lets your lamps do the fading for real.*

## 2. Goals

| # | Goal |
|---|------|
| G1 | Design a per-room / per-lamp day (or week) of on/off, brightness, and color temperature in a visual timeline — no per-transition automations, no YAML. |
| G2 | Anchor keyframes to **clock times** or **sun events** (sunrise/sunset ± offset). |
| G3 | Smooth, real ramping: the lamp fades over the interval between keyframes (we pass `transition`), not stepped re-sets. |
| G4 | Never fight the user: detect manual overrides **in real time** (websocket `states` subscription) and back off gracefully. |
| G5 | Survive restarts (HA, app, network) and recover state correctly. |
| G6 | Installable from a personal app repository; a first-class UI inside HA via **ingress**; zero custom code inside HA core. |
| G7 | Work with *any* HA light entity, respecting each bulb's real capabilities. |

## 3. What "better than Adaptive Lighting" concretely means

| Dimension | Adaptive Lighting | Light Scheduler |
|---|---|---|
| Form factor | Custom integration (runs inside HA core) | **App** (isolated container, own process, own logs, isolated restarts) |
| Configuration | YAML + a few global options | **Visual timeline UI** (drag keyframes, preview the day) |
| Scope of control | One global sun-curve, optional per-light mode | **Independent schedules** per room/lamp, **daily or weekly** |
| Transition | `transition` in seconds per service call; app re-sets state on a timer | **Per-property target + ramp time**; each characteristic can ramp on its own time; lamp does the fading |
| Manual control | `set_manual_control` service; global `transition_until_sleep` | **Real-time** override detection + per-characteristic, per-schedule resume policy (from HA events, not a service you must remember to call) |
| Color | Global CTT/RGB sun curves | Per-keyframe temperature **and** (later) RGB, mixed safely per bulb capability |
| Runs | Inside HA core (a bug risks HA) | Isolated (a bug only restarts this app) |

**We inherit Adaptive Lighting's hard-won lessons** and must not relearn them
the wrong way:

- Its `set_manual_control` + auto-reset is the *concept* for G4; we implement it
  with live websocket state instead of a service call.
- Its `tanh` vs `linear` brightness modes exist because **naïve linear ramps look
  awful at low brightness** (perceived brightness is non-linear). We adopt the
  same "perceptual" option (see D3 / §6).
- Its Zigbee/Z-Wave "mesh can't keep up with rapid updates" caveat maps directly
  to our ramp-strategy choice (D5).

## 4. MVP scope

### 4.1 Scheduling engine

- **Schedule** = a set of **characteristic tracks**, each a list of **keyframes**.
  Tracks: `on/off` (bool), `brightness` (0–100 %), `temperature` (kelvin), and
  `color` (RGB/xy — *Phase 3, designed for now*).
- **Keyframe** = `{ when, value, ramp }`:
  - `when`: `HH:MM` **or** `sunrise|sunset|solar_noon [± <offset>]` (D9).
  - `value`: the target for that characteristic.
  - `ramp`: *how long to fade to this value* (D5). Default = the time to the
    next keyframe; can be overridden per keyframe.
- **Interpolation:** within a ramp interval the *value follows an easing curve*
  (D3) — MVP ships `linear`, `cubic`, `ease-in`, `ease-out`, `smoothstep`, plus a
  **perceptual-brightness** toggle (D3).
- **Ramping model (D5, the core "better" mechanism):** the engine sends the lamp
  **one** `light.turn_on` with the *target* value and a `transition` = the ramp
  time, and then *trusts the lamp* to fade. It does **not** re-set the value
  every tick. A lightweight watch loop only *verifies* the lamp arrived and
  *re-issues* if it didn't (mesh resilience). → **This makes the per-property
  "cut the keyframe" backend idea (your note on mixed transition speeds) a
  non-problem at MVP**: we never compute per-tick intermediate values per
  property; each property just has its own (value, ramp) pair sent once.
- **Capability awareness (G7):** per entity, read supported features once and
  cache. A `temperature` keyframe on a non-CTT bulb → skipped with a one-time
  warning; a `color` keyframe on a CTT-only bulb → skipped. Never send a
  property a bulb can't do.
- **`on/off` gate:** brightness/temperature/color only apply while the lamp is
  `on`; an `off` keyframe wins and clears pending property ramps for that lamp.

### 4.2 Light targeting

- A **light** (or a **group**) is *assigned* to a schedule, per characteristic.
  A dropdown lists all `light.*` / `light.group` entities (resolved from HA).
- **Conflict rule (your requirement):** one lamp may appear in multiple
  schedules, but at any instant **at most one schedule owns a given
  characteristic** of that lamp. The engine validates this on save and in the UI
  (overlaps highlighted), with a clear "these two both control brightness of
  `light.kitchen` 17:00–19:00" error. Daily schedules are non-overlapping in the
  day; weekly schedules are non-overlapping in the week **and** the day.

### 4.3 User-override protection (G4) — now real-time

- Subscribe to **`states`** changes for all assigned entities over the **HA
  WebSocket API** (Supervisor proxy). No polling.
- Classify each change as *app-initiated* (we just told it to) or *external*
  (user, other automation, wall switch).
- On an external change to a schedule-owned characteristic, that characteristic
  goes **dormant** for that lamp. Resume policy is per-characteristic
  (D10): `resume_next_on` (default) | `hold_until_on` | `dormant_until_midnight`
  | `always`. A dormant characteristic is never re-driven; the user is in charge
  until the policy resumes it.
- Because this is event-driven, "you flipped it off in the evening" is handled
  within milliseconds, with zero extra API chatter.

### 4.4 The UI (requirement — this forces the App + ingress)

A single-page app served through **ingress** (own HA sidebar panel,
`panel_icon`/`panel_title`; auth is HA's; no ports, no tokens — D8). The editor:

- **Day / Week view toggle.** Day = a 24 h horizontal track; Week = 7 day-
  columns (weekly schedules live here).
- **Tracks (rows), top-to-bottom, exactly your model:**
  1. **on/off** — a binary lane (on segments as filled blocks, off as gaps).
  2. **brightness** — a filled area chart of 0–100 % over the day.
  3. **temperature** — a line/area of kelvin (color-shaded warm→cool).
  4. **color** (Phase 3) — a swatch strip over the day.
- **Keyframe editing** like NLE software: click a track to add a keyframe, drag
  to move (time axis) and reshape (value), click a segment to change its ramp
  and easing, snap to grid (1/4 h) and to sunrise/sunset markers, delete.
- **Live preview:** a "play the day" scrubber that animates the computed curve
  and shows a lamp swatch at any moment; and a **"now" cursor** showing what's
  happening today, per assigned lamp.
- **Assignment:** add/remove lights/groups to a schedule via an entity dropdown
  (searchable), with the overlap-conflict validation of §4.2 shown inline.
- **Read/write the config through the app backend** (not the Supervisor options
  form — see D2). The UI is the config surface.

### 4.5 Configuration storage

- Schedules are stored as **JSON in the app's persistent `/data`** volume,
  written/read by the app backend. The UI edits this file (with optimistic
  concurrency / version stamping). The Supervisor **options** form is only for
  a few *global* knobs (tick interval, default override policy, app log level) —
  **not** for schedules (D2). This replaces the earlier "YAML in File Editor"
  idea: the UI *is* the editor, and JSON lives in the app's own sandbox, so a bad
  save never touches HA core config.

### 4.6 Resilience (G5)

- Token via `SUPERVISOR_TOKEN`; all HA traffic via the Supervisor proxy.
- Exponential backoff + full reconnect on HA/network drops; websocket
  re-subscribes, then does a state *read-through* to resync (lights may have
  moved while disconnected).
- **Catch-up, not force (D5):** after a long outage, apply the *current* target
  values on reconnect rather than replaying a history of stale calls.

### 4.7 Observability

- Structured logging through the Supervisor log viewer (app logs + a
  "last applied / current target" line per lamp).
- In-UI **status** tab: current computed vs actual per lamp, dormant flags, and
  an error ring (failed service calls, validation errors).

### 4.5 Mode profiles (D14) — named fixed-value overrides

A **mode profile** is a user-invokable preset that snaps one or more assigned
lights to a fixed set of values, overriding the schedule for a defined period.
Canonical examples: **Night** (get up for a bathroom trip → a gentle, dim, warm
setback), **Party** (full brightness), **Movie** (very dim, cool-ish).

- **Shape:** `profile = { name, values: {power, brightness_pct, kelvin | rgb}, resume, scope }`.
  `values` reuses the exact same fields as a keyframe, so the UI and engine need
  one new concept("a named snapshot") — not a new value model.
- **Resume** reuses the D10 override-policy enum, so manual changes and
  profiles share one mechanism:
  - **`hold`** — stays until the user turns it off (Party).
  - **`resume_after_duration`** — applies for `N`, then the schedule resumes
    (Night: +15 min → back to off).
  - **`resume_next_on`** (default) — applies until the next `on` keyframe fires.
- **Scope:** per-light or whole-schedule. M2+ multi-light schedules make
  whole-schedule relevant (Party = every lamp in the room). Single-light MVP is
  trivially per-light.
- **Interaction rule:** an *active* profile **suspends** the schedule for the
  affected lights — the engine sends profile values, not the daily curve, and
  re-syncs (catch-up, D5) the moment the profile ends. Because M2 drives lights
  from `resolve(schedule, t)` (Appendix C), a profile is just a *temporary
  override of the schedule at `resolve()`*, and the UI can preview it the same
  way.
- **Not in MVP:** profiles depend on D10 resume + M2's resume handling, so they
  land **in M4** alongside weekday/weekend + presence pausing ("richer
  behavior"). The concept is locked now (D14) so the M2 `resume` field is
  designed profile-ready.

### Explicitly **not** in MVP

- **Color** scheduling (engine designed for it; ships Phase 3).
- **Monthly** schedules (backlog, per your note).
- Presence/occupancy-aware pausing (Phase 4).
- Weekday/weekend *variants* (Phase 4) — weekly is in MVP, but auto weekday/
  weekend duplication is later.
- A full "sun-curve generator" preset (nice, but optional — AL covers the sun
  case; we can add a "generate from sunrise/sunset" one-click later).
- Energy tracking, voice control, mobile app, non-light entities.

## 5. Architecture

```
┌──────────────────────────── HA OS / Supervisor ────────────────────────────┐
│                                                                             │
│  Home Assistant core                 ┌───────────────────────────────────┐  │
│   • REST + WebSocket APIs ◄──────────┤  Light Scheduler App (Docker)     │  │
│   • sun entity                        │  Python + asyncio                 │  │
│   • light services                    │  ┌──────────────────────────────┐ │  │
│   • states (subscribable)             │  │ Scheduler engine             │ │  │
│                                        │  │  • keyframe + ramp math     │ │  │
│  UI (browser / mobile)                 │  │  • websocket override detect │ │  │
│   └─ ingress ─────────────────────────►│  │  • capability cache          │  │
│      (HA handles auth;                │  │  │ HA REST + WS client        │  │
│       app serves SPA)                 │  │  │ config store (/data JSON)  │  │
│                                        │  │  │ SPA static + small API    │  │
│   App settings form                    │  │  └────────────────────────────┘ │  │
│   (global options only)                │  └────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Process:** one slim Python 3.12 asyncio app in an Alpine Docker image
  (pinned `ghcr.io/home-assistant/base:<ver>`). S6 overlay for restart.
- **HA access:** `homeassistant_api: true` → `SUPERVISOR_TOKEN` bearer; REST via
  `http://supervisor/core/api/`, **WebSocket via `ws://supervisor/core/websocket`**.
- **UI:** `ingress: true` (+ `panel_icon`, `panel_title`, `panel_admin`); SPA on
  internal port **8099**, allowing only `172.30.32.2`. Static assets + a tiny
  JSON API (read/save config, current state, "apply now", validation).
- **Time:** container clock (Supervisor sets the host TZ; DST handled by host)
  for `HH:MM`; sun events read from `sun.sun`. (If the sun entity is unavailable
  before HA starts, fall back to a built-in solar calc from a lat/lon option — D9.)

### Key decisions

| # | Decision | Choice | Rationale / trade-off |
|---|----------|--------|----------------------|
| D1 | App vs custom integration | **App** (your call; the UI forces it anyway) | Isolated process (a bug restarts the app, not HA), own ingress UI, standard install. Cost: talks to HA over REST/WS proxy (not in-process) — but WS subscriptions remove the old "polling" penalty, so the cost is now negligible. |
| D2 | Config surface | **UI is the config editor**; config persisted as **JSON in `/data`**. Supervisor options = globals only. | Schedules in a Supervisor options form are unusable; a file-in-File-Editor is a stepping stone we now skip straight past because the UI is a hard requirement. JSON in the app sandbox is versionable and crash-safe (validated before write). |
| D3 | Easing | MVP ships **5 named easings**: `linear`, `cubic`, `ease-in`, `ease-out`, `smoothstep`; + a **perceptual-brightness** mode (perceived-linear via a gamma/tanh-like curve). | Your "pick three now" → we ship the three you named plus two obvious ones (`smoothstep` is the natural default, `ease-in`/`ease-out` are explicit requests). The perceptual mode is what makes low-brightness ramps *look* right (the exact thing AL added `tanh` for). Easings are per-keyframe so the UI can visualize any of them later. |
| D4 | Status UI | **Same ingress SPA** (tab), not a second panel | One ingress panel hosts editor + status; simpler, one security surface. |
| D5 | Ramp strategy | **Target + `transition`, trust the lamp** (ESPHome-style); watch+reissue only on failure; **catch-up-not-force** after outages. | This is the "improve transition esp. at low brightness" win and it *removes* the future "cut keyframes per property" backend hazard: we never compute per-tick values, each property is a single (target, ramp) instruction, so brightness and temperature ramping on independent times is just two independent service params — no backend keyframe slicing. |
| D6 | Brightness unit | **Percent** in config/UI; send `brightness_pct`. | HA exposes 0–100 %; avoids 0–255 confusion and is human-readable on a chart. (HA maps % → 0–255 for the bulb.) |
| D7 | Distribution | **Personal app repository** (GitHub) + **pre-built multi-arch images** via HA GitHub Actions. | Standard, self-hosted, fast installs. (Locally-built images are for early experiments only; we go straight to the GHCR/builder path.) |
| D8 | UI delivery | **Ingress** (in-HA panel). | The spec's recommended, secure way: HA auth, no ports, works on mobile/remote. This is *why* it must be an App. |
| D9 | Sun anchors | `sunrise|sunset|solar_noon ± offset`, resolved from `sun.sun`; built-in solar calc fallback. | Covers "follow the season" without hardcoding times. |
| D10 | Override policy (incl. auto-resume) | Per-characteristic, per-schedule; default **`resume_next_on`** (auto-resume). | **Decided (user) for the default.** A single global policy (AL-style) fights real usage; per-characteristic matches your model (on/off and brightness resume independently). Default `resume_next_on`: you flip a lamp off → it stays off → the schedule quietly reclaims it the next morning on its `on` keyframe, honoring any manual changes in between. Alternatives: `hold_until_on` / `dormant_until_midnight` / `always`. |
| D11 | Characteristic isolation | **Temperature and color are mutually exclusive per keyframe** (a keyframe sets *one or the other*, not both). | Your note: mixing CTT+RGB "always looks off" because bulbs implement them differently. A keyframe is either `{temperature: 2700}` *or* `{color: [r,g,b]}`. A *lamp* that supports both just never gets them in the same step. |
| D12 | **Reference target** | **ESPHome + WLED** (WW/CW, RGB, RGB+CTT) over **LAN**; no mesh. MVP ramp strategy tuned for these. | **Decided (user):** the design target is ESPHome/WLED with no mesh. `transition` is natively honored, and these expose both CTT *and* RGB — so the "temperature XOR color" keyframe rule (D11) is actually *exercised* (a bulb can do both, but not in one step). **Mesh = Phase-5 concern only** (no one can test it yet; revisit only if adopters on mesh report issues). |
| D13 | **Target entities** | **Individual `light.*` only** in MVP. Groups/zones explicitly out. | **Decided (user).** Keeps the model + UI + capability logic clean. HA itself degrades gracefully for groups (a `turn_on` to a group applies what each member supports), but we don't rely on that ambiguity for per-bulb capability — so we steer clear of groups until needed. |
| D14 | **Mode profiles** | Named fixed-value presets (Night, Party, Movie) that override the schedule for a defined resume, reusing the D10 policy + the keyframe value shape. | **Proposed (user idea, 2026-10).** It is *the same override mechanism* the schedule already has — a profile is a user-triggered snapshot, not a new value model. Kept out of MVP; designed so M2's `resume` is profile-ready. Lands M4 (§4.5). |

## 6. Milestones

| Phase | Name | Contents | Exit criteria |
|-------|------|----------|---------------|
| **M0** | Scope & architecture | This document; answer open questions (§7). | Scope approved; App-vs-integration and ingress decisions locked. |
| **M1** | Skeleton ("it talks, it shows") | App boilerplate (Docker, S6, `config.yaml` with `ingress`+`homeassistant_api`); reads `SUPERVISOR_TOKEN`; connects HA REST **and** WebSocket; ingress serves a placeholder SPA; turns **one hardcoded light** on a fixed 2-keyframe ramp (target+transition); logs visible in HA. | Installed from a personal repo on real HA OS; a lamp fades on a curve; ingress panel opens in the HA sidebar. |
| **M2** | **MVP engine + editor** | Full §4: keyframe tracks (on/brightness/temperature), ramp+easing math, **websocket override detection + resume policies**, capability awareness, daily **and** weekly schedules, conflict validation, JSON config store, and the **NLE-style UI** (day/week, on/off lane, brightness area, temperature line, keyframe drag, live preview, "now" cursor, light-assignment dropdown). | A real day/week runs unattended in your home; manual toggles are respected in real time; everything is editable in the UI with zero YAML. |
| **M3** | Color | RGB/xy color track + swatch strip; perceptual color ramp; CTT↔color safe per D11; "generate from sun" one-click preset. | You can design a color-of-day in the UI and it ramps smoothly on RGB-capable bulbs. |
| **M4** | Richer behavior | Weekday/weekend variants, presence/occupancy pause, per-lamp time offsets, template/macros (reuse a "warm evening" block across rooms). | Weekdays vs weekends differ; leaving home pauses; macros exist. |
| **M5** | Hardening & release | Soaking, docs, changelog, custom AppArmor profile (security points), stable + canary branches, (optionally) app-store listing. | One month unattended; documented install; good app-store rating. |

## 7. Open questions (answer before M1)

1. **Which lamps/integrations do you actually run** (Hue, Zigbee2MQTT, Z-Wave,
   Tasmota, ESPHome, …)? This decides how much we lean on `transition` (mesh
   radios handle long fades badly) and whether we need a per-integration ramp
   strategy (D5) — and what the color Phase 3 must support. *My working
   assumption: a mix, some CTT, some RGB, at least one mesh-based.*
2. **Mesh/limit:** do any of your "always-on ambient" bulbs sit on a congested
   Zigbee/Z-Wave mesh? If so I'll default those to *coarser* ramps (fewer
   reissues) to avoid the "lamp stops responding" failure AL documents.
3. **Override default (D10):** confirm `resume_next_on` — i.e., you flip a lamp
   off in the evening, it stays off, and the schedule quietly reclaims it the
   next morning on its `on` keyframe? Or do you prefer it *never* reclaims until
   you re-enable?
4. **Weekly vs daily as the default grid:** when you create a new schedule, should
   the default canvas be **daily** (24 h, repeats every day) with weekly as an
   opt-in, or weekly by default? (I'd default to daily; weekly adds the week axis
   only when a day needs to differ.)
5. **Scope of "any light":** for MVP do you want us to target **individual
   `light.*` entities only**, or also **`light.group` / zones** in the same drop-
   down? (We can support both, but groups + per-bulb capability differs; I'll
   confirm the model.)

## 8. Risks & mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Mesh bulbs can't keep up with long/rapid ramps (AL's documented failure) | Medium | Per-integration ramp policy; watch+reissue with backoff; cap ramp rate for known mesh integrations. |
| Two independent property ramps (D5) desync on a flaky bulb (brightness arrives, kelvin doesn't) | Low | One `turn_on` carries both params together, so a bulb applies them as one transition; watch loop reconciles actual vs target per property. |
| Sun-anchored keyframes collide/cross as seasons swing (your note) | Medium | Validation detects resulting *time-order* violations (a keyframe whose resolved time inverts the track) and warns in the UI; per-keyframe override to absolute time; sun anchors clamp to sane bounds. |
| Ingress UI complexity (SPA + API in one app) | Medium | Lean stack (static SPA + small JSON API over the same 8099 server); engine and UI share a pure "schedule→(value, ramp)" model so the UI can preview without the engine. |
| App talks to HA via proxy only (no in-process access) | Low | WS subscriptions make override detection real-time; only multi-second operations (config save) are REST. Proxy is the sanctioned, stable path. |
| HA/Supervisor API drift | Low | Pin a min `homeassistant:` version in `config.yaml`; feature-detect; clear logs; App restarts in isolation. |
| Scope creep toward "general automation app" | Medium | Non-goals list is binding through M2; new ideas → §9. |

## 9. Ideas park (deliberately out of scope)

- **Monthly schedules** (your noted edge case) — a month grid; big UI effort.
- **Sun-curve generator** — "make a curve that follows the sun like AL does" as a one-click starter, then editable.
- Daylight *matching* (mirror outdoor luminance through windows).
- **Mode profiles** (Night / Party / Movie) — user-triggered fixed-value presets overriding the schedule. Now a first-class concept (D14 / §4.5); implementation lands M4. (Was the old "per-activity profiles" note.)
- Per-integration ramp presets as shareable "materials."
- Public app-store listing / stable+canary branches.
- Optional: an **MQTT** output (so ESPHome lamps can subscribe and self-ramp) —
  the app already has the `mqtt` service available via the Services API.

---

## Appendix A — proposed repository layout (current App conventions)

A single-app Supervisor **App** repository. `repository.yaml` sits at the repo
root (required keys: `name`, `url`, `maintainer`); the app lives in its own
folder whose root holds `config.yaml` (the manifest).

```
light-scheduler/                      # GitHub repo root
├── repository.yaml                   # store manifest: name, url, maintainer
├── LICENSE                           # GPL-3.0
├── README.md                         # project intro
├── SCOPE.md                          # this file
├── CHANGELOG.md
└── light_scheduler/                  # the App folder (folder name = slug)
    ├── config.yaml                   # manifest at folder root: name, version,
    │                                 #   slug, description, url, arch: [aarch64, amd64],
    │                                 #   init: false, map, options + schema,
    │                                 #   homeassistant_api, ingress, ingress_port,
    │                                 #   panel_icon / panel_title / panel_admin
    ├── Dockerfile                    # FROM ghcr.io/home-assistant/base:<pinned>
    ├── icon.png / logo.png           # store/panel icons (added before release)
    ├── README.md / DOCS.md / CHANGELOG.md
    ├── translations/en.yaml
    └── rootfs/
        ├── etc/services.d/light_scheduler/{run,finish}   # s6
        └── usr/bin/light_scheduler.py                   # the app (Python 3.12)
        # (+ static ingress SPA assets, when added in M2)
```

- **No app-level `build.yaml`** — the builder workflow lives in
  `.github/workflows/builder.yaml` (plus a per-app `build-app.yaml`), added at
  **M5** per D7. Until then there is no `image:` in `config.yaml`, and we install
  from `/local_apps` (Samba → App store → Local apps).
- **Pinned `FROM`** (current spec, Supervisor ≥ 2026.04): set an explicit
  `FROM ghcr.io/home-assistant/base:<pinned>` in the Dockerfile — the old implicit
  `BUILD_FROM` default is gone. With HA's builder actions you don't hand-write the
  `io.hass.*` image labels; they're stamped at build time.
- **s6 layout:** `rootfs/etc/services.d/light_scheduler/{run,finish}` +
  `rootfs/usr/bin/<program>`; `init: false` in `config.yaml` so the base image's
  s6-overlay supervises the program (with restart on crash).

## Appendix B — config model (draft JSON)

```jsonc
{
  "version": 3,
  "schedules": [
    {
      "id": "living-room",
      "name": "Living room",
      "mode": "daily",                      // "daily" | "weekly"
      "lights": ["light.living_left", "light.living_right"],
      "override_policy": "resume_next_on",  // per-characteristic, D10
      "tracks": {
        "power": [
          { "when": "sunrise+15m", "value": true,  "ramp": "instant" },
          { "when": "sunset+2h30m","value": false, "ramp": "30s" }
        ],
        "brightness": [
          { "when": "sunrise+15m", "value": 10,  "ramp": "5m",  "ease": "perceptual" },
          { "when": "08:00",       "value": 80,  "ramp": "10m", "ease": "linear"   },
          { "when": "sunset-30m",  "value": 40,  "ramp": "20m", "ease": "smoothstep" }
        ],
        "temperature": [
          { "when": "sunrise+15m", "value": 2700, "ramp": "5m",  "ease": "cubic" },
          { "when": "08:00",       "value": 4000, "ramp": "10m", "ease": "cubic" },
          { "when": "sunset-30m",  "value": 2700, "ramp": "20m", "ease": "cubic" }
          // color (Phase 3): { "when": "21:00", "color": [255,120,0], ... } —
          // D11: a keyframe sets temperature OR color, never both.
        ]
      }
    }
  ],
  "defaults": { "tick_seconds": 20, "snap_minutes": 15 }
}
```

**Semantics**

- `when` = `HH:MM`, or `sunrise|sunset|solar_noon ± <dur>` (weekly adds a day,
  e.g. `"sat 08:00"`).
- `value` omitted on a keyframe = "stop changing this characteristic" (holds the
  last value until the next keyframe that sets it); a `power:false` keyframe may
  omit everything else.
- `ramp` = how long to fade to `value` (`"instant" | "30s" | "5m" | …`). When
  omitted, defaults to the time until the next keyframe — so a curve reads as
  "fade from A to B over the gap."
- `ease` = D3 easing; `perceptual` = the low-brightness-corrected curve.
- **Validation (saved on write):** per lamp, per characteristic, no two schedules
  own the same characteristic in the same time window; resolved keyframe times
  must stay strictly increasing within a track; sun-anchored times must not
  cross an adjacent keyframe's resolved time.

## Appendix C — the single core function (shared by engine + UI)

```
resolve(schedule, t) -> {
  power:      bool,
  brightness: pct | None,
  kelvin:     K   | None,
  rgb:        [r,g,b] | None,   # Phase 3
  # plus, for the lamp: {transition, ease} per active property
}
```

- The engine calls it with **now** to decide what to send; the UI calls it across
  a sampled time range to **draw the curves** and **animate the preview**. One
  source of truth, no duplication. It is pure and unit-testable — which is where
  the "does the math look right" testing lives before any bulb is touched.
