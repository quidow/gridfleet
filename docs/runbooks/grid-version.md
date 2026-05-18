# Selenium Grid version pin

The hub image is pinned in:

- `docker/docker-compose.yml`
- `docker/docker-compose.prod.yml`

Current pin: `selenium/hub:4.43.0-20260404`.

The Python relay reports the matching version string from
`agent/agent_app/grid_node/service.py:24` (`_GRID_NODE_VERSION`).

## Why these are pinned

A pinned hub tag lets us reason about which distributor and router
fixes are present. The most recent relevant fixes:

- 4.41 `#17022` — distributor deadlock prevention.
- 4.41 `#17104` — distributor thread exhaustion in node health-check.
- 4.41 `#17106` — `ProxyNodeWebsockets` counter leak.
- 4.41 `#17109` — distributor retries session when RemoteNode is shutting down.
- 4.41 `#17097` — stereotype-cap merging in `RelaySessionFactory` (Java only; Python relay verified via `agent/tests/grid_node/test_session_response_stereotype_merge.py`).
- 4.42 `#17146` — router WebSocket transparent TCP tunnel.
- 4.42 `#17197` — router WebSocket dropped-close / idle / high-latency handling.
- 4.42 `#17211` — router-node read timeout aligned with session pageLoad capability.

## Bump checklist

1. Pick the new tag from https://github.com/SeleniumHQ/docker-selenium/releases.
2. Skim https://github.com/SeleniumHQ/selenium/releases between the old and new tags for breaking changes. Grid changes between minor releases have been fixes only up through 4.43 — anything labeled "BREAKING" in the release notes is the stopping criterion.
3. Check the Selenium Java CHANGELOG (`https://github.com/SeleniumHQ/selenium/blob/trunk/java/CHANGELOG`) for any `[grid] Accept legacy …` patches. If the wire format changed for an event we consume, port the lenient parser to `backend/app/grid/event_bus.py:parse_session_closed_id` and add tests.
4. Update both compose files + the agent version string.
5. Restart the hub: `docker compose pull selenium-hub && docker compose up -d selenium-hub`.
6. Drain a node mid-session and confirm the distributor retries the in-flight session.
7. Open a PR with the diff plus the verification output in the description.

## Rollback

Revert the compose change, `docker compose up -d selenium-hub`. Image bumps are atomic per service.
