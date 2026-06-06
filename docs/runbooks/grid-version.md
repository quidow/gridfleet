# WebDriver router version

The WebDriver router replaces the Selenium Grid hub. It is built from source in this repo (`router/`) rather than pulled as a pinned third-party image.

It is wired into both compose files as the `router` service:

- `docker/docker-compose.yml`
- `docker/docker-compose.prod.yml`

Both build the image from `router/Dockerfile`. The Rust toolchain version is pinned in `router/rust-toolchain.toml`.

## Rebuild and roll out

1. Make the change under `router/src` (or bump a dependency in `router/Cargo.toml` / `Cargo.lock`).
2. Build and run the router tests: `cd router && cargo build --release && cargo test`.
3. Rebuild and restart the service: `docker compose up -d --build router`.
4. Smoke-test a session end to end (a testkit run, or a manual `POST /session` against `:4444`), and confirm the router proxies it to the device's Appium server.
5. Open a PR with the diff plus the verification output in the description.

## Rollback

Revert the `router/` change and rebuild: `docker compose up -d --build router`. The router is a single stateless service; rebuilds are atomic per service. No allocation state lives in the router — it is all in Postgres — so a restart does not lose in-flight bookkeeping.
