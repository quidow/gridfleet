"""Unit tests for the pure grid-invariant evaluator (synthetic timelines, no I/O)."""

from grid_invariants import (
    CHANNELS,
    GridInvariantOverrides,
    GridSample,
    Violation,
    evaluate_invariants,
    open_divergences,
    parse_appium_sessions,
    parse_appium_status,
    parse_backend_status,
    parse_router_active_routes,
    parse_routes,
    state_held,
)

DEV = "11111111-2222-3333-4444-555555555555"


# --- channel parsers --------------------------------------------------------------------


def test_parse_backend_status_resolves_node_fields():
    payload = {
        "registry": {"devices": [{"id": DEV, "node_state": "running", "node_port": 4723}]},
        "active_session_ids": ["sess-1"],
    }
    node_state, node_port, sessions = parse_backend_status(payload, DEV)
    assert node_state == "running"
    assert node_port == 4723
    assert sessions == ("sess-1",)


def test_parse_backend_status_unknown_device_is_none():
    payload = {"registry": {"devices": [{"id": "other", "node_state": "running", "node_port": 1}]}}
    assert parse_backend_status(payload, DEV) == (None, None, ())


def test_parse_backend_status_tolerates_missing_keys():
    assert parse_backend_status({}, DEV) == (None, None, ())


def test_parse_appium_status_ready_flag():
    assert parse_appium_status({"value": {"ready": True}}) is True
    assert parse_appium_status({"value": {"ready": False}}) is False
    assert parse_appium_status({"value": {}}) is True  # 200 with no ready == live


def test_parse_appium_sessions_extracts_ids():
    payload = {"value": [{"id": "a"}, {"id": "b"}, {"no": "id"}, "junk"]}
    assert parse_appium_sessions(payload) == ("a", "b")
    assert parse_appium_sessions({"value": None}) == ()
    assert parse_appium_sessions({}) == ()


def test_parse_router_active_routes():
    text = "# HELP gridfleet_router_active_routes foo\ngridfleet_router_active_routes 3\nother_metric 9\n"
    assert parse_router_active_routes(text) == 3
    assert parse_router_active_routes("nothing here") is None
    assert parse_router_active_routes("gridfleet_router_active_routes 0") == 0


def test_parse_routes_attributes_entries_to_devices():
    payload = {"routes": [
        {"session_id": "s1", "target": "http://h:4723"},
        {"session_id": "s2", "target": "http://h:9999"},  # unknown target
    ]}
    targets = {DEV: "http://h:4723"}
    assert parse_routes(payload, targets) == {DEV: ["s1"]}


# --- sample / violation shapes ----------------------------------------------------------


def test_sample_and_violation_shapes():
    s = GridSample(ts=1.0, iso="t1", operational_state="available", is_reserved=False)
    assert s.backend_error is None and s.appium_error is None and s.router_error is None
    assert s.appium_sessions is None  # not sampled by default
    v = Violation("G1", "t1", "t9", 8.0, "detail")
    assert "G1" in v.summary() and "8s" in v.summary()
    o = GridInvariantOverrides(exempt=("G6",), grace=(("G2", 40.0),))
    assert o.exempt == ("G6",)
    assert s.channel_error("backend") is None


def mk(ts, *, state="available", reserved=False,
       node_state="running", node_port=4723,
       appium_reachable=True, appium_sessions=(),
       active_routes=0, route_sessions=(),
       db_sessions=0, db_ids=(), sess_age=None,
       sess_run=None, resv_run=None,
       pending_age=None, waiting_age=None, claimed_no_row=0,
       device_pending=0, device_pending_age=None,
       route_no_db=(),
       backend_error=None, appium_error=None, router_error=None,
       qt=330.0, cw=135.0, **extra):
    """Synthetic three-channel sample; defaults model a clean idle available device whose
    Appium answers, with no sessions, no queue rows, and an empty route map."""
    return GridSample(
        ts=float(ts), iso=f"t{ts}", operational_state=state, is_reserved=reserved,
        backend_node_state=node_state, backend_node_port=node_port, backend_error=backend_error,
        appium_reachable=appium_reachable, appium_sessions=appium_sessions, appium_error=appium_error,
        router_active_routes=active_routes, route_sessions=route_sessions, router_error=router_error,
        db_running_sessions=db_sessions, db_running_session_ids=db_ids,
        youngest_session_age_sec=sess_age, db_session_run_id=sess_run, db_reservation_run_id=resv_run,
        db_pending_oldest_age_sec=pending_age, db_queue_waiting_oldest_age_sec=waiting_age,
        db_queue_claimed_without_row=claimed_no_row,
        db_device_pending_sessions=device_pending, db_device_pending_oldest_age_sec=device_pending_age,
        route_sessions_without_db_row=route_no_db,
        queue_timeout_threshold_sec=qt, claim_window_threshold_sec=cw, **extra,
    )


