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

## 3. Check manager-to-agent reachability from inside the backend container

```bash
cd docker
docker compose --env-file .env -f docker-compose.prod.yml exec backend \
  python -c "import urllib.request; print(urllib.request.urlopen('http://HOST_IP:5100/agent/health', timeout=5).read().decode())"
```

If this fails but step 2 succeeds, the problem is network reachability from the manager host to the agent host.

## 4. Check the agent service manager on the host

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

## 5. Verify the agent process configuration

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
- `AGENT_GRID_HUB_URL` points at the Grid hub port
- `AGENT_GRID_PUBLISH_URL` and `AGENT_GRID_SUBSCRIBE_URL` match the manager host

## 6. Recover the host in the manager

If the host is still `pending` but expected:

```bash
curl -X POST -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/hosts/HOST_ID/approve
```

After connectivity is restored, refresh discovery:

```bash
curl -X POST -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/hosts/HOST_ID/discover | python -m json.tool
```

## 7. When to escalate

Escalate beyond the manager if:

- direct `curl` to `/agent/health` fails on the host itself
- the service manager shows repeated process crashes
- the manager host can reach the agent, but Grid URLs are wrong or blocked for all hosts
