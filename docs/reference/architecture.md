# System Architecture

GridFleet uses a host-first orchestration model to manage Appium and Selenium Grid workflows. Its responsibilities are split across three major layers.

## 1. Backend Control Plane (FastAPI + Postgres)

The backend is a multi-worker stateless group of HTTP API servers. State is stored entirely in PostgreSQL. 

### Advisory Locks and Background Loops
Because multiple Uvicorn/FastAPI workers can run simultaneously (e.g., in a production Compose setup), the backend uses **PostgreSQL Advisory Locks** to ensure exactly one leader evaluates background maintenance tasks. The `app.main` lifespan starts ~10 distinct background loops (heartbeat, session_sync, node_health, device_connectivity, property_refresh, etc.) that:

- Monitor missing Agent heartbeats.
- Evaluate node health via Appium and Grid calls.
- Sync stray sessions on Appium that don't belong to the internal state.
- Transition device maintenance lifecycles.

### Data Storage
- Database: Async PostgreSQL via `asyncpg`.
- Tables are strictly schema-typed with `alembic` handling migrations.
- `tags` and hardware detections are JSON fields attached to the `Device` model.
- Process configurations use `GRIDFLEET_` prefixed env vars, while the device configuration mostly delegates to a dynamic Database Settings Registry.

## 2. Host Agent 

Agents run on physical lab hosts or VMs where devices are attached. Unlike the centralized Backend, Agents run on the 'edge' and govern physical connections.

- **Discovery**: Runs pack-aware probes and adapters, then reports discovered candidates through manager-owned intake flows.
- **Appium Process Management**: The Agent isolates each device by spawning standalone Appium server processes attached to that device's UDID/Serial.
- **Selenium Grid Registration**: Once Appium is healthy, the Agent launches a Grid Relay node (Java, via TOML config) that points to the central Grid Hub port.
- **Health Checks**: Monitors ADB connectivity and driver viability, terminating Appium processes gracefully if the physical device goes offline.

## 3. Frontend Operator Dashboard

The Frontend (`frontend/src`) acts as the single pane of glass for Fleet Operators.

- Built with React + TypeScript + Vite.
- Continuously polls API endpoints (5-15s intervals) to present real-time readiness matrices.
- Serves as the interface for Device Onboarding/Intake (where discovered devices are promoted into the active fleet).
- Exposes bulk actions and run/reservation overrides.

## Typical Event Flow (Registration & Run)

1. A device is plugged into a lab machine with the Agent installed.
2. The Agent discovers it through the relevant driver-pack probe and reports candidates to the manager-owned discovery flow.
3. The Operator views the Intake drawer in the Dashboard and "Registers" the device.
4. The Backend signals the Agent to start an Appium node for that device.
5. The Appium node points to the hardware; the complementary Grid Node attaches to the Hub.
6. A CI runner makes a reservation via `/api/runs`.
7. Testing traffic is sent directly to the Hub (`http://localhost:4444`), where Selenium routing matches it by Capabilities directly to that specific Relay Node, and therefore Appium Server & Device.