def _busy(ts, **kw):
    """A clean busy device: DB row + Appium-enumerated session + a route entry."""
    kw.setdefault("appium_sessions", ("sess-1",))
    kw.setdefault("db_sessions", 1)
    kw.setdefault("db_ids", ("sess-1",))
    kw.setdefault("route_sessions", ("sess-1",))
    kw.setdefault("active_routes", 1)
    return mk(ts, state="busy", **kw)


def _ids(violations):
    return [v.invariant_id for v in violations]


def test_clean_window_no_violations():
    samples = [mk(t) for t in range(0, 60, 2)]
    assert evaluate_invariants(samples) == []


def test_clean_busy_window_no_violations():
    samples = [_busy(t) for t in range(0, 60, 2)]
    assert evaluate_invariants(samples) == []


# --- G1: busy ⟺ (DB session ∧ Appium session) -------------------------------------------


def test_g1_busy_without_appium_session_sustained():
    # busy + DB row but Appium never enumerates the session for 30s (> 15s grace)
    samples = [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=300.0)
               for t in range(0, 31, 2)]
    assert "G1" in _ids(evaluate_invariants(samples))


def test_g1_transient_divergence_inside_grace_passes():
    # 8s of busy-without-appium-session, then the session appears: inside 15s grace
    samples = [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=300.0)
               for t in range(0, 9, 2)]
    samples += [_busy(t) for t in range(10, 40, 2)]
    assert evaluate_invariants(samples) == []


def test_g1_appium_session_on_maintenance_is_drain_not_violation():
    samples = [mk(t, state="maintenance", appium_sessions=("s",), db_sessions=1, db_ids=("s",))
               for t in range(0, 40, 2)]
    assert "G1" not in _ids(evaluate_invariants(samples))


def test_g1_appium_session_on_available_sustained_is_violation():
    samples = [mk(t, appium_sessions=("s",), db_sessions=1, db_ids=("s",)) for t in range(0, 31, 2)]
    assert "G1" in _ids(evaluate_invariants(samples))


def test_g1_not_judged_when_appium_channel_dark():
    samples = [mk(t, state="busy", db_sessions=1, appium_error="boom", appium_sessions=None)
               for t in range(0, 31, 2)]
    assert "G1" not in _ids(evaluate_invariants(samples))


def test_g1_busy_with_young_pending_claim_is_exempt():
    # allocate→confirm claim window: busy derives from the PENDING row
    # (live_session_predicate includes pending) — no running row and no Appium
    # session can exist yet. Bounded by the claim window: see the wedge test below.
    samples = [mk(t, state="busy", device_pending=1, device_pending_age=float(t) + 1.0)
               for t in range(0, 31, 2)]
    assert "G1" not in _ids(evaluate_invariants(samples))


def test_g1_busy_with_wedged_pending_claim_still_fires():
    # a claim OLDER than the claim window is a wedge, not a window — G1 keeps hunting
    samples = [mk(t, state="busy", device_pending=1, device_pending_age=200.0 + t)
               for t in range(0, 31, 2)]
    assert "G1" in _ids(evaluate_invariants(samples))


def test_g1_busy_with_no_rows_at_all_is_still_a_violation():
    # the ftv-09 S12 signature WITHOUT a pending row must keep firing
    samples = [mk(t, state="busy") for t in range(0, 31, 2)]
    assert "G1" in _ids(evaluate_invariants(samples))


# --- G2: available ⟹ Appium answers the probe -------------------------------------------


def test_g2_available_appium_unreachable_sustained():
    samples = [mk(t, appium_reachable=False) for t in range(0, 31, 2)]
    assert "G2" in _ids(evaluate_invariants(samples))


def test_g2_passes_when_appium_reachable():
    samples = [mk(t, appium_reachable=True) for t in range(0, 31, 2)]
    assert "G2" not in _ids(evaluate_invariants(samples))


