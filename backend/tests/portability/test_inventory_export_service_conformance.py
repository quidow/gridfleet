from app.portability.protocols import InventoryExportProtocol
from app.portability.services.inventory import InventoryExportService


def test_inventory_export_service_satisfies_protocol() -> None:
    assert isinstance(InventoryExportService(), InventoryExportProtocol)
