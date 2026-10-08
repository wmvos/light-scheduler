# Changelog

All notable changes to **Light Scheduler**.

## [0.1.0] — M1 (skeleton)

- App boilerplate: Supervisor **App** manifest (`config.yaml`), pinned Dockerfile,
  s6 layout (`etc/services.d` + `usr/bin`), ingress on `:8099`.
- Talks to Home Assistant over the Supervisor-proxied **REST** API, with a
  best-effort **WebSocket** subscription for live state.
- Fixed 2-ramp daily demo drives **one** light (the `light` option):
  `08:00 → on 30% 4000K`, `18:00 → on 10% 2700K`, `23:00 → off (30 s fade)`.
- Ingress panel shows live state, last applied keyframe, and next keyframe.

> Skeleton only — the visual timeline editor, per-schedule config, and public
> distribution land in later milestones. See [SCOPE.md](SCOPE.md).