def test_g2_not_judged_when_appium_channel_errored():
    # appium channel errored (not merely unreachable): G2 abstains; unobservable owns it
    samples = [mk(t, appium_error="timeout", appium_reachable=False) for t in range(0, 9, 2)]
    samples += [mk(t) for t in range(10, 20, 2)]
    assert "G2" not in _ids(evaluate_invariants(samples))


# --- G3: offline/maintenance ⟹ nothing live ---------------------------------------------


def test_g3_offline_with_appium_session_sustained():
    samples = [mk(t, state="offline", appium_sessions=("s",)) for t in range(0, 31, 2)]
    assert "G3" in _ids(evaluate_invariants(samples))


def test_g3_offline_with_route_entry_sustained():
    samples = [mk(t, state="offline", route_sessions=("s",), active_routes=1) for t in range(0, 31, 2)]
    assert "G3" in _ids(evaluate_invariants(samples))


def test_g3_offline_with_db_row_sustained():
    samples = [mk(t, state="offline", db_sessions=1, db_ids=("s",)) for t in range(0, 31, 2)]
    assert "G3" in _ids(evaluate_invariants(samples))


def test_g3_offline_with_nothing_is_fine():
    samples = [mk(t, state="offline", appium_reachable=False) for t in range(0, 40, 2)]
    assert "G3" not in _ids(evaluate_invariants(samples))


def test_g3_maintenance_draining_session_is_fine():
    # one Appium session + matching route while draining (S06): legit
    samples = [mk(t, state="maintenance", appium_sessions=("s",), route_sessions=("s",),
                  db_sessions=1, db_ids=("s",), active_routes=1) for t in range(0, 40, 2)]
    assert "G3" not in _ids(evaluate_invariants(samples))


def test_g3_maintenance_phantom_route_without_appium_is_violation():
    samples = [mk(t, state="maintenance", appium_sessions=(), route_sessions=("s",), active_routes=1)
               for t in range(0, 31, 2)]
    assert "G3" in _ids(evaluate_invariants(samples))


# --- G4: DB session ⟺ Appium session ----------------------------------------------------


def test_g4_phantom_db_row_without_appium_session_sustained():
    samples = [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=300.0)
               for t in range(0, 31, 2)]
    assert "G4" in _ids(evaluate_invariants(samples))


def test_g4_orphan_appium_session_without_db_row_sustained():
    samples = [mk(t, appium_sessions=("s",), db_sessions=0) for t in range(0, 31, 2)]
    assert "G4" in _ids(evaluate_invariants(samples))


def test_g4_balanced_is_fine():
    samples = [_busy(t) for t in range(0, 31, 2)]
    assert "G4" not in _ids(evaluate_invariants(samples))


# --- G5: reserved running session run_id matches reservation ----------------------------


def test_g5_session_run_id_mismatch_sustained():
    samples = [_busy(t, reserved=True, sess_run="run-old", resv_run="run-7") for t in range(0, 31, 2)]
    assert "G5" in _ids(evaluate_invariants(samples))


def test_g5_matching_run_ids_is_fine():
    samples = [_busy(t, reserved=True, sess_run="run-7", resv_run="run-7") for t in range(0, 31, 2)]
    assert "G5" not in _ids(evaluate_invariants(samples))


def test_g5_reserved_without_running_session_is_inert():
    samples = [mk(t, reserved=True, resv_run="run-7", db_sessions=0) for t in range(0, 31, 2)]
    assert "G5" not in _ids(evaluate_invariants(samples))


def test_g5_unreserved_is_inert():
    samples = [_busy(t, reserved=False, sess_run="run-x", resv_run=None) for t in range(0, 31, 2)]
    assert "G5" not in _ids(evaluate_invariants(samples))


# --- G6: flap ---------------------------------------------------------------------------


def test_g6_appium_reachability_flap_same_backend_state():
    samples = [mk(t) for t in range(0, 9, 2)]
    samples += [mk(t, appium_reachable=False) for t in range(10, 17, 2)]
    samples += [mk(t) for t in range(18, 25, 2)]
    assert "G6" in _ids(evaluate_invariants(samples))


def test_g6_no_flap_when_backend_state_changed_across_gap():
    samples = [mk(t) for t in range(0, 9, 2)]
    samples += [mk(t, state="offline", appium_reachable=False) for t in range(10, 17, 2)]
    samples += [mk(t) for t in range(18, 25, 2)]
    assert "G6" not in _ids(evaluate_invariants(samples))


