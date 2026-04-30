from __future__ import annotations

import copy
import io
import os
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_TEMPLATES_DIR = Path(
    os.environ.get(
        "GRIDFLEET_DRIVER_PACK_TEMPLATES_DIR",
        str(Path(__file__).resolve().parents[3] / "driver-packs" / "templates"),
    )
)


@dataclass(frozen=True)
class TemplateDescriptor:
    id: str
    display_name: str
    target_driver_summary: str
    source_pack_id: str
    prerequisite_host_tools: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Template:
    descriptor: TemplateDescriptor
    raw_yaml: str
    manifest_dict: dict[str, Any]


_TEMPLATE_CACHE: dict[str, Template] | None = None


def _load_all_templates() -> dict[str, Template]:
    result: dict[str, Template] = {}
    if not _TEMPLATES_DIR.exists():
        return result
    for path in sorted(_TEMPLATES_DIR.glob("*.yaml")):
        raw_text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
        if not isinstance(data, dict):
            continue

        meta = data.pop("template_metadata", {})
        template_id: str = meta.get("id") or path.stem
        display_name: str = meta.get("display_name", template_id)
        summary: str = meta.get("target_driver_summary", "")
        tools: list[str] = list(meta.get("prerequisite_host_tools") or [])
        source_pack_id: str = meta.get("source_pack_id", path.stem)

        result[template_id] = Template(
            descriptor=TemplateDescriptor(
                id=template_id,
                display_name=display_name,
                target_driver_summary=summary,
                source_pack_id=source_pack_id,
                prerequisite_host_tools=tools,
            ),
            raw_yaml=raw_text,
            manifest_dict=data,
        )
    return result


def _get_cache() -> dict[str, Template]:
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is None:
        _TEMPLATE_CACHE = _load_all_templates()
    return _TEMPLATE_CACHE


def list_templates() -> list[TemplateDescriptor]:
    return [template.descriptor for template in _get_cache().values()]


def load_template(template_id: str) -> Template:
    cache = _get_cache()
    if template_id not in cache:
        raise LookupError(f"template {template_id!r} not found; available: {sorted(cache)}")
    return cache[template_id]


def build_tarball_from_template(
    template: Template,
    *,
    pack_id: str,
    release: str,
    display_name: str | None = None,
) -> bytes:
    manifest_dict = copy.deepcopy(template.manifest_dict)
    manifest_dict["id"] = pack_id
    manifest_dict["release"] = release
    if display_name:
        manifest_dict["display_name"] = display_name
    manifest_dict["derived_from"] = {
        "pack_id": template.descriptor.source_pack_id,
        "release": release,
    }
    manifest_dict["template_id"] = template.descriptor.id

    manifest_bytes = yaml.safe_dump(manifest_dict, sort_keys=False).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("manifest.yaml")
        info.size = len(manifest_bytes)
        info.mtime = 0
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        tar.addfile(info, io.BytesIO(manifest_bytes))
    return buf.getvalue()
