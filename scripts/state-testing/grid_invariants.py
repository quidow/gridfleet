"""Pure backend↔Appium↔router invariant evaluation over a captured timeline.

``grid_observe.GridStateCapture`` produces the timeline; this module judges it — no I/O.
A violation = an invariant's predicate continuously False for longer than its grace
window. The system is eventually-consistent by design (the allocator inserts a pending
row, the router establishes the session against Appium, the sweep closes vanished
sessions), so transitions legitimately diverge for a few seconds; only SUSTAINED
divergence is a finding. ``verifying`` is exempt from every invariant (job-in-progress
phase by definition) and RESETS any open divergence run.

Three independent observation channels per tick (see grid_observe.py):
  1. backend  — GET /api/grid/status (control plane; what the manager believes)
  2. appium   — direct GET {target}/status + /appium/sessions (GROUND TRUTH)
  3. router   — GET {router}/metrics + GET /internal/grid/routes (data plane)
plus a direct Postgres read (DB truth). The invariants below NEVER compare the backend
endpoint against the DB it is served from (that would be a tautology): the independent
witness for "is this device actually serving a session" is Appium itself (channel 2),
cross-checked against the router's route map (channel 3).

Invariants:
  G1  busy ⟺ a running DB session row AND a session in the device's Appium enumeration
  G2  available ⟹ the device's Appium answers /status to the harness's own probe
  G3  offline/maintenance ⟹ no Appium session ∧ no running/pending DB row ∧ no route entry
  G4  DB running session row ⟺ Appium-enumerated session (phantom / orphan classes);
      an orphan is additionally cross-checked against the router route map
  G5  reserved device with a running session ⟹ session.run_id == reservation.run_id
  G6  flap: Appium-probe reachability flap, or route-map / active_routes churn, while
      backend device state is unchanged (a finding regardless of duration). Two passes:
      the gap-based pass (across reachability gaps) and the churn pass (route-set change
      while the probe stays UP, uncorrelated with any DB session-set change)
  G7  queue hygiene: no waiting ticket older than queue_timeout+30s; no claimed ticket
      without a session_row_id; no claimed ticket whose session row already ended
  G8  pending leak: no pending session row older than claim_window+15s
  G9  route-map coherence: every /internal/grid/routes entry has a running DB session row
  G9b every running DB session row has a /internal/grid/routes entry (inverse of G9;
      grace = one route-reconcile interval)
  grid_unobservable  any single channel dark continuously for > 30s. The appium channel's
      darkness only counts while the backend channel reports the node ``running`` (a node the
      operator stopped goes appium-dark by design — cross-channel agreement, not blindness);
      backend-dark + appium-dark still counts (no backend truth to excuse it). Overridable
      per scenario via GridInvariantOverrides (id "grid_unobservable") for rows whose
      INJECTED fault is a dark control plane (S26 backend restart).
"""

from __future__ import annotations

from dataclasses import dataclass

# Grace windows (seconds). G2/G3/G5 are the likely live-tuning candidates (Fire TV
# cold-start / Appium re-spawn latency — G5's post-cancel "back to free" lag rides the
# same path).
GRACE: dict[str, float] = {
    "G1": 15.0,
    "G2": 20.0,
    "G3": 20.0,
    "G4": 15.0,
    "G5": 20.0,
    "G7": 15.0,
    "G8": 15.0,
    "G9": 15.0,
    # G9b sits above one route-reconcile interval (60s): a route legitimately lags a
    # reconcile sweep; only a session unroutable PAST that is a finding.
    "G9b": 60.0,
}
UNOBSERVABLE_GRACE = 30.0
# A DB session row younger than this with no Appium session yet = "establishing", not a
# phantom — sits above the 180s reserved-session connect timeout (TR03's Fire TV ceiling).
SESSION_ESTABLISH_SEC = 190.0

# The three sample channels. A None list/flag on a channel means it was not sampled this
# tick (see the matching *_error field). Names are used by the per-channel unobservable check.
CHANNELS = ("backend", "appium", "router")