def test_g6_no_flap_when_active_routes_changed_across_gap():
    # active_routes moved across the gap => a real data-plane transition, not a flap.
    # A REAL transition has a DB side too (the session that created the route) — without
    # it the churn pass would rightly flag an uncorrelated route appearance.
    samples = [mk(t, active_routes=0) for t in range(0, 9, 2)]
    samples += [mk(t, appium_reachable=False, active_routes=0) for t in range(10, 15, 2)]
    samples += [
        mk(t, active_routes=1, route_sessions=("s",), db_sessions=1, db_ids=("s",),
           appium_sessions=("s",), state="busy")
        for t in range(16, 23, 2)
    ]
    assert "G6" not in _ids(evaluate_invariants(samples))


def test_g6_exempt_override():
    samples = [mk(t) for t in range(0, 9, 2)]
    samples += [mk(t, appium_reachable=False) for t in range(10, 17, 2)]
    samples += [mk(t) for t in range(18, 25, 2)]
    out = evaluate_invariants(samples, overrides=GridInvariantOverrides(exempt=("G6",)))
    assert "G6" not in _ids(out)


def test_g6_ignores_verifying_phase():
    samples = [mk(t) for t in range(0, 9, 2)]
    samples += [mk(t, state="verifying", appium_reachable=False) for t in range(10, 17, 2)]
    samples += [mk(t) for t in range(18, 25, 2)]
    assert "G6" not in _ids(evaluate_invariants(samples))


# --- G7: queue hygiene ------------------------------------------------------------------


def test_g7_waiting_ticket_past_timeout_sustained():
    samples = [mk(t, waiting_age=400.0, qt=330.0) for t in range(0, 31, 2)]
    assert "G7" in _ids(evaluate_invariants(samples))


def test_g7_claimed_without_row_sustained():
    samples = [mk(t, claimed_no_row=1) for t in range(0, 31, 2)]
    assert "G7" in _ids(evaluate_invariants(samples))


def test_g7_young_waiting_ticket_is_fine():
    samples = [mk(t, waiting_age=10.0, qt=330.0) for t in range(0, 31, 2)]
    assert "G7" not in _ids(evaluate_invariants(samples))


def test_g7_no_queue_rows_is_fine():
    samples = [mk(t) for t in range(0, 31, 2)]
    assert "G7" not in _ids(evaluate_invariants(samples))


def test_g7v2_claimed_stale_sustained_is_violation():
    samples = [
        mk(0.0, db_queue_claimed_stale=1),
        mk(20.0, db_queue_claimed_stale=1),  # 20s > G7 grace (15s)
    ]
    out = evaluate_invariants(samples)
    assert _ids(out) == ["G7"]
    assert "claimed" in out[0].detail


def test_g7v2_claimed_stale_under_grace_is_clean():
    samples = [
        mk(0.0, db_queue_claimed_stale=1),
        mk(10.0, db_queue_claimed_stale=1),  # 10s <= 15s grace
    ]
    assert evaluate_invariants(samples) == []


def test_g7v2_zero_stale_not_applicable():
    samples = [mk(0.0), mk(20.0)]
    assert evaluate_invariants(samples) == []


# --- G8: pending leak -------------------------------------------------------------------


def test_g8_pending_past_claim_window_sustained():
    samples = [mk(t, pending_age=200.0, cw=135.0) for t in range(0, 31, 2)]
    assert "G8" in _ids(evaluate_invariants(samples))


def test_g8_young_pending_is_fine():
    samples = [mk(t, pending_age=10.0, cw=135.0) for t in range(0, 31, 2)]
    assert "G8" not in _ids(evaluate_invariants(samples))


def test_g8_no_pending_is_fine():
    samples = [mk(t) for t in range(0, 31, 2)]
    assert "G8" not in _ids(evaluate_invariants(samples))


# --- G9: route-map coherence ------------------------------------------------------------


def test_g9_route_without_db_row_sustained():
    samples = [mk(t, route_no_db=("ghost",), active_routes=1) for t in range(0, 31, 2)]
    assert "G9" in _ids(evaluate_invariants(samples))


def test_g9_all_routes_backed_by_db_is_fine():
    samples = [_busy(t) for t in range(0, 31, 2)]
    assert "G9" not in _ids(evaluate_invariants(samples))


