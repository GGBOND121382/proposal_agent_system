from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable


DEFAULT_BATCHES: dict[str, list[str]] = {
    "STAGE-6A": [f"SEC-{i:02d}" for i in range(1, 6)],
    "STAGE-6B": [f"SEC-{i:02d}" for i in range(6, 9)],
    "STAGE-6C": [f"SEC-{i:02d}" for i in range(9, 12)],
    "STAGE-6D": [f"SEC-{i:02d}" for i in range(12, 15)],
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def project_title(*artifacts: dict[str, Any] | None, default: str = "未命名项目") -> str:
    """Resolve the project title from the newest authoritative staged artifact."""
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        value = str(artifact.get("project_title") or artifact.get("title") or "").strip()
        if value:
            return value
        definition = artifact.get("project_definition")
        if isinstance(definition, dict):
            value = str(definition.get("project_title") or definition.get("title") or "").strip()
            if value:
                return value
    return default


def batch_spec(stage5: dict[str, Any], batch_id: str, fallback: Iterable[str] | None = None) -> dict[str, Any]:
    batches = stage5.get("draft_batches") or []
    for batch in batches:
        if str(batch.get("batch_id")) == batch_id:
            section_ids = [str(item) for item in batch.get("section_ids") or []]
            if not section_ids:
                raise ValueError(f"{batch_id} does not contain section_ids")
            return {**batch, "section_ids": section_ids}
    fallback_ids = list(fallback or DEFAULT_BATCHES.get(batch_id, []))
    if fallback_ids:
        return {"batch_id": batch_id, "section_ids": fallback_ids}
    raise KeyError(f"Unknown staged draft batch: {batch_id}")


def section_ids_for_batch(stage5: dict[str, Any], batch_id: str, fallback: Iterable[str] | None = None) -> list[str]:
    return list(batch_spec(stage5, batch_id, fallback)["section_ids"])


def section_ids_from_run(run_dir: Path, batch_id: str, fallback: Iterable[str] | None = None) -> list[str]:
    metadata = run_dir / "RUN_METADATA.json"
    if metadata.exists():
        ids = [str(item) for item in read_json(metadata).get("section_ids") or []]
        if ids:
            return ids
    stage5_path = run_dir / "source_snapshots" / "stage5_section_plan.json"
    if stage5_path.exists():
        return section_ids_for_batch(read_json(stage5_path), batch_id, fallback)
    return list(fallback or DEFAULT_BATCHES.get(batch_id, []))


def all_section_ids(stage5: dict[str, Any]) -> list[str]:
    sections = stage5.get("sections") or []
    ordered = sorted(sections, key=lambda item: (int(item.get("order", 10**9)), str(item.get("section_id", ""))))
    result = [str(item["section_id"]) for item in ordered if item.get("section_id")]
    if not result:
        raise ValueError("stage5 section plan does not contain sections")
    return result


def section_contract(stage5: dict[str, Any], section_id: str) -> dict[str, Any]:
    for item in stage5.get("sections") or []:
        if str(item.get("section_id")) == section_id:
            return item
    raise KeyError(section_id)


def page_totals(stage5: dict[str, Any], section_ids: Iterable[str]) -> tuple[float, float]:
    target = 0.0
    maximum = 0.0
    for section_id in section_ids:
        contract = section_contract(stage5, section_id)
        target += float(contract.get("target_pages") or 0.0)
        maximum += float(contract.get("max_pages") or contract.get("target_pages") or 0.0)
    return round(target, 4), round(maximum, 4)


def stage_boundary(batch_id: str, section_ids: Iterable[str]) -> str:
    joined = "_".join(str(item).replace("-", "_") for item in section_ids)
    return f"{batch_id.replace('-', '_')}_{joined}_ONLY"


def safe_output_stem(title: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", title).strip(" ._")
    return value or "未命名项目"
