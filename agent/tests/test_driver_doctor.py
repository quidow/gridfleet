import asyncio
from unittest.mock import patch

from agent_app.driver_doctor import parse_driver_doctor_output, run_driver_doctor


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self) -> asyncio.Future[tuple[bytes, bytes]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bytes, bytes]] = loop.create_future()
        future.set_result((self._stdout, self._stderr))
        return future


def test_parse_driver_doctor_json_success() -> None:
    assert parse_driver_doctor_output('{"ok": true, "required_fixes": 0, "issues": []}') == {
        "ok": True,
        "required_fixes": 0,
        "issues": [],
    }


def test_parse_driver_doctor_json_wrapped_in_appium_logs() -> None:
    output = '[Appium] Running doctor\n{"ok": true, "required_fixes": 0, "issues": []}\n[Appium] Done'

    assert parse_driver_doctor_output(output) == {
        "ok": True,
        "required_fixes": 0,
        "issues": [],
    }


def test_parse_driver_doctor_required_checks_shape() -> None:
    output = """
    {
      "required": [
        {"ok": true, "message": "Java found"},
        {"ok": false, "message": "ANDROID_HOME is not set"}
      ],
      "optional": [
        {"ok": false, "message": "Optional check failed"}
      ]
    }
    """

    assert parse_driver_doctor_output(output) == {
        "ok": False,
        "required_fixes": 1,
        "issues": ["ANDROID_HOME is not set"],
    }


def test_parse_driver_doctor_text_failure() -> None:
    result = parse_driver_doctor_output("✓ Java found\n✗ ANDROID_HOME is not set\n1 required fix needed")

    assert result == {"ok": False, "required_fixes": 1, "issues": ["ANDROID_HOME is not set"]}


def test_parse_driver_doctor_text_with_log_prefix() -> None:
    result = parse_driver_doctor_output("[AppiumDoctor] ✖ Java not found\n1 fix needed")

    assert result == {"ok": False, "required_fixes": 1, "issues": ["Java not found"]}


def test_parse_driver_doctor_no_checks() -> None:
    result = parse_driver_doctor_output('\u2139 The driver "roku" does not export any doctor checks')

    assert result == {"ok": True, "required_fixes": 0, "issues": [], "available": False}


def test_parse_driver_doctor_unexpected_output() -> None:
    assert parse_driver_doctor_output("something changed") == {
        "ok": False,
        "required_fixes": -1,
        "issues": ["parse error"],
    }


async def test_run_driver_doctor_parses_nonzero_json_output() -> None:
    proc = _FakeProc(1, stdout=b'{"ok": false, "required_fixes": 1, "issues": ["Java not found"]}')

    with (
        patch("agent_app.driver_doctor._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.driver_doctor._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.driver_doctor.asyncio.create_subprocess_exec", return_value=proc),
    ):
        result = await run_driver_doctor("uiautomator2")

    assert result == {"ok": False, "required_fixes": 1, "issues": ["Java not found"]}


async def test_run_driver_doctor_parses_summary_from_stderr_when_stdout_is_numeric() -> None:
    proc = _FakeProc(
        0,
        stdout=b"8",
        stderr=b"info Doctor ### Diagnostic completed, 0 required fixes needed, 2 optional fixes possible. ###",
    )

    with (
        patch("agent_app.driver_doctor._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.driver_doctor._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.driver_doctor.asyncio.create_subprocess_exec", return_value=proc),
    ):
        result = await run_driver_doctor("xcuitest")

    assert result == {"ok": True, "required_fixes": 0, "issues": []}


async def test_run_driver_doctor_falls_back_when_json_mode_returns_only_numeric_marker() -> None:
    proc_json = _FakeProc(0, stdout=b"0")
    proc_text = _FakeProc(0, stdout='\u2139 The driver "roku" does not export any doctor checks'.encode())

    with (
        patch("agent_app.driver_doctor._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.driver_doctor._build_env", return_value={"PATH": "/usr/bin"}),
        patch(
            "agent_app.driver_doctor.asyncio.create_subprocess_exec",
            side_effect=[proc_json, proc_text],
        ) as create_proc,
    ):
        result = await run_driver_doctor("roku")

    assert result == {"ok": True, "required_fixes": 0, "issues": [], "available": False}
    assert create_proc.call_args_list[0].args[:5] == ("/usr/local/bin/appium", "driver", "doctor", "roku", "--json")
    assert create_proc.call_args_list[1].args[:4] == ("/usr/local/bin/appium", "driver", "doctor", "roku")


async def test_run_driver_doctor_timeout() -> None:
    with (
        patch("agent_app.driver_doctor._find_appium", return_value="/usr/local/bin/appium"),
        patch("agent_app.driver_doctor._build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.driver_doctor.asyncio.create_subprocess_exec", return_value=_FakeProc(0)),
        patch("agent_app.driver_doctor.asyncio.wait_for", side_effect=TimeoutError),
    ):
        result = await run_driver_doctor("uiautomator2")

    assert result == {"ok": False, "required_fixes": -1, "issues": ["doctor timed out"]}
