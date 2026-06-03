# gridfleet-agent-relay

The GridFleet grid-relay **fast-lane sidecar**: a small Rust proxy the
[gridfleet-agent](https://pypi.org/project/gridfleet-agent/) spawns per
device node. It streams WebDriver session commands directly to Appium and
forwards control-plane requests (session create/delete, node status, drain)
to the agent's Python relay.

Installing this package puts a `gridfleet-relay-proxy` binary on the
environment's PATH; the agent discovers it automatically
(`AGENT_RELAY_FAST_LANE=auto`). Without it the agent falls back to
in-process proxying.

This package contains no Python code. It is built with
[maturin](https://maturin.rs) (`bindings = "bin"`) from the `relay-proxy/`
component of the GridFleet monorepo.
