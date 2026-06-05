# GridFleet Router

Pingora-based W3C WebDriver router for GridFleet.

Accepts WebDriver `POST /session` requests, allocates a device via the backend internal grid API, and proxies all subsequent session commands directly to Appium on the allocated host. Runs as a standalone binary alongside (or instead of) a Selenium Grid hub.

## CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--listen` | required | `host:port` to bind for WebDriver traffic, e.g. `0.0.0.0:4444` |
| `--backend` | required | Backend base URL, e.g. `http://backend:8000` |
| `--backend-auth` | — | HTTP Basic credentials for backend calls: `user:pass` |
| `--proxy-timeout` | `300` | Per-command upstream timeout in seconds |
| `--new-session-timeout` | `330` | Overall cap on a new-session request including queueing, in seconds |

`--backend-auth` is visible in process listings; prefer injecting credentials via container secret mounts / env-based wrappers at deployment time (deployment wiring arrives with the compose integration).

## Getting started

```bash
cd router
cargo build --release
./target/release/gridfleet-router \
  --listen 0.0.0.0:4444 \
  --backend http://localhost:8000
```

Rust toolchain version is pinned in `rust-toolchain.toml`. Install via [rustup](https://rustup.rs/).
