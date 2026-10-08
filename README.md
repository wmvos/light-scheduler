# Light Scheduler

A Home Assistant **App** that drives your lights through the day — **on/off,
brightness, and color temperature** (color later) — using a **visual timeline
editor** that works like a video NLE.

It is a deliberately *better* take on [Adaptive Lighting](https://github.com/basnijholt/adaptive-lighting):
instead of a handful of global settings, you **see** the day as tracks and drag
keyframes, and your lamps do the fading for real (we send a *target + ramp* and
let the lamp glide).

> Home Assistant renamed *add-ons* to **apps** (Oct 2026). This is an app: an
> isolated container with its own UI panel (ingress), logs, and restarts. The
> required in-app UI is exactly why this is an App rather than a custom
> integration.

## Status

**M1 (skeleton)** — the app boilerplate, Supervisor-proxied HA **REST** +
best-effort **WebSocket**, a placeholder **ingress** panel, and a fixed 2-ramp
demo driving **one** light. See [SCOPE.md](SCOPE.md) for the full design and
[CHANGELOG.md](CHANGELOG.md) for what ships when.

## Install (local, from this repo)

The app installs from `/local_apps` on Home Assistant OS:

1. Enable the **Samba** app (Settings → Apps → App store → Samba).
2. Browse to `smb://homeassistant.local` (or your HA host) and copy the
   `light_scheduler/` folder into `/local_apps/`.
3. Settings → Apps → App store → **Check for updates** — *Light Scheduler* appears
   under **Local apps** → install.
4. Open **Settings → Apps → Light Scheduler** and set the **light** option to a
   `light.*` entity to point the demo ramp at it.

Public distribution (pre-built multi-arch images, one-click install) lands in
**M5**.

## Layout

- `repository.yaml` — app store manifest
- `light_scheduler/` — the App (manifest, `Dockerfile`, `rootfs/`)
- `SCOPE.md` — design & decisions
- `CHANGELOG.md` — release notes
- `LICENSE` — GPL-3.0

## License

GPL-3.0 — free to use and modify, but derivative work must stay GPL-3.0 (no
selling a closed fork). See [LICENSE](LICENSE).