def test_g9_not_judged_when_router_channel_dark():
    samples = [mk(t, route_no_db=("ghost",), router_error="down") for t in range(0, 31, 2)]
    assert "G9" not in _ids(evaluate_invariants(samples))


# --- G9b: inverse route-map coherence ----------------------------------------------------


def test_g9b_unrouted_running_session_sustained_is_violation():
    samples = [
        mk(0.0, db_sessions_without_route=("sess-x",)),
        mk(70.0, db_sessions_without_route=("sess-x",)),  # 70s > 60s grace
    ]
    out = evaluate_invariants(samples)
    assert _ids(out) == ["G9b"]


def test_g9b_under_route_reconcile_grace_is_clean():
    samples = [
        mk(0.0, db_sessions_without_route=("sess-x",)),
        mk(50.0, db_sessions_without_route=("sess-x",)),  # 50s <= 60s
    ]
    assert evaluate_invariants(samples) == []


def test_g9b_router_channel_dark_is_not_applicable():
    samples = [
        mk(0.0, db_sessions_without_route=("sess-x",), router_error="boom"),
        mk(70.0, db_sessions_without_route=("sess-x",), router_error="boom"),
    ]
    out = evaluate_invariants(samples)
    assert all(v.invariant_id != "G9b" for v in out)  # unobservable may fire; G9b must not


# --- verifying / continuity / grace -----------------------------------------------------


def test_verifying_exempt_and_resets_divergence():
    samples = [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=300.0)
               for t in range(0, 11, 2)]
    samples += [mk(t, state="verifying") for t in range(12, 61, 2)]
    samples += [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=300.0)
                for t in range(62, 73, 2)]
    out = evaluate_invariants(samples)
    assert "G1" not in _ids(out) and "G4" not in _ids(out)


def test_channel_error_ticks_do_not_reset_continuity():
    # diverged 0..8, appium-channel error 10..20, diverged 22..30: one 30s run > grace
    samples = [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=300.0)
               for t in range(0, 9, 2)]
    samples += [mk(t, appium_error="blip", appium_sessions=None) for t in range(10, 21, 2)]
    samples += [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=300.0)
                for t in range(22, 31, 2)]
    assert "G1" in _ids(evaluate_invariants(samples))


def test_open_subgrace_divergence_reported_pending_not_violation():
    samples = [mk(t) for t in range(0, 21, 2)]
    samples += [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=300.0)
                for t in range(22, 31, 2)]  # 8s open
    assert "G1" not in _ids(evaluate_invariants(samples))
    pending = open_divergences(samples)
    assert "G1" in pending and "G4" in pending


def test_open_divergence_past_grace_is_violation_at_window_end():
    samples = [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=300.0)
               for t in range(0, 31, 2)]
    assert "G1" in _ids(evaluate_invariants(samples))
    assert "G1" not in open_divergences(samples)


def test_override_exempt_and_extended_grace():
    samples = [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=300.0)
               for t in range(0, 31, 2)]
    o_exempt = GridInvariantOverrides(exempt=("G1", "G4"))
    assert evaluate_invariants(samples, overrides=o_exempt) == []
    o_grace = GridInvariantOverrides(grace=(("G1", 60.0), ("G4", 60.0)))
    assert evaluate_invariants(samples, overrides=o_grace) == []


def test_grace_boundary_exactly_at_grace_is_not_violation():
    # duration == grace must NOT violate (strict >): G1 grace is 15s, run lasts exactly 15s
    samples = [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=300.0)
               for t in range(0, 16, 5)]  # ts 0,5,10,15
    samples += [_busy(t) for t in range(20, 41, 5)]
    assert "G1" not in _ids(evaluate_invariants(samples))


# --- establishment exemption ------------------------------------------------------------


def test_g1_g4_establishing_session_is_exempt():
    # DB row exists, Appium doesn't enumerate it yet, row is YOUNG: still creating it
    samples = [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=float(t) + 1.0)
               for t in range(0, 61, 2)]
    ids = _ids(evaluate_invariants(samples))
    assert "G1" not in ids and "G4" not in ids


def test_g4_old_row_without_appium_session_is_still_a_phantom():
    samples = [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=300.0 + t)
               for t in range(0, 31, 2)]
    ids = _ids(evaluate_invariants(samples))
    assert "G1" in ids and "G4" in ids


