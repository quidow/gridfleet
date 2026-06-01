from app.devices.protocols import InventoryExportProtocol
from app.devices.services.inventory_export import InventoryExportService


def test_inventory_export_service_satisfies_protocol() -> None:
    assert isinstance(InventoryExportService(), InventoryExportProtocol)
