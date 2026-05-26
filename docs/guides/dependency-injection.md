# Dependency Injection Guide

## Overview

The backend uses Protocol-based dependency injection following SOLID principles.
Domain modules depend on abstract Protocols, not concrete classes. A single
composition root wires all implementations at startup.

## Key Files

| File | Purpose |
|------|---------|
| `app/composition.py` | Composition root -- the only module that imports concrete types |
| `app/{domain}/services_container.py` | Frozen dataclass holding a domain's service instances |
| `app/{domain}/protocols.py` | Protocol definitions consumed by the domain |
| `app/{domain}/dependencies.py` | FastAPI `Depends()` wiring for routers |
| `app/core/protocols.py` | Cross-domain Protocol definitions (settings, emit) |
| `app/dependencies.py` | App-level `AppServicesDep` for routers that span domains |

## How It Works

### Startup

`app/main.py` lifespan calls `compose_app()` once, which:

1. Creates concrete service instances (EventBus, SettingsService, etc.)
2. Patches module-level singletons for backward compatibility
3. Packs everything into `AppServices` -- a frozen dataclass tree
4. Stores `AppServices` on `app.state.services`

### Request Path

Routers access services through FastAPI dependency injection:

```python
# app/hosts/dependencies.py
def get_host_services(request: Request) -> HostServices:
    return request.app.state.services.hosts

HostServicesDep = Annotated["HostServices", Depends(get_host_services)]

# app/hosts/router.py
@router.get("/hosts")
async def list_hosts(services: HostServicesDep) -> list[HostRead]:
    ...
```

### Background Loops

Leader-owned loops receive their dependencies at construction time from the
composition root, not through FastAPI `Depends()`.

## How to Add a New Dependency

### 1. Define a Protocol (if cross-domain)

If other domains will consume your service, define a Protocol in the consuming
domain's `protocols.py`:

```python
# app/agent_comm/protocols.py
@runtime_checkable
class CircuitBreakerProtocol(Protocol):
    async def before_request(self, host: str) -> float | None: ...
    async def record_success(self, host: str) -> None: ...
    async def record_failure(self, host: str, *, error: str) -> None: ...
```

### 2. Add to the domain container

```python
# app/agent_comm/services_container.py
@dataclass(frozen=True, slots=True)
class AgentCommServices:
    http_pool: AgentHttpPool
    circuit_breaker: AgentCircuitBreaker
```

### 3. Wire in `compose_app()`

```python
# app/composition.py
breaker = AgentCircuitBreaker()
agent_comm_services = AgentCommServices(
    http_pool=pool,
    circuit_breaker=breaker,
)
```

### 4. Expose via FastAPI Depends

```python
# app/agent_comm/dependencies.py
def get_agent_comm_services(request: Request) -> AgentCommServices:
    return request.app.state.services.agent_comm

AgentCommServicesDep = Annotated["AgentCommServices", Depends(get_agent_comm_services)]
```

### 5. Add a protocol conformance test

```python
# tests/agent_comm/test_protocols.py
def test_agent_circuit_breaker_satisfies_protocol() -> None:
    breaker = AgentCircuitBreaker()
    assert isinstance(breaker, CircuitBreakerProtocol)
```

## How to Write Tests

### Unit tests -- construct containers with fakes

Pass fake implementations directly to the container or function under test.
No patching needed when the code depends on Protocols:

```python
class FakeCircuitBreaker:
    async def before_request(self, host: str) -> float | None:
        return None
    async def record_success(self, host: str) -> None:
        pass
    async def record_failure(self, host: str, *, error: str) -> None:
        pass

services = AgentCommServices(
    http_pool=fake_pool,
    circuit_breaker=FakeCircuitBreaker(),  # type: ignore[arg-type]
)
```

### Integration tests -- use dependency_overrides

Override the FastAPI dependency to swap in a test double:

```python
app.dependency_overrides[get_agent_comm_services] = lambda: test_services
```

## Rules

- **Protocols live in the consumer**, not the provider. If `hosts` depends on
  the circuit breaker, the Protocol goes in `app/hosts/protocols.py` (or
  `app/core/protocols.py` if shared widely).
- **`composition.py` is the only file that imports concrete classes** from
  multiple domains. Domain code imports Protocols or its own types only.
- **Module-level singletons are deprecated.** They exist for backward
  compatibility during migration. New code should receive dependencies through
  containers or function arguments.
- **Containers are frozen dataclasses** with `slots=True`. They are immutable
  after construction.
- **Every Protocol must have a conformance test** using `isinstance()` with the
  real implementation, in `tests/{domain}/test_protocols.py`.
