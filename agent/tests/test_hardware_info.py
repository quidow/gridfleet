from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_app.host import hardware_info


def _reset_cache() -> None:
    hardware_info.collect.cache_clear()


def test_collect_returns_expected_shape_on_darwin() -> None:
    _reset_cache()
    uname = MagicMock(system="Darwin", release="23.5.0", machine="arm64")
    vm = MagicMock(total=32 * 1024**3)
    disk = MagicMock(total=1024 * 1024**3)

    with (
        patch("agent_app.host.hardware_info.platform.uname", return_value=uname),
        patch("agent_app.host.hardware_info.platform.system", return_value="Darwin"),
        patch("agent_app.host.hardware_info.psutil.virtual_memory", return_value=vm),
        patch("agent_app.host.hardware_info.psutil.disk_usage", return_value=disk),
        patch("agent_app.host.hardware_info.psutil.cpu_count", return_value=12),
        patch(
            "agent_app.host.hardware_info.subprocess.check_output",
            side_effect=["macOS\n", "14.5\n", "Apple M2 Pro\n"],
        ),
    ):
        info = hardware_info.collect()

    assert info["os_version"] == "macOS 14.5"
    assert info["kernel_version"] == "Darwin 23.5.0"
    assert info["cpu_arch"] == "arm64"
    assert info["cpu_model"] == "Apple M2 Pro"
    assert info["cpu_cores"] == 12
    assert info["total_memory_mb"] == 32 * 1024
    assert info["total_disk_gb"] == 1024


def test_collect_returns_expected_shape_on_linux(tmp_path: Path) -> None:
    _reset_cache()
    os_release = tmp_path / "os-release"
    os_release.write_text('PRETTY_NAME="Ubuntu 22.04.3 LTS"\nID=ubuntu\n')
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("processor\t: 0\nmodel name\t: Intel Xeon E5-2680\n")

    uname = MagicMock(system="Linux", release="5.15.0-89-generic", machine="x86_64")
    vm = MagicMock(total=16 * 1024**3)
    disk = MagicMock(total=500 * 1024**3)

    real_open = open

    def fake_open(path: str, *args: object, **kwargs: object) -> object:
        if path == "/etc/os-release":
            return real_open(os_release, *args, **kwargs)  # type: ignore[arg-type]
        if path == "/proc/cpuinfo":
            return real_open(cpuinfo, *args, **kwargs)  # type: ignore[arg-type]
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch("agent_app.host.hardware_info.platform.uname", return_value=uname),
        patch("agent_app.host.hardware_info.platform.system", return_value="Linux"),
        patch("agent_app.host.hardware_info.psutil.virtual_memory", return_value=vm),
        patch("agent_app.host.hardware_info.psutil.disk_usage", return_value=disk),
        patch("agent_app.host.hardware_info.psutil.cpu_count", return_value=8),
        patch("builtins.open", side_effect=fake_open),
    ):
        info = hardware_info.collect()

    assert info["os_version"] == "Ubuntu 22.04.3 LTS"
    assert info["kernel_version"] == "Linux 5.15.0-89-generic"
    assert info["cpu_arch"] == "x86_64"
    assert info["cpu_model"] == "Intel Xeon E5-2680"
    assert info["cpu_cores"] == 8
    assert info["total_memory_mb"] == 16 * 1024
    assert info["total_disk_gb"] == 500


def test_collect_returns_none_for_failing_helpers() -> None:
    _reset_cache()
    uname = MagicMock(system="Darwin", release="23.5.0", machine="arm64")
    vm = MagicMock(total=32 * 1024**3)
    disk = MagicMock(total=1024 * 1024**3)

    with (
        patch("agent_app.host.hardware_info.platform.uname", return_value=uname),
        patch("agent_app.host.hardware_info.platform.system", return_value="Darwin"),
        patch("agent_app.host.hardware_info.psutil.virtual_memory", return_value=vm),
        patch("agent_app.host.hardware_info.psutil.disk_usage", return_value=disk),
        patch("agent_app.host.hardware_info.psutil.cpu_count", return_value=12),
        patch(
            "agent_app.host.hardware_info.subprocess.check_output",
            side_effect=FileNotFoundError("sw_vers"),
        ),
    ):
        info = hardware_info.collect()

    assert info["os_version"] is None
    assert info["cpu_model"] is None
    assert info["kernel_version"] == "Darwin 23.5.0"
    assert info["cpu_cores"] == 12


def test_collect_caches_result() -> None:
    _reset_cache()
    uname = MagicMock(system="Darwin", release="23.5.0", machine="arm64")
    vm = MagicMock(total=32 * 1024**3)
    disk = MagicMock(total=1024 * 1024**3)

    cpu_count = MagicMock(return_value=12)
    with (
        patch("agent_app.host.hardware_info.platform.uname", return_value=uname),
        patch("agent_app.host.hardware_info.platform.system", return_value="Darwin"),
        patch("agent_app.host.hardware_info.psutil.virtual_memory", return_value=vm),
        patch("agent_app.host.hardware_info.psutil.disk_usage", return_value=disk),
        patch("agent_app.host.hardware_info.psutil.cpu_count", new=cpu_count),
        patch(
            "agent_app.host.hardware_info.subprocess.check_output",
            side_effect=["macOS\n", "14.5\n", "Apple M2 Pro\n"],
        ),
    ):
        hardware_info.collect()
        hardware_info.collect()

    assert cpu_count.call_count == 1