def test_g4_orphan_unaffected_by_establishment():
    samples = [mk(t, appium_sessions=("s",), db_sessions=0) for t in range(0, 31, 2)]
    assert "G4" in _ids(evaluate_invariants(samples))


# --- per-channel unobservable -----------------------------------------------------------


def test_unobservable_backend_channel_dark():
    samples = [mk(t) for t in range(0, 5, 2)]
    samples += [mk(t, backend_error="status: timeout") for t in range(6, 45, 2)]  # 38s
    samples += [mk(t) for t in range(46, 51, 2)]
    out = evaluate_invariants(samples)
    assert "grid_unobservable" in _ids(out)


def test_unobservable_appium_channel_dark():
    samples = [mk(t) for t in range(0, 5, 2)]
    samples += [mk(t, appium_error="connrefused", appium_sessions=None) for t in range(6, 45, 2)]
    samples += [mk(t) for t in range(46, 51, 2)]
    assert "grid_unobservable" in _ids(evaluate_invariants(samples))


def test_unobservable_router_channel_dark():
    samples = [mk(t) for t in range(0, 5, 2)]
    samples += [mk(t, router_error="metrics down") for t in range(6, 45, 2)]
    samples += [mk(t) for t in range(46, 51, 2)]
    assert "grid_unobservable" in _ids(evaluate_invariants(samples))


def test_isolated_channel_error_ticks_are_not_unobservable():
    samples = []
    for t in range(0, 60, 2):
        samples.append(mk(t, router_error="blip") if t % 10 == 0 else mk(t))
    assert "grid_unobservable" not in _ids(evaluate_invariants(samples))


def test_one_channel_dark_does_not_mask_another_channel_health():
    # router dark the whole window, but the appium/backend axes stay observable.
    # Only ONE grid_unobservable (router); no spurious extras.
    samples = [mk(t, router_error="down") for t in range(0, 60, 2)]
    out = [v for v in evaluate_invariants(samples) if v.invariant_id == "grid_unobservable"]
    assert len(out) == 1 and "router" in out[0].detail


def test_unobservable_boundary_exactly_at_grace_is_not_violation():
    samples = [mk(t, appium_error="x", appium_sessions=None) for t in range(0, 31, 5)]  # 30s
    assert "grid_unobservable" not in _ids(evaluate_invariants(samples))
    samples = [mk(t, appium_error="x", appium_sessions=None) for t in (0, 31)]  # 31s
    assert "grid_unobservable" in _ids(evaluate_invariants(samples))


def test_unobservable_and_content_violation_coexist():
    samples = [mk(t) for t in range(0, 5, 2)]
    samples += [mk(t, backend_error="down") for t in range(6, 45, 2)]
    samples += [mk(t, state="busy", db_sessions=1, db_ids=("s",), sess_age=300.0)
                for t in range(46, 80, 2)]
    ids = _ids(evaluate_invariants(samples))
    assert "grid_unobservable" in ids and "G1" in ids


def test_empty_and_all_error_timelines_are_handled():
    assert evaluate_invariants([]) == []
    all_err = [mk(t, backend_error="dead", appium_error="dead", router_error="dead", appium_sessions=None)
               for t in range(0, 60, 2)]
    # three channels dark -> three grid_unobservable findings, nothing else
    ids = set(_ids(evaluate_invariants(all_err)))
    assert ids == {"grid_unobservable"}
    assert open_divergences(all_err) == []


# --- F-4: appium-dark correlated with backend node state --------------------------------


def test_unobservable_appium_dark_while_node_running_is_finding():
    # appium dark 38s while the backend channel says the node is running -> genuine blindness.
    samples = [mk(t) for t in range(0, 5, 2)]
    samples += [mk(t, appium_error="connrefused", appium_sessions=None, node_state="running")
                for t in range(6, 45, 2)]
    samples += [mk(t) for t in range(46, 51, 2)]
    assert "grid_unobservable" in _ids(evaluate_invariants(samples))


def test_unobservable_appium_dark_while_node_stopped_is_not_finding():
    # Operator-stopped node (S17): appium goes dark BY DESIGN. The backend channel reports
    # the node stopped, so the darkness is cross-channel agreement, not unobservability.
    samples = [mk(t, state="offline", node_state="stopped",
                  appium_error="connrefused", appium_sessions=None)
               for t in range(0, 60, 2)]
    assert "grid_unobservable" not in _ids(evaluate_invariants(samples))


