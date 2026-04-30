import asyncio
import json
import logging
import re
from typing import Any

from agent_app.appium_process import _build_env
from agent_app.tool_paths import find_appium as _find_appium

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SUMMARY_RE = re.compile(
    r"(?:(?P<count>\d+)\s+required\s+fix|(?P<needed>\d+)\s+fix(?:es)?\s+needed)",
    re.IGNORECASE,
)
_FAIL_PREFIX_RE = re.compile(r"^\s*(?:✗|✖|x|\[x\]|\[fail(?:ed)?\])\s*(?P<issue>.+)$", re.IGNORECASE)
_FAIL_GLYPH_RE = re.compile(r"(?:✗|✖)\s*(?P<issue>.+)$")
_NO_CHECKS_RE = re.compile(r"does not (?:export|have|provide) any doctor checks|no doctor checks", re.IGNORECASE)


def _clean_line(value: str) -> str:
    return _ANSI_RE.sub("", value).strip()


def _extract_json_payload(output: str) -> object | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char not in "{[":
            continue
        try:
            payload, _end = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        decoded_payload: object = payload
        return decoded_payload
    return None


def _string_value(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _issue_from_check(check: dict[str, Any], *, optional_context: bool | None = None) -> tuple[str, bool] | None:
    has_issue_text = any(_string_value(check.get(key)) for key in ("message", "error", "detail", "name", "description"))
    has_child_failures = any(
        key in check
        for key in ("issues", "required_fixes", "requiredFixes", "failures", "errors", "checks", "required")
    )
    ok_value = check.get("ok", check.get("passed", check.get("success")))
    status = str(check.get("status", "")).lower()
    failed = ok_value is False or status in {"fail", "failed", "error", "missing"}
    if not failed:
        return None
    if not has_issue_text and has_child_failures:
        return None
    optional = check.get("optional")
    if not isinstance(optional, bool):
        optional = bool(optional_context)
    for key in ("message", "error", "detail", "name", "description"):
        value = _string_value(check.get(key))
        if value:
            return value, optional
    return "doctor check failed", optional


def _collect_json_issues(data: object, *, optional_context: bool | None = None) -> list[tuple[str, bool]]:
    issues: list[tuple[str, bool]] = []
    if isinstance(data, list):
        for item in data:
            issues.extend(_collect_json_issues(item, optional_context=optional_context))
        return issues

    if not isinstance(data, dict):
        return issues

    issue = _issue_from_check(data, optional_context=optional_context)
    if issue is not None:
        issues.append(issue)

    for key, value in data.items():
        lowered = key.lower()
        child_optional = optional_context
        if lowered in {"optional", "optionalchecks", "optional_checks"} and isinstance(value, list):
            child_optional = True
        elif lowered in {"required", "requiredchecks", "required_checks", "requiredfixes", "required_fixes"}:
            child_optional = False

        if isinstance(value, (dict, list)) or (lowered in {"issues", "failures", "errors"} and isinstance(value, list)):
            issues.extend(_collect_json_issues(value, optional_context=child_optional))
    return issues


def _parse_json_doctor(data: object) -> dict[str, object] | None:
    if isinstance(data, list):
        issues_with_optional = _collect_json_issues(data)
        issues = [message for message, optional in issues_with_optional if not optional]
        return {"ok": not issues, "required_fixes": len(issues), "issues": issues}

    if not isinstance(data, dict):
        return None

    issues_with_optional = _collect_json_issues(data)
    issues = [message for message, optional in issues_with_optional if not optional]
    for key in ("issues", "required_fixes", "requiredFixes", "failures", "errors"):
        value = data.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    issues.append(item.strip())
                elif isinstance(item, dict):
                    issue = _issue_from_check(item)
                    if issue:
                        message, optional = issue
                        if not optional:
                            issues.append(message)

    checks = data.get("checks")
    if isinstance(checks, list):
        for item in checks:
            if isinstance(item, dict):
                issue = _issue_from_check(item)
                if issue:
                    message, optional = issue
                    if not optional:
                        issues.append(message)

    raw_required = data.get(
        "required_fixes",
        data.get("requiredFixes", data.get("requiredFixesCount", data.get("fixesNeeded", data.get("fixes_needed")))),
    )
    if isinstance(raw_required, int):
        required_fixes = raw_required
    elif isinstance(raw_required, list):
        required_fixes = len([item for item in _collect_json_issues(raw_required, optional_context=False)])
    else:
        required_fixes = len(issues)

    raw_ok = data.get("ok", data.get("success", data.get("passed")))
    status = _string_value(data.get("status"))
    if isinstance(raw_ok, bool):
        ok = raw_ok
    elif status:
        ok = status.lower() in {"ok", "pass", "passed", "success", "successful"}
    else:
        ok = required_fixes == 0
    return {"ok": ok and required_fixes == 0, "required_fixes": required_fixes, "issues": issues}


def parse_driver_doctor_output(output: str) -> dict[str, Any]:
    cleaned = output.strip()
    if not cleaned:
        return {"ok": False, "required_fixes": -1, "issues": ["doctor returned empty output"]}
    if _NO_CHECKS_RE.search(cleaned):
        return {"ok": True, "required_fixes": 0, "issues": [], "available": False}

    parsed = _parse_json_doctor(_extract_json_payload(cleaned))
    if parsed is not None:
        return parsed

    issues: list[str] = []
    required_fixes: int | None = None
    for raw_line in cleaned.splitlines():
        line = _clean_line(raw_line)
        if not line:
            continue
        summary = _SUMMARY_RE.search(line)
        if summary:
            required_fixes = int(summary.group("count") or summary.group("needed"))
            continue
        failed = _FAIL_PREFIX_RE.match(line)
        if failed:
            issues.append(failed.group("issue").strip())
            continue
        failed = _FAIL_GLYPH_RE.search(line)
        if failed:
            issues.append(failed.group("issue").strip())

    if required_fixes is None:
        if issues:
            required_fixes = len(issues)
        else:
            logger.warning("Unable to parse appium driver doctor output: %s", cleaned)
            return {"ok": False, "required_fixes": -1, "issues": ["parse error"]}
    if required_fixes == 0:
        return {"ok": True, "required_fixes": 0, "issues": []}
    if issues or required_fixes > 0:
        return {"ok": False, "required_fixes": required_fixes, "issues": issues}

    logger.warning("Unable to parse appium driver doctor output: %s", cleaned)
    return {"ok": False, "required_fixes": -1, "issues": ["parse error"]}


async def run_driver_doctor(
    driver_name: str,
    *,
    appium_bin: str | None = None,
    appium_home: str | None = None,
) -> dict[str, Any]:
    async def _run_appium_doctor(*extra_args: str) -> tuple[str, str] | dict[str, Any]:
        try:
            proc = await asyncio.create_subprocess_exec(
                appium,
                "driver",
                "doctor",
                driver_name,
                *extra_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except FileNotFoundError:
            return {"ok": False, "required_fixes": -1, "issues": ["appium binary not found"]}
        except TimeoutError:
            return {"ok": False, "required_fixes": -1, "issues": ["doctor timed out"]}
        return stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()

    appium = appium_bin or _find_appium()
    env = _build_env(appium_bin=appium_bin, appium_home=appium_home)
    output_result = await _run_appium_doctor("--json")
    if isinstance(output_result, dict):
        return output_result
    output, error_output = output_result

    # Some Appium versions print the useful doctor report to stderr and a bare
    # numeric value to stdout even when --json is passed. Parse both streams
    # together so the summary line is not hidden by that stdout marker.
    combined_output = "\n".join(part for part in (output, error_output) if part)
    parsed = parse_driver_doctor_output(combined_output)
    if parsed["required_fixes"] == -1 and output.isdigit() and not error_output:
        fallback_result = await _run_appium_doctor()
        if isinstance(fallback_result, dict):
            return fallback_result
        fallback_output, fallback_error_output = fallback_result
        fallback_combined = "\n".join(part for part in (fallback_output, fallback_error_output) if part)
        parsed = parse_driver_doctor_output(fallback_combined)

    if parsed["required_fixes"] == -1 and error_output:
        parsed["issues"] = [*parsed["issues"], error_output]
    return parsed
