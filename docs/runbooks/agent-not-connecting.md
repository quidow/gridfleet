# Runbook: Agent Not Connecting

Use this runbook when a host stays `pending`, flips `offline`, or stops returning discovery/health results.

When `GRIDFLEET_AUTH_ENABLED=true`, every `/api/*` call below requires HTTP Basic auth with the manager's machine credentials. Export them once and pass with `-u`:

```bash
export GRIDFLEET_TESTKIT_USERNAME="$GRIDFLEET_MACHINE_AUTH_USERNAME"
export GRIDFLEET_TESTKIT_PASSWORD="$GRIDFLEET_MACHINE_AUTH_PASSWORD"
```

`/api/health` stays open and does not need `-u`.

## 1. Identify the host state in the manager

```bash
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/hosts | python -m json.tool
curl -s http://localhost:8000/api/health | python -m json.tool
```

If the backend is unhealthy, recover the manager first. Host symptoms are often downstream from that.

## 2. Check the agent directly from the operator machine

```bash
curl -s http://HOST_IP:5100/agent/health | python -m json.tool
```

If this fails, the agent service, local firewall, host routing, or DNS/IP assignment is the first problem to solve.

## 3. Check whether the backend is actually seeing status pushes (forward direction)

Host liveness is push-recency based, not probe-based: the agent pushes `POST /agent/hosts/status` every `AGENT_STATUS_PUSH_INTERVAL_SEC` (default 10 s), and `host_sweep` marks a host offline once `last_heartbeat` is older than `general.host_offline_after_sec` (default 45 s). Check this first — a healthy `curl` to `/agent/health` does not mean the backend is receiving pushes.

```bash
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/hosts/HOST_ID | python -m json.tool   # check last_heartbeat recency
curl -s http://localhost:8000/metrics | grep gridfleet_host_status_pushes_total                                                     # push counter, per host_id label
```

If `last_heartbeat` is stale and the push counter isn't advancing for this host, check the agent's own logs for `status push failed` (step 4) before assuming a network problem — the agent logs its own push failures.

## 4. Check manager-to-agent reachability (reverse direction — partition-probe diagnostic only)

This is the same check the backend's own 60-second plumbing-cadence `/agent/health` probe performs. It no longer drives host liveness — it is a network-partition diagnostic and the installer's self-update drain gate. A failure here explains *why* pushes might not be reaching the backend (if the network is down both ways), but a success here does not clear the agent — go back to step 3.

```bash
cd docker
docker compose --env-file .env -f docker-compose.prod.yml exec backend \
  python -c "import urllib.request; print(urllib.request.urlopen('http://HOST_IP:5100/agent/health', timeout=5).read().decode())"
```

If this fails but step 2 succeeds, the problem is network reachability from the manager host to the agent host.

## 5. Check the agent service manager on the host

Linux:

```bash
systemctl --user status gridfleet-agent
journalctl --user -u gridfleet-agent -n 200 --no-pager
```

macOS:

```bash
launchctl print "gui/$(id -u)/com.gridfleet.agent"
tail -n 200 ~/Library/Logs/gridfleet-agent/stdout.log
tail -n 200 ~/Library/Logs/gridfleet-agent/stderr.log
```

## 6. Verify the agent process configuration

Linux:

```bash
grep '^AGENT_' ~/.config/gridfleet-agent/config.env
```

macOS:

```bash
plutil -p ~/Library/LaunchAgents/com.gridfleet.agent.plist
```

Check that:

- `AGENT_MANAGER_URL` points at the backend port
- `AGENT_ADVERTISE_IP` (if set) is an address the backend and the WebDriver router can reach, since the router connects directly to this host's Appium ports

## 7. Recover the host in the manager

If the host is still `pending` but expected:

```bash
curl -X POST -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/hosts/HOST_ID/approve
```

After connectivity is restored, refresh discovery:

```bash
curl -X POST -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/hosts/HOST_ID/discover | python -m json.tool
```

## 8. When to escalate

Escalate beyond the manager if:

- direct `curl` to `/agent/health` fails on the host itself
- `last_heartbeat` is stale, the push counter isn't advancing, and the agent's own logs show no `status push failed` entries (the push loop itself may be wedged)
- the service manager shows repeated process crashes
- the manager host can reach the agent, but Grid URLs are wrong or blocked for all hosts
