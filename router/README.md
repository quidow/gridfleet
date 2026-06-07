# GridFleet Router

Pingora-based W3C WebDriver router for GridFleet.

Accepts WebDriver `POST /session` requests, allocates a device via the backend internal grid API, and proxies all subsequent session commands directly to Appium on the allocated host. Runs as a standalone binary alongside (or instead of) a Selenium Grid hub.

WebSocket upgrades on session paths (W3C BiDi / CDP pointed at `:4444`) are tunneled to the Appium host as a duplex stream; an established tunnel is exempt from `--proxy-timeout`, so idle gaps between frames do not tear it down. Note that Appium returns `webSocketUrl` pointing at its own host — clients honoring that capability verbatim connect directly to the device host and need network reach to it, same as in the relay era.

## CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--listen` | required | `host:port` to bind for WebDriver traffic, e.g. `0.0.0.0:4444` |
| `--backend` | required | Backend base URL, e.g. `http://backend:8000` |
| `--backend-auth` | — | HTTP Basic credentials for backend calls: `user:pass` |
| `--proxy-timeout` | `300` | Per-command upstream timeout in seconds |
| `--new-session-timeout` | `330` | Overall cap on a new-session request including queueing, in seconds |

## Environment variables

Every flag has an equivalent environment variable, so values never need to appear in the process argv (which is visible to `ps` / `docker inspect`):

| Flag | Environment variable |
|------|----------------------|
| `--listen` | `GRIDFLEET_ROUTER_LISTEN` |
| `--backend` | `GRIDFLEET_ROUTER_BACKEND` |
| `--backend-auth` | `GRIDFLEET_ROUTER_BACKEND_AUTH` |
| `--proxy-timeout` | `GRIDFLEET_ROUTER_PROXY_TIMEOUT` |
| `--new-session-timeout` | `GRIDFLEET_ROUTER_NEW_SESSION_TIMEOUT` |

Always pass `--backend-auth` credentials via `GRIDFLEET_ROUTER_BACKEND_AUTH` rather than the command line — env-based credentials stay out of `ps` / `docker inspect`-visible argv.

## Getting started

```bash
cd router
cargo build --release
./target/release/gridfleet-router \
  --listen 0.0.0.0:4444 \
  --backend http://localhost:8000
```

Rust toolchain version is pinned in `rust-toolchain.toml`. Install via [rustup](https://rustup.rs/).
