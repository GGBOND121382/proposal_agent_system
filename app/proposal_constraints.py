from __future__ import annotations

import copy
import json
import re
from typing import Any

_RANGE = r"(\d{1,4})\s*(?:—|–|-|~|～|至|到)\s*(\d{1,4})"


def _merge_range(current: dict[str, int] | None, low: int, high: int) -> dict[str, int]:
    if low > high:
        low, high = high, low
    if current is None:
        return {"min": low, "max": high}
    merged = {"min": max(int(current["min"]), low), "max": min(int(current["max"]), high)}
    if merged["min"] > merged["max"]:
        raise ValueError(f"Conflicting mandatory proposal constraints: {current} vs {(low, high)}")
    return merged


def _merge_min(current: int | None, value: int) -> int:
    return max(int(current or 0), int(value))


def _range_after(label: str, text: str, units: str) -> tuple[int, int] | None:
    patterns = (
        rf"{label}[^\d]{{0,12}}{_RANGE}\s*(?:{units})",
        rf"{_RANGE}\s*(?:{units})[^。；;，,]{{0,12}}{label}",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _minimum(label: str, text: str, units: str) -> int | None:
    patterns = (
        rf"(?:至少|不少于|不低于|minimum(?:\s+of)?)(?:包含|包括|设置|安排|配备|有)?\s*(\d+)\s*(?:{units})?[^。；;，,]{{0,12}}{label}",
        rf"{label}[^。；;，,]{{0,12}}(?:至少|不少于|不低于|minimum(?:\s+of)?)\s*(\d+)\s*(?:{units})?",
        rf"{label}\s*(?:数量)?\s*[≥>=]\s*(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return int(match.group(1))
    return None


def extract_hard_constraints(scheme_profile: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize mandatory quantitative guide rules into one deterministic contract.

    The parser is intentionally narrow: it only promotes explicit page/reference/
    figure/table quantities from mandatory scheme rules.  Unclear prose remains in
    the original rule list and is not guessed into a numeric acceptance threshold.
    """
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "source_rule_ids": [],
        "main_body_pages": None,
        "references": None,
        "minimum_figures": None,
        "minimum_tables": None,
        "references_excluded_from_main_body_pages": False,
    }
    for rule in (scheme_profile or {}).get("rules") or []:
        if not isinstance(rule, dict) or not bool(rule.get("mandatory")):
            continue
        text = str(rule.get("statement") or "").strip()
        if not text:
            continue
        touched = False
        page_range = _range_after(r"(?:正文|主文|main\s+body|body)", text, r"(?:页|pages?)")
        if page_range is None and str(rule.get("rule_type")) == "PAGE_OR_WORD_LIMIT":
            match = re.search(rf"{_RANGE}\s*(?:页|pages?)", text, re.I)
            page_range = (int(match.group(1)), int(match.group(2))) if match else None
        if page_range:
            result["main_body_pages"] = _merge_range(result["main_body_pages"], *page_range)
            touched = True

        references_excluded = bool(
            re.search(
                r"(?:参考文献|references?|bibliography)(?:页)?[^。；;]{0,20}(?:不计入|不计|另计页|另行计页|excluded|not\s+counted)",
                text,
                re.I,
            )
            or re.search(
                r"(?:不计入|不计|另计页|另行计页|excluded|not\s+counted)[^。；;]{0,20}(?:参考文献|references?|bibliography)",
                text,
                re.I,
            )
        )
        if references_excluded:
            result["references_excluded_from_main_body_pages"] = True
            touched = True

        ref_range = _range_after(r"(?:参考文献|references?|bibliography)", text, r"(?:篇|条|项|references?|entries)?")
        if ref_range:
            result["references"] = _merge_range(result["references"], *ref_range)
            touched = True

        # Compact Chinese forms such as “至少5图6表” are common in application guides.
        compact_fig = re.search(r"(?:至少|不少于|不低于)(?:包含|包括|设置|安排|配备|有)?[^。；;，,]*?(\d+)\s*(?:幅|张|个)?\s*(?:有效)?\s*图(?:形)?", text)
        compact_table = re.search(r"(?:至少|不少于|不低于)(?:包含|包括|设置|安排|配备|有)?[^。；;，,]*?(\d+)\s*(?:张|个)?\s*(?:有效)?\s*表(?:格)?", text)
        min_figures = int(compact_fig.group(1)) if compact_fig else _minimum(r"(?:图|图形|figures?)", text, r"(?:幅|张|个|figures?)")
        min_tables = int(compact_table.group(1)) if compact_table else _minimum(r"(?:表|表格|tables?)", text, r"(?:张|个|tables?)")
        if min_figures is not None:
            result["minimum_figures"] = _merge_min(result["minimum_figures"], min_figures)
            touched = True
        if min_tables is not None:
            result["minimum_tables"] = _merge_min(result["minimum_tables"], min_tables)
            touched = True
        if touched and rule.get("rule_id"):
            result["source_rule_ids"].append(str(rule["rule_id"]))
    result["source_rule_ids"] = list(dict.fromkeys(result["source_rule_ids"]))
    result["active"] = any(
        result.get(key) is not None
        for key in ("main_body_pages", "references", "minimum_figures", "minimum_tables")
    )
    return result


def merge_contract_constraints(
    proposal_contract: dict[str, Any] | None,
    hard_constraints: dict[str, Any],
) -> dict[str, Any] | None:
    if not proposal_contract:
        return proposal_contract
    merged = copy.deepcopy(proposal_contract)
    pages = hard_constraints.get("main_body_pages") or {}
    references = hard_constraints.get("references") or {}
    if pages:
        merged["min_main_pages"] = int(pages["min"])
        existing_max = merged.get("max_main_pages")
        merged["max_main_pages"] = min(int(existing_max), int(pages["max"])) if existing_max else int(pages["max"])
    if references:
        merged["min_reference_count"] = int(references["min"])
        merged["max_reference_count"] = int(references["max"])
    if hard_constraints.get("minimum_figures") is not None:
        merged["min_figure_count"] = int(hard_constraints["minimum_figures"])
    if hard_constraints.get("minimum_tables") is not None:
        merged["min_table_count"] = int(hard_constraints["minimum_tables"])
    merged["hard_constraint_rule_ids"] = list(hard_constraints.get("source_rule_ids") or [])
    return merged


def latest_scheme_constraints(db: Any, project_id: str) -> dict[str, Any]:
    row = db.fetchone(
        "SELECT output_json FROM prompt_runs WHERE project_id=? AND prompt_id='P-SCHEME-EXTRACT' "
        "AND status='PASS' AND output_json IS NOT NULL ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    )
    if not row:
        return extract_hard_constraints(None)
    try:
        output = json.loads(row["output_json"])
    except (TypeError, json.JSONDecodeError):
        return extract_hard_constraints(None)
    scheme = ((output.get("result") or {}).get("scheme_profile") if isinstance(output, dict) else None)
    return extract_hard_constraints(scheme)