@dataclass(frozen=True)
class GridSample:
    """One tick across the three channels + a direct DB read.

    A channel's ``*_error`` being set means that channel's fields are untrusted for this
    tick (the per-channel unobservable check folds them in; content predicates that depend
    on the channel return None — not-applicable — when its error is set).
    """

    ts: float                       # monotonic seconds (ordering + durations)
    iso: str                        # wall-clock string, for reports

    # backend device API (shared truth used as the "claim under test")
    operational_state: str | None
    is_reserved: bool

    # --- channel 1: backend GET /api/grid/status -------------------------------------
    backend_node_state: str | None = None        # "running" / "stopped" / None (no node)
    backend_node_port: int | None = None
    backend_sessions: tuple[str, ...] = ()        # session ids the registry shows on this device
    backend_error: str | None = None

    # --- channel 2: direct Appium (GROUND TRUTH) -------------------------------------
    appium_reachable: bool = False                # device's Appium answered GET /status
    appium_sessions: tuple[str, ...] | None = None  # enumerated session ids (None = unreachable)
    appium_error: str | None = None

    # --- channel 3: router data plane -------------------------------------------------
    router_active_routes: int | None = None       # gridfleet_router_active_routes gauge
    route_sessions: tuple[str, ...] | None = None  # session ids the route map carries for this device
    router_error: str | None = None

    # --- direct Postgres read (DB truth) ---------------------------------------------
    db_running_sessions: int = 0
    db_running_session_ids: tuple[str, ...] = ()
    youngest_session_age_sec: float | None = None
    # Device-scoped PENDING rows (router allocate→confirm claim window). The product
    # derives busy from running|pending (live_session_predicate), so a YOUNG pending
    # claim is a legitimate busy with no Appium witness yet. The exemption is bounded
    # by claim_window_threshold_sec — a wedged claim must still trip G1 here, not be
    # delegated to fleet-wide G8.
    db_device_pending_sessions: int = 0
    db_device_pending_oldest_age_sec: float | None = None
    db_session_run_id: str | None = None          # run_id of the device's running session (G5)
    db_reservation_run_id: str | None = None      # active reservation's run_id (G5)
    # G7/G8 queue/pending facts (global, not per-device — the queue is fleet-wide).
    db_pending_oldest_age_sec: float | None = None
    db_queue_waiting_oldest_age_sec: float | None = None
    db_queue_claimed_without_row: int = 0
    # G7 v2: claimed tickets whose session_row_id resolves to a session that is no longer
    # pending|running (the F10/F24 class: a claim outliving its allocation). Precomputed at
    # sample time by one queue⋈sessions join.
    db_queue_claimed_stale: int = 0
    # G9: route-map session ids that have no running DB session row anywhere (precomputed
    # at sample time, since the route map is fleet-wide while a sample is device-scoped).
    route_sessions_without_db_row: tuple[str, ...] = ()
    # G9b (inverse of G9): fleet running-session ids that have NO route-map entry — an
    # unroutable session (the #6 class). Precomputed at sample time; () when either the
    # router channel or the DB read failed this tick (no basis to judge).
    db_sessions_without_route: tuple[str, ...] = ()
    # G7/G8 thresholds carried on the sample so the pure evaluator never reads config.
    # grid_observe stamps these from Config (queue_timeout+30, claim_window+15); the defaults
    # mirror the backend registry defaults (300+30, 120+15).
    queue_timeout_threshold_sec: float = 330.0
    claim_window_threshold_sec: float = 135.0

    def channel_error(self, channel: str) -> str | None:
        return {
            "backend": self.backend_error,
            "appium": self.appium_error,
            "router": self.router_error,
        }[channel]


@dataclass(frozen=True)
class GridInvariantOverrides:
    """Per-scenario knobs for rows that intentionally wedge grid state."""

    exempt: tuple[str, ...] = ()                 # invariant IDs to skip entirely
    grace: tuple[tuple[str, float], ...] = ()    # (invariant_id, grace_sec) extensions


@dataclass(frozen=True)
class Violation:
    invariant_id: str
    started_iso: str
    ended_iso: str
    duration_sec: float
    detail: str

    def summary(self) -> str:
        return (
            f"{self.invariant_id} sustained {self.duration_sec:.0f}s"
            f" [{self.started_iso}..{self.ended_iso}]: {self.detail}"
        )


