from __future__ import annotations

import importlib


def test_model_modules_import_in_codeql_reported_orders() -> None:
    modules = [
        "app.hosts.models",
        "app.hosts.models",
        "app.devices.models",
        "app.appium_nodes.models",
        "app.devices.models",
        "app.devices.models",
        "app.sessions.models",
        "app.runs.models",
        "app.packs.models",
        "app.packs.models",
    ]

    for module in modules:
        importlib.import_module(module)
