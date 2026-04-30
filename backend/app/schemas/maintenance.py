from pydantic import BaseModel


class DeviceMaintenanceUpdate(BaseModel):
    drain: bool = False