# --- channel parsers (pure; consumed by grid_observe at sample time) --------------------


def parse_backend_status(payload: dict, device_id: str) -> tuple[str | None, int | None, tuple[str, ...]]:
    """Channel 1: extract (node_state, node_port, session_ids) for our device.

    ``/api/grid/status`` carries ``registry.devices[]`` (node_state, node_port) and a flat
    ``active_session_ids`` fleet list. Per-device session attribution is left to channels 2/3 + DB
    (the status endpoint reports fleet session ids, not per-device). Control-plane view only.
    """
    registry = payload.get("registry") or {}
    node_state: str | None = None
    node_port: int | None = None
    for dev in registry.get("devices", []):
        if str(dev.get("id")) == device_id:
            node_state = dev.get("node_state")
            node_port = dev.get("node_port")
            break
    sessions = tuple(s for s in payload.get("active_session_ids", []) if isinstance(s, str))
    return node_state, node_port, sessions


def parse_appium_status(payload: dict) -> bool:
    """Channel 2 liveness: GET {target}/status -> ready flag (W3C/Appium ``value.ready``)."""
    value = payload.get("value")
    if isinstance(value, dict) and "ready" in value:
        return bool(value.get("ready"))
    # Some Appium builds answer 200 with no `ready`; a 200 response IS liveness.
    return True


def parse_appium_sessions(payload: dict) -> tuple[str, ...]:
    """Channel 2 enumeration: GET {target}/appium/sessions -> session ids.

    Appium 3.x shape ``{"value": [{"id": ...}, ...]}`` (requires the session_discovery
    insecure feature, which the packs enable)."""
    value = payload.get("value")
    if not isinstance(value, list):
        return ()
    return tuple(s["id"] for s in value if isinstance(s, dict) and isinstance(s.get("id"), str))


