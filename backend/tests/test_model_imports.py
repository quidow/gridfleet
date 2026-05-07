from __future__ import annotations

import importlib


def test_model_modules_import_in_codeql_reported_orders() -> None:
    modules = [
        "app.models.host_terminal_session",
        "app.models.host",
        "app.models.device",
        "app.models.appium_node",
        "app.models.device_event",
        "app.models.device_reservation",
        "app.models.session",
        "app.models.test_run",
        "app.models.driver_pack",
        "app.models.host_pack_installation",
    ]

    for module in modules:
        importlib.import_module(module)
