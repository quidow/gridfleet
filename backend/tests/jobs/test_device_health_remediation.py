from sqlalchemy import inspect as sa_inspect

from app.devices.models import Device
from app.jobs import JOB_KIND_DEVICE_HEALTH_REMEDIATION
from app.jobs.models import Job


def test_device_health_remediation_schema_contract() -> None:
    assert JOB_KIND_DEVICE_HEALTH_REMEDIATION == "device_health_remediation"
    assert {
        "remediation_device_id",
        "failure_episode_id",
        "remediation_action_id",
    } <= {column.key for column in sa_inspect(Job).columns}
    assert "failure_episode_id" in {column.key for column in sa_inspect(Device).columns}