def parse_router_active_routes(metrics_text: str) -> int | None:
    """Channel 3: parse the ``gridfleet_router_active_routes`` IntGauge from /metrics text."""
    for line in metrics_text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        if line.startswith("gridfleet_router_active_routes"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(float(parts[-1]))
                except ValueError:
                    return None
    return None


def parse_routes(payload: dict, device_node_targets: dict[str, str]) -> dict[str, list[str]]:
    """Channel 3: GET /internal/grid/routes -> {device_id: [session_id, ...]}.

    The route map carries ``{session_id, target}`` where target == ``http://{host}:{port}``.
    ``device_node_targets`` maps each device id to its expected target so route entries can
    be attributed back to a device (the route map itself is device-agnostic)."""
    target_to_device = {t: d for d, t in device_node_targets.items()}
    out: dict[str, list[str]] = {}
    for entry in payload.get("routes", []):
        sid = entry.get("session_id")
        target = entry.get("target")
        dev = target_to_device.get(target)
        if dev is not None and isinstance(sid, str):
            out.setdefault(dev, []).append(sid)
    return out


def state_held(samples: list[GridSample], state: str) -> bool:
    """True when every trusted sample (no error, state observed) shows `state`.

    Verify-hook helper: 'the device never left X during the window', which is a
    stronger claim than the settle-read alone."""
    seen = [
        s.operational_state
        for s in samples
        if not _any_channel_error(s) and s.operational_state is not None
    ]
    return all(st == state for st in seen)


# --- predicates -------------------------------------------------------------------------
# Each returns True (holds), False (diverged), or None (not applicable at this sample —
# closes any open divergence run, same as True). A predicate returns None when the channel
# it depends on did not sample this tick (its data is untrusted, not a violation).


def _establishing(s: GridSample) -> bool:
    """A young DB session row with no Appium session yet = Appium still creating it."""
    return (
        not _appium_has_session(s)
        and s.db_running_sessions > 0
        and s.youngest_session_age_sec is not None
        and s.youngest_session_age_sec < SESSION_ESTABLISH_SEC
    )


def _appium_has_session(s: GridSample) -> bool:
    return bool(s.appium_sessions)


def _g1(s: GridSample, run_id: str | None) -> bool | None:
    # G1: busy ⟺ running DB row AND an Appium-enumerated session (DB-vs-Appium, never
    # DB-vs-backend-endpoint). Appium is the independent witness.
    if s.appium_error is not None or s.appium_sessions is None:
        return None  # channel 2 dark: cannot judge
    if s.operational_state == "busy":
        if _establishing(s):
            return None  # busy is session-row-derived; Appium lags by the create time
        if (
            s.db_device_pending_sessions > 0
            and s.db_device_pending_oldest_age_sec is not None
            and s.db_device_pending_oldest_age_sec < s.claim_window_threshold_sec
        ):
            # allocate→confirm claim window (pending row derives busy). A WEDGED
            # claim (age ≥ claim window) falls through and is judged normally.
            return None
        return s.db_running_sessions > 0 and _appium_has_session(s)
    if _appium_has_session(s):
        # A session in Appium while the device is NOT busy is only legit while draining (S06).
        return s.operational_state == "maintenance"
    return None


def _g2(s: GridSample, run_id: str | None) -> bool | None:
    # G2: available ⟹ the device's own Appium answers the harness's /status probe.
    if s.operational_state != "available":
        return None
    if s.appium_error is not None:
        return None  # cannot probe this tick; unobservable handles sustained darkness
    return s.appium_reachable


def _g3(s: GridSample, run_id: str | None) -> bool | None:
    # G3: offline/maintenance ⟹ no Appium session ∧ no running/pending DB row ∧ no route entry.
    if s.operational_state not in ("offline", "maintenance"):
        return None
    if s.appium_error is not None or s.appium_sessions is None:
        return None
    # A draining maintenance device may still hold ONE session (S06); only a free Appium with
    # a route/DB phantom is a violation. We treat any Appium session on an offline device, or a
    # route-map entry, as the violation surface — but allow maintenance to carry a draining one.
    has_appium = _appium_has_session(s)
    has_route = bool(s.route_sessions)
    has_db = s.db_running_sessions > 0
    if s.operational_state == "maintenance":
        # Draining session is legit; only a route/DB row WITHOUT a matching Appium session
        # (i.e. a phantom route to a dead/maintenance device) is the violation.
        return not (has_route and not has_appium) and not (has_db and not has_appium)
    # offline: there must be nothing.
    return not has_appium and not has_db and not has_route


def _g4(s: GridSample, run_id: str | None) -> bool | None:
    # G4: DB running session ⟺ Appium-enumerated session. Phantom = DB-yes/Appium-no;
    # orphan = Appium-yes/DB-no (orphan additionally cross-checked against the route map).
    if s.appium_error is not None or s.appium_sessions is None:
        return None
    has_db = s.db_running_sessions > 0
    has_appium = _appium_has_session(s)
    if not has_db and not has_appium:
        return None
    if has_db and not has_appium and _establishing(s):
        return None  # creation in flight; only an OLD row without an Appium session is a phantom
    return has_db == has_appium


def _g5(s: GridSample, run_id: str | None) -> bool | None:
    # G5: reserved device WITH a running session ⟹ session.run_id == reservation.run_id
    # (Postgres cross-table check). The hub-stereotype clause is dropped (no hub).
    if not s.is_reserved:
        return None
    if s.db_running_sessions == 0:
        return None  # reservation without a session is not G5's subject
    if s.db_reservation_run_id is None:
        return None  # no active reservation row to compare against
    return s.db_session_run_id == s.db_reservation_run_id


def _g7(s: GridSample, run_id: str | None) -> bool | None:
    # G7: queue hygiene. Waiting ticket too old, or claimed ticket without a session row.
    # The slack is folded into db_queue_waiting_oldest_age_sec's threshold at sample time
    # by comparing here against a None-or-age contract: grid_observe stamps the raw age and
    # claimed-without-row count; the threshold lives in the sample's own facts via the runner
    # config. We compare against the per-sample contract below.
    waiting_age = s.db_queue_waiting_oldest_age_sec
    if s.db_queue_claimed_without_row > 0:
        return False
    if s.db_queue_claimed_stale > 0:
        return False
    if waiting_age is None:
        return None
    return waiting_age <= s.queue_timeout_threshold_sec


def _g8(s: GridSample, run_id: str | None) -> bool | None:
    # G8: pending leak. A pending session row older than claim_window+slack never confirmed.
    age = s.db_pending_oldest_age_sec
    if age is None:
        return None
    return age <= s.claim_window_threshold_sec


def _g9(s: GridSample, run_id: str | None) -> bool | None:
    # G9: route-map coherence. Every route entry must map to a running DB session row.
    if s.router_error is not None:
        return None
    if not s.route_sessions_without_db_row:
        return None
    return False


def _g9b(s: GridSample, run_id: str | None) -> bool | None:
    # G9b: every running DB session row must have a route-map entry (inverse of G9).
    if s.router_error is not None:
        return None
    if not s.db_sessions_without_route:
        return None
    return False


_PREDICATES = {
    "G1": _g1,
    "G2": _g2,
    "G3": _g3,
    "G4": _g4,
    "G5": _g5,
    "G7": _g7,
    "G8": _g8,
    "G9": _g9,
    "G9b": _g9b,
}

_DESCRIPTIONS = {
    "G1": "busy⟺(DB session ∧ Appium session) mismatch",
    "G2": "backend available but the device's Appium does not answer the harness /status probe",
    "G3": "offline/maintenance device still has an Appium session / route / DB row",
    "G4": "DB running-session rows disagree with the device's Appium enumeration (phantom/orphan)",
    "G5": "reserved device's running session.run_id != reservation.run_id",
    "G7": "queue unhealthy: waiting ticket past timeout, or claimed ticket with no/ended session row",
    "G8": "pending session row leaked past the claim window without confirming",
    "G9": "route-map entry with no matching running DB session row",
    "G9b": "running DB session with no route-map entry (unroutable session)",
}


def _describe(inv_id: str, s: GridSample) -> str:
    return (
        f"{_DESCRIPTIONS[inv_id]} | first-bad sample: state={s.operational_state}"
        f" reserved={s.is_reserved} appium_reachable={s.appium_reachable}"
        f" appium_sessions={s.appium_sessions} route_sessions={s.route_sessions}"
        f" db_sessions={s.db_running_sessions} device_pending={s.db_device_pending_sessions}"
        f" device_pending_age={s.db_device_pending_oldest_age_sec} active_routes={s.router_active_routes}"
        f" sess_run={s.db_session_run_id} resv_run={s.db_reservation_run_id}"
        f" pending_age={s.db_pending_oldest_age_sec} waiting_age={s.db_queue_waiting_oldest_age_sec}"
        f" claimed_no_row={s.db_queue_claimed_without_row}"
        f" claimed_stale={s.db_queue_claimed_stale}"
        f" route_no_db={s.route_sessions_without_db_row}"
        f" db_no_route={s.db_sessions_without_route}"
    )


# --- scanner ----------------------------------------------------------------------------


def _any_channel_error(s: GridSample) -> bool:
    return any(s.channel_error(c) is not None for c in CHANNELS)


def _scan(
    samples: list[GridSample],
    run_id: str | None,
    overrides: GridInvariantOverrides | None,
) -> tuple[list[Violation], list[str]]:
    """Run-length scan: returns (violations, pending-sub-grace-open-divergence ids).

    A divergence run opens at the first False sample and closes on True/None (or a
    ``verifying`` sample, which resets everything). A sample whose required channel
    erred makes the predicate return None — neither extends nor resets a run — so
    continuity is preserved across a flaky channel tick. A run still open at window
    end is a violation if it already exceeds grace, otherwise it is "pending"."""
    graces = dict(GRACE)
    exempt: set[str] = set()
    if overrides is not None:
        graces.update(dict(overrides.grace))
        exempt = set(overrides.exempt)
    ids = [i for i in _PREDICATES if i not in exempt]

    violations: list[Violation] = []
    started: dict[str, GridSample] = {}
    last_bad: dict[str, GridSample] = {}

    def close(inv_id: str) -> bool:
        start, end = started.pop(inv_id), last_bad.pop(inv_id)
        duration = end.ts - start.ts
        if duration > graces[inv_id]:
            violations.append(Violation(inv_id, start.iso, end.iso, duration, _describe(inv_id, start)))
            return True
        return False

    for s in sorted(samples, key=lambda x: x.ts):
        if s.operational_state == "verifying":
            for inv_id in list(started):
                close(inv_id)
            continue
        for inv_id in ids:
            ok = _PREDICATES[inv_id](s, run_id)
            if ok is False:
                started.setdefault(inv_id, s)
                last_bad[inv_id] = s
            elif ok is True and inv_id in started:
                close(inv_id)
            # ok is None: not applicable this tick — leave any open run intact (continuity).

    pending: list[str] = []
    for inv_id in list(started):
        if not close(inv_id):
            pending.append(inv_id)
    return violations, pending


def _flap_violations(
    samples: list[GridSample], overrides: GridInvariantOverrides | None
) -> list[Violation]:
    """G6: Appium-probe reachability flap, or route-map / active_routes churn, while the
    backend device state is unchanged. A bounce with an unchanged backend reading means the
    control plane missed (or will spuriously react to) a data-plane churn — a finding
    regardless of duration. ``verifying`` samples are excluded (verify legitimately bounces)."""
    if overrides is not None and "G6" in overrides.exempt:
        return []
    clean = sorted(
        (s for s in samples if not _any_channel_error(s) and s.operational_state != "verifying"),
        key=lambda s: s.ts,
    )
    out: list[Violation] = []
    last_up: GridSample | None = None      # most recent reachable/route-stable sample
    gap_start: GridSample | None = None    # first "down" sample of the current gap
    state_changed = False                  # did backend state/reservation move during the gap?

    def _up(s: GridSample) -> bool:
        # "Up" = the device's Appium answers AND its route presence matches its session presence.
        return s.appium_reachable

    for s in clean:
        if _up(s):
            if (
                gap_start is not None
                and last_up is not None
                and not state_changed
                and s.operational_state == last_up.operational_state
                and s.is_reserved == last_up.is_reserved
                # Route/active_routes churn across the gap that lands back on the same count is
                # the data-plane bounce we want to flag; a count change is a real transition.
                and s.router_active_routes == last_up.router_active_routes
                and (s.route_sessions or ()) == (last_up.route_sessions or ())
            ):
                out.append(
                    Violation(
                        "G6", gap_start.iso, s.iso, s.ts - gap_start.ts,
                        f"Appium/route flapped (down {s.ts - gap_start.ts:.0f}s) while backend"
                        f" stayed {s.operational_state!r}",
                    )
                )
            last_up, gap_start, state_changed = s, None, False
        else:
            if last_up is None:
                continue  # window opened down: no flap baseline yet
            if gap_start is None:
                gap_start = s
            if (
                s.operational_state != last_up.operational_state
                or s.is_reserved != last_up.is_reserved
            ):
                state_changed = True
    return out


# Route-set changes must correlate with a DB running-session-set change within this window
# (one route-reconcile interval + slack); an uncorrelated change = data-plane churn the
# control plane never saw. A create/end legitimately moves both, ticks apart — the window
# absorbs that ordering skew.
_ROUTE_DB_CORRELATION_SEC = 75.0


def _route_churn_violations(
    samples: list[GridSample], overrides: GridInvariantOverrides | None
) -> list[Violation]:
    """G6 churn pass: route-map churn while Appium stays reachable. ``_flap_violations``
    only compares route sets ACROSS a reachability gap, so an entry flapping while the
    probe answers is invisible to it. Flag any route-set change with no DB
    running-session-set change within ±_ROUTE_DB_CORRELATION_SEC."""
    if overrides is not None and "G6" in overrides.exempt:
        return []
    clean = sorted(
        (s for s in samples if not _any_channel_error(s) and s.operational_state != "verifying"),
        key=lambda s: s.ts,
    )
    db_change_ts = [
        b.ts for a, b in zip(clean, clean[1:])
        if a.db_running_session_ids != b.db_running_session_ids
    ]
    routed = [s for s in clean if s.route_sessions is not None]
    out: list[Violation] = []
    for a, b in zip(routed, routed[1:]):
        if tuple(a.route_sessions or ()) == tuple(b.route_sessions or ()):
            continue
        if any(abs(b.ts - t) <= _ROUTE_DB_CORRELATION_SEC for t in db_change_ts):
            continue
        out.append(Violation(
            "G6", a.iso, b.iso, b.ts - a.ts,
            f"route map changed {a.route_sessions}->{b.route_sessions} with no DB "
            f"session change within ±{_ROUTE_DB_CORRELATION_SEC:.0f}s (data-plane churn)",
        ))
    return out


def _appium_dark_counts(s: GridSample) -> bool:
    """An appium-channel-dark sample is only UNOBSERVABLE while the backend channel (same
    sample) says the node is running. A node the operator intentionally stopped (S17) goes
    appium-dark by design — that is cross-channel AGREEMENT, not unobservability, and is
    exactly what G2/G3 reason about. The one exception: if the backend channel ALSO errored
    this tick there is no backend truth to correlate against, so the darkness cannot be
    excused and still counts."""
    if s.backend_error is not None:
        return True
    return s.backend_node_state == "running"


def _unobservable_violations(
    samples: list[GridSample], overrides: GridInvariantOverrides | None = None
) -> list[Violation]:
    """Per-channel: a single channel dark continuously for > UNOBSERVABLE_GRACE is a real
    issue (a sample with one channel erring still observes the other two, but if THAT channel
    stays dark too long the axis it witnesses is fiction). Rows whose injected fault IS a
    dark channel (S26 backend restart) exempt/extend via overrides id "grid_unobservable"."""
    grace = UNOBSERVABLE_GRACE
    if overrides is not None:
        if "grid_unobservable" in overrides.exempt:
            return []
        grace = dict(overrides.grace).get("grid_unobservable", UNOBSERVABLE_GRACE)
    out: list[Violation] = []
    ordered = sorted(samples, key=lambda x: x.ts)
    for channel in CHANNELS:
        run_start: GridSample | None = None
        run_end: GridSample | None = None
        for s in ordered:
            dark = s.channel_error(channel) is not None
            if channel == "appium" and dark and not _appium_dark_counts(s):
                # appium-dark-while-stopped is cross-channel agreement, not unobservability:
                # treat as observable so it neither opens nor extends a dark run.
                dark = False
            if dark:
                if run_start is None:
                    run_start = s
                run_end = s
            else:
                if run_start is not None and run_end is not None:
                    duration = run_end.ts - run_start.ts
                    if duration > grace:
                        out.append(
                            Violation(
                                "grid_unobservable", run_start.iso, run_end.iso, duration,
                                f"{channel} channel dark continuously: {run_start.channel_error(channel)}",
                            )
                        )
                run_start = run_end = None
        if run_start is not None and run_end is not None:
            duration = run_end.ts - run_start.ts
            if duration > grace:
                out.append(
                    Violation(
                        "grid_unobservable", run_start.iso, run_end.iso, duration,
                        f"{channel} channel dark continuously: {run_start.channel_error(channel)}",
                    )
                )
    return out


def evaluate_invariants(
    samples: list[GridSample],
    *,
    run_id: str | None = None,
    overrides: GridInvariantOverrides | None = None,
) -> list[Violation]:
    """Judge a scenario window's timeline. Any returned violation = scenario FAIL."""
    violations, _ = _scan(samples, run_id, overrides)
    violations.extend(_flap_violations(samples, overrides))
    violations.extend(_route_churn_violations(samples, overrides))
    violations.extend(_unobservable_violations(samples, overrides))
    return sorted(violations, key=lambda v: v.started_iso)


def open_divergences(
    samples: list[GridSample],
    *,
    run_id: str | None = None,
    overrides: GridInvariantOverrides | None = None,
) -> list[str]:
    """Invariant ids still diverged at window end but below grace — linger on these."""
    _, pending = _scan(samples, run_id, overrides)
    return pending