def test_unobservable_appium_dark_with_backend_channel_also_dark_is_finding():
    # No backend truth to correlate against (backend channel errored too) -> the appium
    # darkness cannot be excused and still counts.
    samples = [mk(t) for t in range(0, 5, 2)]
    samples += [mk(t, appium_error="connrefused", appium_sessions=None, backend_error="status down")
                for t in range(6, 45, 2)]
    samples += [mk(t) for t in range(46, 51, 2)]
    assert "grid_unobservable" in _ids(evaluate_invariants(samples))


def test_unobservable_backend_and_router_channels_ignore_node_state():
    # The backend/router channels' unobservable logic is unchanged: a backend channel dark
    # past grace is a finding regardless of the (now-stale) node_state reading it carries.
    samples = [mk(t) for t in range(0, 5, 2)]
    samples += [mk(t, backend_error="timeout", node_state="stopped") for t in range(6, 45, 2)]
    samples += [mk(t) for t in range(46, 51, 2)]
    ids = _ids(evaluate_invariants(samples))
    assert "grid_unobservable" in ids


# --- state_held -------------------------------------------------------------------------


def _s(i, *, state="available", reachable=True, err=None):
    return GridSample(
        ts=float(i), iso=f"00:00:{i:02d}", operational_state=state, is_reserved=False,
        appium_reachable=reachable, appium_sessions=(), backend_error=err,
    )


def test_state_held_true_when_all_samples_match():
    assert state_held([_s(0), _s(1), _s(2)], "available") is True


def test_state_held_false_on_any_divergent_sample():
    assert state_held([_s(0), _s(1, state="offline"), _s(2)], "available") is False


def test_state_held_skips_error_and_stateless_ticks():
    samples = [_s(0, state=None, err="boom"), _s(1)]
    assert state_held(samples, "available") is True


def test_channels_constant():
    assert CHANNELS == ("backend", "appium", "router")


# --- G6 route-churn-while-up + overridable grid_unobservable -----------------------------


def _ids2(violations):
    return [v.invariant_id for v in violations]


def test_g6_route_churn_while_appium_up_flagged():
    # route entry flaps present -> absent -> present while DB session ids never change and
    # the Appium probe stays up the whole time: invisible to the gap-based flap pass.
    samples = [
        _busy(0),
        _busy(10, route_sessions=()),       # route vanished, DB unchanged, appium up
        _busy(20),
    ]
    out = evaluate_invariants(samples)
    assert any(v.invariant_id == "G6" and "route map changed" in v.detail for v in out)


def test_g6_route_change_correlated_with_db_change_not_flagged():
    samples = [
        mk(0),
        _busy(5, route_sessions=()),        # DB row lands (db_ids change), route lags
        _busy(15),                          # route follows within the correlation window
    ]
    out = [v for v in evaluate_invariants(samples) if "route map changed" in v.detail]
    assert out == []


def test_g6_route_churn_respects_exempt_override():
    samples = [
        _busy(0),
        _busy(10, route_sessions=()),
        _busy(20),
    ]
    out = evaluate_invariants(samples, overrides=GridInvariantOverrides(exempt=("G6",)))
    assert not [v for v in out if "route map changed" in v.detail]


def test_unobservable_exempt_override():
    dark = [mk(t, backend_error="conn refused") for t in range(0, 60, 5)]
    assert "grid_unobservable" in _ids2(evaluate_invariants(dark))
    out = evaluate_invariants(dark, overrides=GridInvariantOverrides(exempt=("grid_unobservable",)))
    assert "grid_unobservable" not in _ids2(out)


def test_unobservable_grace_override():
    dark = [mk(t, backend_error="conn refused") for t in range(0, 60, 5)]
    out = evaluate_invariants(
        dark, overrides=GridInvariantOverrides(grace=(("grid_unobservable", 120.0),)))
    assert "grid_unobservable" not in _ids2(out)
    # but a window past even the extended grace still flags
    long_dark = [mk(t, backend_error="conn refused") for t in range(0, 200, 5)]
    out2 = evaluate_invariants(
        long_dark, overrides=GridInvariantOverrides(grace=(("grid_unobservable", 120.0),)))
    assert "grid_unobservable" in _ids2(out2)
