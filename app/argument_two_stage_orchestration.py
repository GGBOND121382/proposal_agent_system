from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from .model_semantic_contracts import (
    argument_design_model_output_errors,
    argument_design_model_reference_errors,
    argument_evidence_binding_specs,
    argument_foundation_eligible_evidence_ids,
    argument_skeleton_model_output_errors,
    assemble_argument_authored_state,
    build_argument_architecture_model_input,
    build_argument_design_model_input,
    build_argument_skeleton_model_input,
    expand_argument_architecture_model_output,
    semantic_model_reference_errors,
    split_argument_authored_state,
)


ARGUMENT_SKELETON_STAGE = "SKELETON"
ARGUMENT_DESIGN_STAGE = "DESIGN"
ARGUMENT_STAGE_REPAIR_SUFFIX = "_REPAIR"
ARGUMENT_TWO_STAGE_CONTRACT_VERSION = "ARGUMENT_TWO_STAGE_V16"

# Stage-local ceilings replace the legacy one-size-fits-all 131072-token demand
# for the internal two-stage Argument producer.  They are intentionally kept
# here, rather than in the global prompt profile, so no other Producer changes.
ARGUMENT_STAGE_DESIRED_OUTPUT_TOKENS = {
    ARGUMENT_SKELETON_STAGE: 8_192,
    ARGUMENT_DESIGN_STAGE: 65_536,
}

_ARGUMENT_DESIGN_THREAD_COLLECTIONS = (
    "work_packages",
    "methods",
    "theoretical_properties",
    "evaluations",
    "baselines",
    "ablations",
    "innovations",
    "innovation_prior_work",
    "innovation_evaluation_refs",
    "foundation",
    "foundation_supports",
)

_ARGUMENT_SKELETON_COMPONENTS = {
    "CENTRAL_PROPOSITION",
    "SCOPE",
    "GAP",
    "QUESTION",
    "OBJECTIVE",
    "RESEARCH_THREAD",
    "THREAD",
}

_QUESTION_TARGET_BY_GAP_KIND = {
    "FOUNDATION": "FOUNDATION_EVIDENCE",
    "CLOSEST_PRIOR_WORK": "PRIOR_WORK",
    "METRIC_JUSTIFICATION": "METRIC_JUSTIFICATION",
    "OTHER": "RESEARCH_DESIGN",
}


def argument_stage_desired_output_tokens(stage: str) -> int:
    try:
        return ARGUMENT_STAGE_DESIRED_OUTPUT_TOKENS[stage]
    except KeyError as exc:
        raise ValueError(f"Unknown Argument stage: {stage}") from exc


def argument_stage_prompt_text(stage: str) -> str:
    """Return the minimal semantic instruction for one internal Argument stage."""
    if stage == ARGUMENT_SKELETON_STAGE:
        return (
            "# 论证架构：问题骨架阶段\n\n"
            "只确定“研究什么”。依据输入中的项目任务、约束、证据、已有骨架提示、"
            "revision issues 和已确认回答，形成中心研究命题、研究范围以及 1–4 个研究线程。"
            "每个线程只表达差距及限制机制、研究问题、研究目标、必要假设、边界条件和"
            "比较/证伪规则。\n\n"
            "不要生成工作包、方法、理论性质、验证、基线、消融、创新点或团队基础；"
            "不要生成机器 ID、关系 ID、Hash 或运行时字段。证据引用只能使用输入已有的 "
            "`evidence_id`。信息不足时如实记录 evidence gap、用户问题或不能继续的原因。"
            "布尔用户问题只能确认一个可独立判断的命题，不得包含条件分支、多个“是否/能否”或多个问句；"
            "复合问题必须拆分，无法拆分时改为 `MISSING_INFORMATION` 并使用 `STRING` 回答。"
            "需要填写名称、清单、范围、分组或多项结果时也必须使用 `MISSING_INFORMATION + STRING`，"
            "不得用 `CONFIRMATION/CHOICE + OBJECT/ARRAY` 缩窄回答。"
            "只要本阶段存在 `blocking=true` 的用户问题，`cannot_proceed_reason` 必须输出 JSON 空值 "
            "`null`（无引号），不得输出字符串 `\"null\"`；可由用户回答解决的信息缺失用阻断性用户问题表达，"
            "只有无法通过这些问题解决、因而本阶段确实无法形成骨架时才使用非空 `cannot_proceed_reason`。"
            "若 `retry_context` 包含 `previous_candidate`，以上一轮候选为起点，只修正"
            "`validation_errors` 指出的当前阶段问题，其他已有效语义保持不变。"
        )
    if stage == ARGUMENT_DESIGN_STAGE:
        return (
            "# 论证架构：研究设计阶段\n\n"
            "只确定“怎么做、怎么验证、创新在哪里、已有基础支撑什么”。"
            "`frozen_skeleton` 是运行时从已通过 Stage-A 骨架投影出的只读语义视图：不得重写中心命题、研究范围或研究线程。"
            "其中 `research_threads[].thread_index` 仅用于引用冻结线程，`inherited_evidence` 按 gap、limitation_mechanism、objective 保留该线程已有证据绑定。"
            "`design_seed.existing_components[].seed_key` 是请求内语义句柄；关系提示若提供 `source_key/target_key`，"
            "应通过对应 seed component 理解其语义，不要把 seed_key 当作输出机器 ID。"
            "围绕冻结线程生成工作包、方法、必要理论性质、验证方案、代表性基线、必要消融、"
            "创新点及其最近工作/验证关联；只有存在合格证据时才声明团队基础。\n\n"
            "使用输出契约规定的局部整数索引建立这些语义记录之间的关联；"
            "不要生成机器 ID、关系 ID、Hash 或运行时字段。证据引用只能使用输入已有的 "
            "`evidence_id`。信息不足时允许相应记录为空，并如实记录 evidence gap、"
            "用户问题或不能继续的原因；不得重复 `frozen_skeleton` 中已有的问题或缺口。"
            "布尔用户问题只能确认一个可独立判断的命题，不得包含条件分支、多个“是否/能否”或多个问句；"
            "复合问题必须拆分，无法拆分时改为 `MISSING_INFORMATION` 并使用 `STRING` 回答。"
            "需要填写名称、清单、范围、分组或多项结果时也必须使用 `MISSING_INFORMATION + STRING`，"
            "不得用 `CONFIRMATION/CHOICE + OBJECT/ARRAY` 缩窄回答。"
            "`foundation_eligible_evidence_ids` 是团队基础唯一允许使用的证据集合；该集合为空时 "
            "`foundation` 与 `foundation_supports` 必须为空。只要冻结骨架或本阶段仍存在 "
            "`blocking=true` 的用户问题，`cannot_proceed_reason` 必须为 `null`，由用户问题表达待补信息。"
            "若 `retry_context` 包含 `previous_candidate`，以上一轮候选为起点，"
            "只修正 `validation_errors` 指出的当前阶段问题，"
            "其他已有效语义保持不变；任何情况下都不得改写 `frozen_skeleton`。"
        )
    raise ValueError(f"Unknown Argument stage: {stage}")


class ArgumentStageGateway(Protocol):
    async def invoke_stage(
        self,
        stage: str,
        model_input: dict[str, Any],
        output_schema: dict[str, Any],
        *,
        retry_context: dict[str, Any] | None = None,
        desired_output_tokens: int,
    ) -> dict[str, Any]: ...


class ArgumentStageContractError(ValueError):
    """Stage-local validation failure with the rejected candidate preserved for retry."""

    def __init__(
        self,
        stage: str,
        phase: str,
        errors: list[str],
        *,
        candidate: Any = None,
    ) -> None:
        self.stage = stage
        self.phase = phase
        self.errors = list(errors)
        self.candidate = copy.deepcopy(candidate) if isinstance(candidate, dict) else candidate
        detail = "; ".join(self.errors[:6]) or "unknown validation failure"
        super().__init__(f"{stage} {phase} failed: {detail}")


def argument_stage_output_schema(stage: str) -> dict[str, Any]:
    names = {
        ARGUMENT_SKELETON_STAGE: "argument_skeleton_model_output.schema.json",
        ARGUMENT_DESIGN_STAGE: "argument_design_model_output.schema.json",
    }
    try:
        filename = names[stage]
    except KeyError as exc:
        raise ValueError(f"Unknown Argument stage: {stage}") from exc
    path = Path(__file__).resolve().parents[1] / "prompt_pack" / "schemas" / "model" / filename
    return json.loads(path.read_text(encoding="utf-8"))


def argument_stage_repair_output_schema() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[1]
        / "prompt_pack"
        / "schemas"
        / "model"
        / "targeted_repair_model_output.schema.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _known_evidence_ids(model_input: dict[str, Any]) -> set[str]:
    return {
        str(card.get("evidence_id"))
        for card in model_input.get("evidence_cards") or []
        if isinstance(card, dict) and card.get("evidence_id")
    }


def _binding_pattern_tokens(spec: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        token for token in str(spec.get("wire_path") or "").strip("/").split("/")
        if token
    )


def _iter_binding_lists(
    node: Any,
    pattern: tuple[str, ...],
    path: tuple[str, ...] = (),
):
    if not pattern:
        if isinstance(node, list):
            yield path, node
        return
    token, *rest = pattern
    tail = tuple(rest)
    if token == "*":
        if not isinstance(node, list):
            return
        for index, item in enumerate(node):
            yield from _iter_binding_lists(item, tail, (*path, str(index)))
        return
    if isinstance(node, dict) and token in node:
        yield from _iter_binding_lists(node[token], tail, (*path, token))


def _registered_evidence_parent_paths(stage: str, candidate: dict[str, Any]) -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    for spec in argument_evidence_binding_specs(stage):
        pattern = _binding_pattern_tokens(spec)
        paths.update(path for path, _values in _iter_binding_lists(candidate, pattern))
    return paths


def _stage_evidence_reference_errors(
    stage: str, model_input: dict[str, Any], candidate: dict[str, Any]
) -> list[str]:
    known = _known_evidence_ids(model_input)
    foundation_eligible = {
        str(value)
        for value in model_input.get("foundation_eligible_evidence_ids") or []
        if str(value).strip()
    }
    errors: list[str] = []
    for spec in argument_evidence_binding_specs(stage):
        pattern = _binding_pattern_tokens(spec)
        domain = str(spec.get("reference_domain") or "EVIDENCE_CARDS")
        for parent_path, values in _iter_binding_lists(candidate, pattern):
            for index, evidence_id in enumerate(values):
                evidence_text = str(evidence_id)
                pointer = "/" + "/".join((*parent_path, str(index)))
                if evidence_text not in known:
                    errors.append(
                        pointer
                        + f": evidence_id {evidence_id!r} is not present in evidence_cards"
                    )
                    continue
                if domain == "FOUNDATION_ELIGIBLE" and evidence_text not in foundation_eligible:
                    errors.append(
                        pointer
                        + f": evidence_id {evidence_id!r} is not present in foundation_eligible_evidence_ids"
                    )
    return errors


def _skeleton_reference_errors(
    model_input: dict[str, Any], candidate: dict[str, Any]
) -> list[str]:
    errors = _stage_evidence_reference_errors(ARGUMENT_SKELETON_STAGE, model_input, candidate)
    threads = candidate.get("research_threads")
    thread_count = len(threads) if isinstance(threads, list) else None
    gaps = candidate.get("evidence_gaps")
    for index, gap in enumerate(gaps if isinstance(gaps, list) else []):
        if not isinstance(gap, dict):
            continue
        thread_index = gap.get("thread_index")
        if (
            thread_count is not None
            and isinstance(thread_index, int)
            and not isinstance(thread_index, bool)
            and not (0 <= thread_index < thread_count)
        ):
            errors.append(f"/evidence_gaps/{index}/thread_index: out of range")
    return errors


def _design_reference_errors(
    model_input: dict[str, Any],
    candidate: dict[str, Any],
    frozen_skeleton: dict[str, Any],
) -> list[str]:
    errors = [
        *argument_design_model_reference_errors(candidate, frozen_skeleton),
        *_stage_evidence_reference_errors(ARGUMENT_DESIGN_STAGE, model_input, candidate),
    ]
    eligible = {
        str(value)
        for value in model_input.get("foundation_eligible_evidence_ids") or []
        if str(value).strip()
    }
    unsupported_foundation: list[int] = []
    for index, item in enumerate(candidate.get("foundation") or []):
        if not isinstance(item, dict):
            continue
        evidence_ids = {str(value) for value in item.get("evidence_ids") or []}
        if not (evidence_ids & eligible):
            unsupported_foundation.append(index)
    if unsupported_foundation:
        errors.append(
            "/foundation: records at indexes "
            + str(unsupported_foundation)
            + " lack any foundation_eligible_evidence_ids; remove unsupported foundation "
            "records (and their foundation_supports) or replace them with qualified evidence; "
            "when the eligible set is empty, foundation and foundation_supports must both be empty"
        )
    return errors


_REQUIRED_PROPERTY_ERROR = re.compile(
    r"^'(?P<property>[^']+)' is a required property$"
)
_ADDITIONAL_PROPERTY_ERROR = re.compile(
    r"^Additional propert(?:y|ies) (?:is|are) not allowed \((?P<detail>.+)\)$"
)
_QUOTED_PROPERTY = re.compile(r"'([^']+)'")


def _decode_error_pointer(raw_pointer: str) -> tuple[str, ...] | None:
    """Decode one validator JSON pointer without accepting ambiguous text."""

    if raw_pointer == "/":
        return ()
    if not raw_pointer.startswith("/"):
        return None
    return tuple(
        token.replace("~1", "/").replace("~0", "~")
        for token in raw_pointer[1:].split("/")
    )


def argument_stage_repair_scope_paths(errors: list[str]) -> tuple[tuple[str, ...], ...]:
    """Return deterministic mutation scopes without trusting invalid rows.

    A structurally invalid object in a root collection is not an immutable row
    with a few mutable leaves. Its identity and every sibling value may be a
    transport fragment, so the whole row must be replaceable or deletable. A
    scalar/reference failure below an otherwise addressable row remains scoped
    to the reported value. Ambiguous root failures still fail closed.
    """

    paths: set[tuple[str, ...]] = set()
    for raw_error in errors:
        pointer_text, separator, message = str(raw_error).partition(": ")
        if not separator:
            continue
        base = _decode_error_pointer(pointer_text)
        if base is None:
            continue

        required = _REQUIRED_PROPERTY_ERROR.fullmatch(message)
        if required:
            if len(base) == 2 and base[1].isdigit():
                paths.add(base)
            else:
                paths.add((*base, required.group("property")))
            continue

        additional = _ADDITIONAL_PROPERTY_ERROR.fullmatch(message)
        if additional:
            if len(base) == 2 and base[1].isdigit():
                paths.add(base)
            else:
                for property_name in _QUOTED_PROPERTY.findall(
                    additional.group("detail")
                ):
                    paths.add((*base, property_name))
            continue

        if base:
            paths.add(base)

    # An authorized ancestor already covers every descendant and is the only
    # scope that should be retained in the deterministic merge.
    minimal: list[tuple[str, ...]] = []
    for path in sorted(paths, key=lambda item: (len(item), item)):
        if any(path[: len(parent)] == parent for parent in minimal):
            continue
        minimal.append(path)
    return tuple(minimal)


_UNKNOWN_EVIDENCE_ERROR_PREFIX = "evidence_id "
_UNKNOWN_EVIDENCE_ERROR_SUFFIX = " is not present in evidence_cards"
_FOUNDATION_EVIDENCE_DOMAIN_ERROR_SUFFIX = " is not present in foundation_eligible_evidence_ids"


def _value_at_pointer(root: Any, path: tuple[str, ...]) -> Any:
    value = root
    for token in path:
        if isinstance(value, dict) and token in value:
            value = value[token]
            continue
        if isinstance(value, list):
            try:
                index = int(token)
            except ValueError:
                return _MISSING
            if 0 <= index < len(value):
                value = value[index]
                continue
        return _MISSING
    return value


def _drop_reported_unknown_evidence_ids(
    candidate: Any,
    errors: list[str],
    *,
    stage: str | None = None,
) -> tuple[Any, list[str]]:
    """Delete only exact evidence values named by reference validation.

    The validator reports the old JSON pointer and ``repr`` of each unavailable
    ID.  Both must still match the immutable candidate before any deletion is
    applied. Grouped indexes are removed in descending order so earlier paths
    cannot drift. Any malformed, stale, or non-evidence error remains unresolved.
    """

    repaired = copy.deepcopy(candidate)
    if not isinstance(repaired, dict):
        return repaired, []

    allowed_parent_paths = (
        _registered_evidence_parent_paths(stage, repaired)
        if stage is not None
        else None
    )
    planned: dict[tuple[str, ...], set[int]] = {}
    resolved_indexes: set[int] = set()
    for error_index, raw_error in enumerate(errors):
        pointer_text, separator, message = str(raw_error).partition(": ")
        if not separator:
            continue
        path = _decode_error_pointer(pointer_text)
        if (
            path is None
            or len(path) < 2
            or not message.startswith(_UNKNOWN_EVIDENCE_ERROR_PREFIX)
            or not message.endswith((
                _UNKNOWN_EVIDENCE_ERROR_SUFFIX,
                _FOUNDATION_EVIDENCE_DOMAIN_ERROR_SUFFIX,
            ))
        ):
            continue
        try:
            item_index = int(path[-1])
        except ValueError:
            continue
        parent_path = path[:-1]
        if stage is None and not path[-2].endswith("evidence_ids"):
            continue
        values = _value_at_pointer(repaired, parent_path)
        if not isinstance(values, list) or not (0 <= item_index < len(values)):
            continue
        if allowed_parent_paths is not None and parent_path not in allowed_parent_paths:
            continue
        suffix = (
            _FOUNDATION_EVIDENCE_DOMAIN_ERROR_SUFFIX
            if message.endswith(_FOUNDATION_EVIDENCE_DOMAIN_ERROR_SUFFIX)
            else _UNKNOWN_EVIDENCE_ERROR_SUFFIX
        )
        expected_repr = message[
            len(_UNKNOWN_EVIDENCE_ERROR_PREFIX) : -len(suffix)
        ]
        if repr(values[item_index]) != expected_repr:
            continue
        planned.setdefault(parent_path, set()).add(item_index)
        resolved_indexes.add(error_index)

    for parent_path, indexes in planned.items():
        values = _value_at_pointer(repaired, parent_path)
        if not isinstance(values, list):
            continue
        for item_index in sorted(indexes, reverse=True):
            values.pop(item_index)
    return repaired, [
        error for index, error in enumerate(errors) if index in resolved_indexes
    ]


_MISSING = object()


def _has_authorized_descendant(
    path: tuple[str, ...],
    scopes: tuple[tuple[str, ...], ...],
) -> bool:
    return any(scope[: len(path)] == path for scope in scopes)


def _is_authorized_path(
    path: tuple[str, ...],
    scopes: tuple[tuple[str, ...], ...],
) -> bool:
    return path in scopes


def _repair_invariant_projection(
    value: Any,
    path: tuple[str, ...],
    scopes: tuple[tuple[str, ...], ...],
) -> Any:
    """Mask authorized descendants so list-row identity cannot be swapped."""

    if _is_authorized_path(path, scopes):
        return _MISSING
    if not _has_authorized_descendant(path, scopes):
        return copy.deepcopy(value)
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, child in value.items():
            child_path = (*path, str(key))
            child_projection = _repair_invariant_projection(
                child, child_path, scopes
            )
            if child_projection is not _MISSING:
                projected[str(key)] = child_projection
        return projected
    if isinstance(value, list):
        projected_items: list[Any] = []
        for index, item in enumerate(value):
            item_projection = _repair_invariant_projection(
                item, (*path, str(index)), scopes
            )
            # An exactly scoped list element is excluded from the invariant,
            # just like an exactly scoped mapping property. Keeping the
            # sentinel in the list would make a valid deletion look like a row
            # identity change and cause the whole repaired row to be rejected.
            if item_projection is not _MISSING:
                projected_items.append(item_projection)
        return projected_items
    return copy.deepcopy(value)


def _repair_list_item_is_deletable(
    value: Any,
    path: tuple[str, ...],
    scopes: tuple[tuple[str, ...], ...],
) -> bool:
    if _is_authorized_path(path, scopes):
        return True
    if not _has_authorized_descendant(path, scopes):
        return False
    invariant = _repair_invariant_projection(value, path, scopes)
    return invariant in ({}, [])


def _repair_invariant_matches(
    previous: Any,
    repaired: Any,
    *,
    path: tuple[str, ...],
    scopes: tuple[tuple[str, ...], ...],
) -> bool:
    """Return whether every unscoped part retains value, order, and identity."""

    if _is_authorized_path(path, scopes):
        return True
    if not _has_authorized_descendant(path, scopes):
        return previous == repaired
    if isinstance(previous, dict) and isinstance(repaired, dict):
        for key in dict.fromkeys([*previous.keys(), *repaired.keys()]):
            if not _repair_invariant_matches(
                previous.get(key, _MISSING),
                repaired.get(key, _MISSING),
                path=(*path, str(key)),
                scopes=scopes,
            ):
                return False
        return True
    if isinstance(previous, list) and isinstance(repaired, list):
        return (
            _align_argument_stage_repair_list(
                previous,
                repaired,
                path=path,
                scopes=scopes,
                allow_trailing_appends=False,
            )
            is not None
        )
    return previous == repaired


def _invalid_row_replacement_index(
    previous: Any,
    repaired: list[Any],
    *,
    start: int,
) -> int | None:
    """Select one unambiguous replacement using only surviving Draft hints.

    Values in a structurally invalid row are not authoritative, but exact
    overlaps are still useful for locating its repair among a full provider
    collection.  A tie is deliberately treated as ambiguous: the invalid row
    may be deleted, but an arbitrary unrelated row must never be adopted.
    """

    if not isinstance(previous, dict):
        return None
    scored: list[tuple[int, int]] = []
    for index in range(start, len(repaired)):
        candidate = repaired[index]
        if not isinstance(candidate, dict):
            continue
        score = sum(
            1
            for key, value in previous.items()
            if key in candidate and candidate[key] == value
        )
        if score:
            scored.append((score, index))
    if not scored:
        return start if start < len(repaired) and len(repaired) - start == 1 else None
    best = max(score for score, _index in scored)
    indexes = [index for score, index in scored if score == best]
    return indexes[0] if len(indexes) == 1 else None


def _align_argument_stage_repair_list(
    previous: list[Any],
    repaired: list[Any],
    *,
    path: tuple[str, ...],
    scopes: tuple[tuple[str, ...], ...],
    allow_trailing_appends: bool,
) -> tuple[int | None, ...] | None:
    """Align repaired positions to old positions without positional drift.

    Each old unscoped item must consume one identical repaired item in order.
    An exactly scoped item, or a row whose entire content is scoped, may consume
    one replacement or be deleted.  This supports authorized edits at the
    beginning, middle, or end while rejecting reorder, prefix insertion, and
    mutation of any unreported value. Unreported object rows may be skipped at
    any position so a model's expanded collection cannot shift old row paths;
    scalar prefix/middle insertion remains forbidden. A caller may also ignore
    trailing appended values; ignored values never enter the merged result.
    """

    memo: dict[tuple[int, int], tuple[int | None, ...] | None] = {}
    object_row_sequence = bool(previous) and all(
        isinstance(item, dict) for item in previous
    ) and all(isinstance(item, dict) for item in repaired)

    def solve(old_index: int, new_index: int) -> tuple[int | None, ...] | None:
        key = (old_index, new_index)
        if key in memo:
            return memo[key]
        if old_index == len(previous):
            result: tuple[int | None, ...] | None = (
                ()
                if allow_trailing_appends or new_index == len(repaired)
                else None
            )
            memo[key] = result
            return result

        child_path = (*path, str(old_index))
        whole_row_repair = _is_authorized_path(child_path, scopes)
        replacement_index = (
            _invalid_row_replacement_index(
                previous[old_index], repaired, start=new_index
            )
            if whole_row_repair and object_row_sequence
            else new_index
        )
        if (
            replacement_index is not None
            and replacement_index < len(repaired)
            and _repair_invariant_matches(
                previous[old_index],
                repaired[replacement_index],
                path=child_path,
                scopes=scopes,
            )
        ):
            tail = solve(old_index + 1, replacement_index + 1)
            if tail is not None:
                result = (replacement_index, *tail)
                memo[key] = result
                return result

        if _repair_list_item_is_deletable(
            previous[old_index], child_path, scopes
        ):
            tail = solve(old_index + 1, new_index)
            if tail is not None:
                result = (None, *tail)
                memo[key] = result
                return result

        if object_row_sequence and new_index < len(repaired):
            tail = solve(old_index, new_index + 1)
            if tail is not None:
                memo[key] = tail
                return tail

        memo[key] = None
        return None

    return solve(0, 0)


def _merge_argument_stage_repair_value(
    previous: Any,
    repaired: Any,
    *,
    path: tuple[str, ...],
    scopes: tuple[tuple[str, ...], ...],
) -> Any:
    if _is_authorized_path(path, scopes):
        return copy.deepcopy(repaired) if repaired is not _MISSING else _MISSING
    if not _has_authorized_descendant(path, scopes):
        return copy.deepcopy(previous) if previous is not _MISSING else _MISSING

    if isinstance(previous, dict) and isinstance(repaired, dict):
        merged: dict[str, Any] = {}
        for key in dict.fromkeys([*previous.keys(), *repaired.keys()]):
            child = _merge_argument_stage_repair_value(
                previous.get(key, _MISSING),
                repaired.get(key, _MISSING),
                path=(*path, str(key)),
                scopes=scopes,
            )
            if child is not _MISSING:
                merged[key] = child
        return merged

    if isinstance(previous, list) and isinstance(repaired, list):
        alignment = _align_argument_stage_repair_list(
            previous,
            repaired,
            path=path,
            scopes=scopes,
            allow_trailing_appends=True,
        )
        if alignment is None:
            return copy.deepcopy(previous)
        merged_items: list[Any] = []
        for old_index, new_index in enumerate(alignment):
            if new_index is None:
                continue
            merged_items.append(
                _merge_argument_stage_repair_value(
                    previous[old_index],
                    repaired[new_index],
                    path=(*path, str(old_index)),
                    scopes=scopes,
                )
            )
        return merged_items

    # A container/type rewrite was not explicitly authorized. Preserve the
    # prior candidate and let validation report the unresolved scoped failure.
    return copy.deepcopy(previous) if previous is not _MISSING else _MISSING


def merge_argument_stage_repair_candidate(
    previous_candidate: Any,
    repaired_candidate: Any,
    validation_errors: list[str],
    *,
    deterministic_defaults: tuple[tuple[tuple[str, ...], Any], ...] = (),
) -> Any:
    """Accept only reported repairs and exact deterministic-default equivalents.

    A default path is merge-neutral only when the new provider candidate omits
    it or supplies the exact unique value recorded by normalization. This keeps
    runtime-created defaults out of row identity without authorizing arbitrary
    changes at those paths.
    """

    if not isinstance(previous_candidate, dict) or not isinstance(
        repaired_candidate, dict
    ):
        return copy.deepcopy(previous_candidate)
    scope_set = set(argument_stage_repair_scope_paths(validation_errors))
    for path, expected_value in deterministic_defaults:
        repaired_value = _value_at_pointer(repaired_candidate, path)
        if repaired_value is _MISSING or repaired_value == expected_value:
            scope_set.add(path)
    scopes_list: list[tuple[str, ...]] = []
    for path in sorted(scope_set, key=lambda item: (len(item), item)):
        if any(path[: len(parent)] == parent for parent in scopes_list):
            continue
        scopes_list.append(path)
    scopes = tuple(scopes_list)
    merged = _merge_argument_stage_repair_value(
        previous_candidate,
        repaired_candidate,
        path=(),
        scopes=scopes,
    )
    return copy.deepcopy(previous_candidate) if merged is _MISSING else merged


def _normalized_semantic_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    for token in " \t\r\n，。；：！？,:;!?“”\"'（）()[]{}":
        text = text.replace(token, "")
    return text


def _question_key(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return _normalized_semantic_text(item.get("question"))


def _gap_key(item: Any) -> tuple[str, str, str] | None:
    if not isinstance(item, dict):
        return None
    semantic = _normalized_semantic_text(
        item.get("suggested_question") or item.get("reason")
    )
    if not semantic:
        return None
    return (
        str(item.get("kind") or ""),
        str(item.get("thread_index")),
        semantic,
    )


def _primitive_allowed_values(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    result: list[Any] = []
    for item in value:
        if not isinstance(item, (str, int, float, bool)) or item in result:
            continue
        result.append(copy.deepcopy(item))
    return result


def _project_stage_user_questions(stage: str, candidate: Any) -> Any:
    """Make Gate metadata a deterministic projection of semantic questions.

    The model owns question/reason text and any explicit alternatives. Runtime
    owns routing, control type, blocking and priority. In particular, malformed
    metadata fragments can never force another model call or become immutable
    Draft identity.
    """

    normalized = copy.deepcopy(candidate)
    if not isinstance(normalized, dict):
        return normalized

    gaps = [
        item
        for item in normalized.get("evidence_gaps") or []
        if isinstance(item, dict)
    ]
    gaps_by_question = {
        _normalized_semantic_text(item.get("suggested_question")): item
        for item in gaps
        if _normalized_semantic_text(item.get("suggested_question"))
    }
    default_target = (
        "RESEARCH_DESIGN"
        if stage == ARGUMENT_DESIGN_STAGE
        else "PROJECT_SCOPE"
    )

    projected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def append_question(
        *,
        question: Any,
        reason: Any,
        allowed_values: Any,
        gap: dict[str, Any] | None,
        authored_blocking: Any = False,
    ) -> None:
        question_text = str(question or "").strip()
        reason_text = str(reason or "").strip()
        key = _normalized_semantic_text(question_text)
        if not key or not reason_text or key in seen:
            return
        seen.add(key)
        choices = _primitive_allowed_values(allowed_values)
        boolean_choices = (
            len(choices) == 2
            and all(isinstance(item, bool) for item in choices)
            and set(choices) == {True, False}
        )
        if boolean_choices:
            question_type = "CONFIRMATION"
            answer_shape = "BOOLEAN"
            choices = []
        elif choices:
            question_type = "CHOICE"
            answer_shape = "STRING"
        else:
            # Free text is the safe, non-narrowing representation for missing
            # information and conflict explanations. No text classifier is
            # allowed to guess a Boolean/enum contract.
            question_type = "MISSING_INFORMATION"
            answer_shape = "STRING"
        # Gap ownership wins when present. An unlinked question's first-stage
        # blocking intent is retained in the Draft, then priority and all Gate
        # representation fields are projected deterministically from it.
        blocking = (
            bool(gap.get("blocking"))
            if gap is not None
            else bool(authored_blocking)
        )
        target_area = (
            _QUESTION_TARGET_BY_GAP_KIND.get(str(gap.get("kind") or ""), default_target)
            if gap is not None
            else default_target
        )
        projected.append(
            {
                "target_area": target_area,
                "question_type": question_type,
                "question": question_text,
                "reason": reason_text,
                "answer_shape": answer_shape,
                "allowed_values": choices,
                "blocking": blocking,
                "priority": "P0" if blocking else "P2",
            }
        )

    for item in normalized.get("user_questions") or []:
        if not isinstance(item, dict):
            continue
        question_key = _normalized_semantic_text(item.get("question"))
        append_question(
            question=item.get("question"),
            reason=item.get("reason"),
            allowed_values=item.get("allowed_values"),
            gap=gaps_by_question.get(question_key),
            authored_blocking=item.get("blocking"),
        )

    # A semantic gap is the single source of truth for a missing-input Gate.
    # If the model supplied the gap but omitted the duplicate question object,
    # runtime projects it without another provider call.
    for gap in gaps:
        append_question(
            question=gap.get("suggested_question"),
            reason=gap.get("reason"),
            allowed_values=[],
            gap=gap,
            authored_blocking=gap.get("blocking"),
        )

    normalized["user_questions"] = projected
    if any(item["blocking"] for item in projected):
        normalized["cannot_proceed_reason"] = None
    elif "cannot_proceed_reason" not in normalized:
        normalized["cannot_proceed_reason"] = None
    return normalized


def _salvage_root_question_fragment(candidate: Any) -> Any:
    """Lift one exact root-level MiniMax question fragment before stripping it."""

    normalized = copy.deepcopy(candidate)
    if not isinstance(normalized, dict):
        return normalized
    question = normalized.get("question")
    reason = normalized.get("reason")
    if not isinstance(question, str) or not question.strip():
        return normalized
    if not isinstance(reason, str) or not reason.strip():
        return normalized
    questions = normalized.get("user_questions")
    if not isinstance(questions, list):
        questions = []
        normalized["user_questions"] = questions
    questions.append(
        {
            "question": question,
            "reason": reason,
            "allowed_values": copy.deepcopy(normalized.get("allowed_values") or []),
        }
    )
    return normalized


def _normalize_stage_root_and_optional_rows(stage: str, candidate: Any) -> Any:
    """Apply schema-derived defaults and drop only invalid optional rows."""

    normalized = _salvage_root_question_fragment(candidate)
    if not isinstance(normalized, dict):
        return normalized
    schema = argument_stage_output_schema(stage)
    properties = schema.get("properties") or {}
    normalized = {
        key: copy.deepcopy(value)
        for key, value in normalized.items()
        if key in properties
    }
    normalized = _project_stage_user_questions(stage, normalized)

    default_collections = {"evidence_gaps", "user_questions"}
    optional_collections = set(default_collections)
    for collection in optional_collections:
        collection_schema = properties.get(collection) or {}
        if collection not in normalized:
            if collection in default_collections:
                normalized[collection] = []
            continue
        rows = normalized.get(collection)
        item_schema = collection_schema.get("items")
        if not isinstance(rows, list) or not isinstance(item_schema, dict):
            normalized[collection] = []
            continue
        validator = Draft202012Validator(item_schema)
        normalized[collection] = [
            copy.deepcopy(item)
            for item in rows
            if not any(validator.iter_errors(item))
        ]

    if stage == ARGUMENT_DESIGN_STAGE:
        for collection, collection_schema in properties.items():
            if collection in optional_collections or not isinstance(collection_schema, dict):
                continue
            rows = normalized.get(collection)
            item_schema = collection_schema.get("items")
            if not isinstance(rows, list) or not isinstance(item_schema, dict):
                continue
            validator = Draft202012Validator(item_schema)
            kept: list[Any] = []
            for item in rows:
                errors = list(validator.iter_errors(item))
                if not errors:
                    kept.append(copy.deepcopy(item))
                    continue
                # A row containing only local indexes carries no authored
                # semantic content. It is a displaced relation shell, not a
                # Draft entity that warrants another model call.
                if isinstance(item, dict) and item and all(
                    key == "thread_index" or key.endswith("_index")
                    for key in item
                ):
                    continue
                kept.append(copy.deepcopy(item))
            normalized[collection] = kept
    return normalized


def _normalize_stage_mechanical_defaults_with_provenance(
    candidate: Any,
) -> tuple[Any, tuple[tuple[tuple[str, ...], Any], ...]]:
    """Apply only unambiguous stage-boundary defaults on a deep copy.

    Provider evidence remains immutable; validation and deterministic assembly
    consume a deep copy of the candidate.  Other null-looking strings remain
    authored values. Missing fields are filled only when the surrounding
    authored values determine the sole contract-valid value. The returned
    provenance records each normalized path and its unique value so scoped
    retry merging can treat only that exact value (or omission) as equivalent.
    """
    normalized = copy.deepcopy(candidate)
    defaults: dict[tuple[str, ...], Any] = {}
    if not isinstance(normalized, dict):
        return normalized, ()
    if (
        isinstance(normalized.get("cannot_proceed_reason"), str)
        and normalized["cannot_proceed_reason"] == "null"
    ):
        normalized["cannot_proceed_reason"] = None
        defaults[("cannot_proceed_reason",)] = None

    questions = normalized.get("user_questions")
    if isinstance(questions, list):
        blocking_question_exists = any(
            isinstance(item, dict) and item.get("blocking") is True
            for item in questions
        )
        if blocking_question_exists and "cannot_proceed_reason" not in normalized:
            normalized["cannot_proceed_reason"] = None
            defaults[("cannot_proceed_reason",)] = None
        for index, item in enumerate(questions):
            if not isinstance(item, dict) or "allowed_values" in item:
                continue
            # A missing/garbled question_type does not determine a default.
            # In particular, MiniMax may emit standalone ``$text``/empty
            # objects when one question row is fragmented. Enriching those
            # artifacts would turn a runtime-created value into apparent
            # authored content and can prevent a later list repair.
            if item.get("question_type") in {
                "CONFIRMATION",
                "MISSING_INFORMATION",
                "CONFLICT_RESOLUTION",
            }:
                item["allowed_values"] = []
                defaults[("user_questions", str(index), "allowed_values")] = []
    return normalized, tuple(
        (path, copy.deepcopy(value)) for path, value in defaults.items()
    )


def _normalize_stage_mechanical_defaults(candidate: Any) -> Any:
    normalized, _defaults = _normalize_stage_mechanical_defaults_with_provenance(
        candidate
    )
    return normalized


_SKELETON_THREAD_ANCHOR_FIELDS = {
    "gap_statement",
    "gap_evidence_ids",
    "limitation_mechanism_statement",
    "limitation_mechanism_evidence_ids",
}
_SKELETON_THREAD_COMPLETION_FIELDS = {
    "question_statement",
    "question_type",
    "answerability",
    "success_evidence",
    "objective_statement",
    "objective_evidence_ids",
    "assumptions",
    "falsification_or_comparison_rule",
}
_SKELETON_THREAD_COMPLETION_LIST_FIELDS = {
    "success_evidence",
    "objective_evidence_ids",
    "assumptions",
}

_SKELETON_FRAGMENT_ANCHOR_FIELDS = {
    "gap_statement",
    "gap_evidence_ids",
    "limitation_mechanism_statement",
}


def _fragment_text(value: Any) -> str | None:
    if not isinstance(value, dict) or set(value) != {"$text"}:
        return None
    text = value.get("$text")
    return text if isinstance(text, str) and text.strip() else None


def _fragment_items(value: Any) -> list[str] | None:
    if not isinstance(value, dict) or set(value) != {"item"}:
        return None
    items = value.get("item")
    if isinstance(items, str):
        items = [items]
    return _nonempty_string_list(items)


def _compact_one_skeleton_thread_fragment(chunk: list[Any]) -> dict[str, Any] | None:
    if not chunk or not isinstance(chunk[0], dict):
        return None
    anchor = chunk[0]
    if set(anchor) != _SKELETON_FRAGMENT_ANCHOR_FIELDS:
        return None

    question_type_index = next(
        (
            index
            for index in range(2, len(chunk) - 1)
            if _fragment_text(chunk[index]) in {"SCIENTIFIC", "TECHNICAL", "ENGINEERING"}
            and _fragment_text(chunk[index + 1])
            in {"TESTABLE", "COMPARABLE", "DESIGN_VERIFIABLE", "UNCLEAR"}
            and _fragment_text(chunk[index - 1]) is not None
        ),
        None,
    )
    if question_type_index is None:
        return None
    evidence_fragments = chunk[1 : question_type_index - 1]
    evidence_ids = [_fragment_text(item) for item in evidence_fragments]
    if not evidence_ids or any(item is None for item in evidence_ids):
        return None

    question_statement = _fragment_text(chunk[question_type_index - 1])
    question_type = _fragment_text(chunk[question_type_index])
    answerability = _fragment_text(chunk[question_type_index + 1])
    tail = chunk[question_type_index + 2 :]
    if len(tail) not in {4, 5}:
        return None
    success_evidence = _fragment_items(tail[0])
    objective_statement = _fragment_text(tail[1])
    objective_evidence_ids = _fragment_items(tail[2])
    assumptions: list[str] | None = None
    falsification_rule: str | None = None
    if len(tail) == 5:
        assumptions = _fragment_items(tail[3])
        falsification_rule = _fragment_text(tail[4])
    elif (
        isinstance(tail[3], dict)
        and set(tail[3]) == {"assumptions", "falsification_or_comparison_rule"}
    ):
        assumptions = _nonempty_string_list(tail[3].get("assumptions"))
        raw_rule = tail[3].get("falsification_or_comparison_rule")
        falsification_rule = (
            raw_rule if isinstance(raw_rule, str) and raw_rule.strip() else None
        )
    if any(
        value is None
        for value in (
            question_statement,
            question_type,
            answerability,
            success_evidence,
            objective_statement,
            objective_evidence_ids,
            assumptions,
            falsification_rule,
        )
    ):
        return None
    return {
        **copy.deepcopy(anchor),
        "limitation_mechanism_evidence_ids": evidence_ids,
        "question_statement": question_statement,
        "question_type": question_type,
        "answerability": answerability,
        "success_evidence": success_evidence,
        "objective_statement": objective_statement,
        "objective_evidence_ids": objective_evidence_ids,
        "assumptions": assumptions,
        "falsification_or_comparison_rule": falsification_rule,
    }


def _compact_skeleton_thread_collection(rows: Any) -> Any:
    """Compact exact MiniMax row fragmentation without guessing semantics."""

    if not isinstance(rows, list):
        return copy.deepcopy(rows)
    result: list[Any] = []
    index = 0
    while index < len(rows):
        item = rows[index]
        if not (
            isinstance(item, dict)
            and set(item) == _SKELETON_FRAGMENT_ANCHOR_FIELDS
        ):
            result.append(copy.deepcopy(item))
            index += 1
            continue
        end = index + 1
        while end < len(rows):
            possible_anchor = rows[end]
            if isinstance(possible_anchor, dict) and (
                set(possible_anchor) == _SKELETON_FRAGMENT_ANCHOR_FIELDS
                or set(possible_anchor) == _SKELETON_THREAD_ANCHOR_FIELDS
                or set(possible_anchor) >= _SKELETON_THREAD_ANCHOR_FIELDS
            ):
                break
            end += 1
        compacted = _compact_one_skeleton_thread_fragment(rows[index:end])
        if compacted is None:
            result.append(copy.deepcopy(item))
            index += 1
            continue
        result.append(compacted)
        index = end
    return result


def _nonempty_string_list(value: Any, *, unique: bool = False) -> list[str] | None:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        return None
    if unique and len(set(value)) != len(value):
        return None
    return copy.deepcopy(value)


def _normalize_skeleton_completion_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or set(payload) != _SKELETON_THREAD_COMPLETION_FIELDS:
        return None
    normalized = copy.deepcopy(payload)
    for field in _SKELETON_THREAD_COMPLETION_LIST_FIELDS:
        value = normalized[field]
        if isinstance(value, dict) and set(value) == {"item"}:
            value = value["item"]
        values = _nonempty_string_list(
            value, unique=field == "objective_evidence_ids"
        )
        if values is None:
            return None
        normalized[field] = values

    for field in (
        "question_statement",
        "objective_statement",
        "falsification_or_comparison_rule",
    ):
        if not isinstance(normalized[field], str) or not normalized[field].strip():
            return None
    if normalized["question_type"] not in {
        "SCIENTIFIC",
        "TECHNICAL",
        "ENGINEERING",
    }:
        return None
    if normalized["answerability"] not in {
        "TESTABLE",
        "COMPARABLE",
        "DESIGN_VERIFIABLE",
        "UNCLEAR",
    }:
        return None
    return normalized


def _normalize_skeleton_mechanical_artifacts(candidate: Any) -> Any:
    """Lift only an exact, conflict-free MiniMax thread-field displacement.

    Two observed transport artifacts place the eight completion fields of one
    research thread either inside its evidence-id array or behind an ``item``
    wrapper in place of that array.  The lift is allowed only when the row has
    exactly the four complementary anchor fields and every displaced value has
    its contract type. Ambiguous candidates remain untouched for normal retry.
    """

    normalized = copy.deepcopy(candidate)
    if not isinstance(normalized, dict):
        return normalized
    threads = normalized.get("research_threads")
    if not isinstance(threads, list):
        return normalized
    normalized["research_threads"] = _compact_skeleton_thread_collection(threads)
    threads = normalized["research_threads"]

    for index, thread in enumerate(threads):
        if not isinstance(thread, dict) or set(thread) != _SKELETON_THREAD_ANCHOR_FIELDS:
            continue
        malformed_evidence = thread.get("limitation_mechanism_evidence_ids")
        evidence_ids: list[str] | None = None
        displaced: Any = None

        if isinstance(malformed_evidence, list):
            displaced_items = [
                item for item in malformed_evidence if isinstance(item, dict)
            ]
            scalar_items = [
                item for item in malformed_evidence if not isinstance(item, dict)
            ]
            if len(displaced_items) != 1:
                continue
            evidence_ids = _nonempty_string_list(scalar_items, unique=True)
            displaced = displaced_items[0]
        elif (
            isinstance(malformed_evidence, dict)
            and set(malformed_evidence)
            == {"item", "limitation_mechanism_evidence_ids"}
        ):
            item_value = malformed_evidence["item"]
            if isinstance(item_value, str):
                item_value = [item_value]
            evidence_ids = _nonempty_string_list(item_value, unique=True)
            displaced = malformed_evidence["limitation_mechanism_evidence_ids"]
        else:
            continue

        completion = _normalize_skeleton_completion_payload(displaced)
        if evidence_ids is None or completion is None:
            continue
        repaired_thread = copy.deepcopy(thread)
        repaired_thread["limitation_mechanism_evidence_ids"] = evidence_ids
        repaired_thread.update(completion)
        threads[index] = repaired_thread
    return normalized


def _local_index_tuple(item: Any, fields: tuple[str, ...]) -> tuple[int, ...] | None:
    if not isinstance(item, dict):
        return None
    values: list[int] = []
    for field in fields:
        value = item.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        values.append(value)
    return tuple(values)


def _local_relation_tuple(
    item: Any,
    fields: tuple[str, ...],
    *,
    nullable_fields: frozenset[str] = frozenset(),
) -> tuple[Any, ...] | None:
    """Return an exact local relation identity without guessing malformed values."""
    if not isinstance(item, dict):
        return None
    values: list[Any] = []
    for field in fields:
        value = item.get(field)
        if value is None and field in nullable_fields:
            values.append(None)
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        values.append(value)
    return tuple(values)


def _normalize_design_mechanical_artifacts(
    candidate: Any,
    frozen_skeleton: dict[str, Any],
) -> Any:
    """Remove only unambiguous Design-stage mechanical defects.

    The provider response remains immutable.  This function never renumbers a
    record, guesses an intended parent, or changes semantic text.  Malformed or
    ambiguous core records remain in the candidate so normal validation can
    request a model retry.
    """
    normalized = copy.deepcopy(candidate)
    if not isinstance(normalized, dict):
        return normalized

    def dedupe_owned_rows(
        collection: str,
        key_fn: Any,
    ) -> None:
        inherited = frozen_skeleton.get(collection)
        rows = normalized.get(collection)
        if not isinstance(rows, list):
            return
        seen = {
            key
            for key in (key_fn(item) for item in inherited or [])
            if key is not None
        }
        kept: list[Any] = []
        for item in rows:
            key = key_fn(item)
            if key is not None and key in seen:
                continue
            kept.append(item)
            if key is not None:
                seen.add(key)
        normalized[collection] = kept

    dedupe_owned_rows("user_questions", _question_key)
    dedupe_owned_rows("evidence_gaps", _gap_key)

    def dedupe_relation_rows(
        collection: str,
        fields: tuple[str, ...],
        *,
        nullable_fields: frozenset[str] = frozenset(),
    ) -> None:
        """Canonicalize exact relation-set duplicates, preserving first occurrence."""
        rows = normalized.get(collection)
        if not isinstance(rows, list):
            return
        seen: set[tuple[Any, ...]] = set()
        kept: list[Any] = []
        for item in rows:
            key = _local_relation_tuple(
                item, fields, nullable_fields=nullable_fields
            )
            if key is not None and key in seen:
                continue
            kept.append(item)
            if key is not None:
                seen.add(key)
        normalized[collection] = kept

    # Relationship rows are set-valued canonical facts.  Repeating the exact
    # same endpoints cannot add semantics, so Runtime removes duplicates before
    # Stage-B schema/reference validation rather than spending a provider retry.
    dedupe_relation_rows(
        "innovation_evaluation_refs",
        (
            "thread_index",
            "innovation_index",
            "work_package_index",
            "method_index",
            "evaluation_index",
        ),
    )
    dedupe_relation_rows(
        "foundation_supports",
        (
            "thread_index",
            "foundation_index",
            "work_package_index",
            "method_index",
        ),
        nullable_fields=frozenset({"method_index"}),
    )

    def keyset(collection: str, fields: tuple[str, ...]) -> set[tuple[int, ...]]:
        rows = normalized.get(collection)
        if not isinstance(rows, list):
            return set()
        return {
            key
            for key in (_local_index_tuple(item, fields) for item in rows)
            if key is not None
        }

    work_packages = keyset("work_packages", ("thread_index", "work_package_index"))
    methods = keyset(
        "methods", ("thread_index", "work_package_index", "method_index")
    )
    evaluations = keyset(
        "evaluations",
        ("thread_index", "work_package_index", "method_index", "evaluation_index"),
    )
    innovations = keyset("innovations", ("thread_index", "innovation_index"))
    foundation = keyset("foundation", ("thread_index", "foundation_index"))

    def prune_if_resolved_but_missing(
        collection: str,
        fields: tuple[str, ...],
        parents: set[tuple[int, ...]],
    ) -> None:
        rows = normalized.get(collection)
        if not isinstance(rows, list):
            return
        normalized[collection] = [
            item
            for item in rows
            if (key := _local_index_tuple(item, fields)) is None or key in parents
        ]

    # These collections are optional leaves.  An explicitly indexed row whose
    # parent does not exist cannot be assembled without guessing, so dropping
    # that row is the only deterministic repair.
    evaluation_fields = (
        "thread_index",
        "work_package_index",
        "method_index",
        "evaluation_index",
    )
    prune_if_resolved_but_missing("baselines", evaluation_fields, evaluations)
    prune_if_resolved_but_missing("ablations", evaluation_fields, evaluations)
    prune_if_resolved_but_missing(
        "innovation_prior_work",
        ("thread_index", "innovation_index"),
        innovations,
    )

    refs = normalized.get("innovation_evaluation_refs")
    if isinstance(refs, list):
        kept_refs: list[Any] = []
        for item in refs:
            innovation_key = _local_index_tuple(
                item, ("thread_index", "innovation_index")
            )
            evaluation_key = _local_index_tuple(item, evaluation_fields)
            if innovation_key is not None and innovation_key not in innovations:
                continue
            if evaluation_key is not None and evaluation_key not in evaluations:
                continue
            kept_refs.append(item)
        normalized["innovation_evaluation_refs"] = kept_refs

    supports = normalized.get("foundation_supports")
    if isinstance(supports, list):
        kept_supports: list[Any] = []
        for item in supports:
            foundation_key = _local_index_tuple(
                item, ("thread_index", "foundation_index")
            )
            if foundation_key is not None and foundation_key not in foundation:
                continue
            if isinstance(item, dict) and item.get("method_index") is None:
                target = _local_index_tuple(
                    item, ("thread_index", "work_package_index")
                )
                if target is not None and target not in work_packages:
                    continue
            else:
                target = _local_index_tuple(
                    item,
                    ("thread_index", "work_package_index", "method_index"),
                )
                if target is not None and target not in methods:
                    continue
            kept_supports.append(item)
        normalized["foundation_supports"] = kept_supports

    return normalized


def _normalize_stage_structural_artifacts(
    stage: str,
    candidate: Any,
    *,
    frozen_skeleton: dict[str, Any] | None,
) -> Any:
    """Normalize deterministic structure without adding synthetic defaults.

    Retry isolation must compare provider-authored structure (after exact,
    auditable artifact removal), never the validation projection enriched with
    runtime defaults. Keeping this boundary explicit prevents defaults such as
    ``allowed_values=[]`` or ``cannot_proceed_reason=null`` from becoming
    immutable row identity during a later scoped merge.
    """

    candidate = _normalize_stage_root_and_optional_rows(stage, candidate)
    if stage == ARGUMENT_SKELETON_STAGE:
        return _normalize_skeleton_mechanical_artifacts(candidate)
    if stage == ARGUMENT_DESIGN_STAGE:
        if frozen_skeleton is None:
            raise ValueError("Design stage requires frozen_skeleton")
        return _normalize_design_mechanical_artifacts(candidate, frozen_skeleton)
    raise ValueError(f"Unknown Argument stage: {stage}")


def _stage_candidate_has_substantive_draft(stage: str, candidate: Any) -> bool:
    """Return whether a rejected response contains stage-owned work worth preserving.

    This intentionally distinguishes a completely empty generation from an
    incomplete Draft.  Missing roots, collections, objects, or fields in a
    Draft that contains any authored stage content belong to targeted repair;
    an empty/non-object response has nothing that a targeted repair can safely
    preserve and must be regenerated as the same full stage.
    """

    if not isinstance(candidate, dict):
        return False

    def has_authored_value(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, dict):
            return any(has_authored_value(child) for child in value.values())
        if isinstance(value, list):
            return any(has_authored_value(child) for child in value)
        return False

    if stage == ARGUMENT_SKELETON_STAGE:
        roots = ("central_proposition", "scope", "research_threads")
    elif stage == ARGUMENT_DESIGN_STAGE:
        roots = _ARGUMENT_DESIGN_THREAD_COLLECTIONS
    else:
        raise ValueError(f"Unknown Argument stage: {stage}")
    authored_roots = (
        *roots,
        "evidence_gaps",
        "user_questions",
        "cannot_proceed_reason",
    )
    return any(has_authored_value(candidate.get(root)) for root in authored_roots)


def _readiness_conflict_errors(
    candidate: dict[str, Any],
    *,
    frozen_skeleton: dict[str, Any] | None = None,
) -> list[str]:
    """Validate final readiness invariants while the responsible stage can still retry."""
    inherited = frozen_skeleton or {}
    inherited_reason = str(inherited.get("cannot_proceed_reason") or "").strip()
    candidate_reason = str(candidate.get("cannot_proceed_reason") or "").strip()
    effective_reason = candidate_reason or inherited_reason

    inherited_questions = [
        item for item in inherited.get("user_questions") or [] if isinstance(item, dict)
    ]
    candidate_questions = [
        item for item in candidate.get("user_questions") or [] if isinstance(item, dict)
    ]
    blocking_exists = any(
        bool(item.get("blocking"))
        for item in (*inherited_questions, *candidate_questions)
    )

    errors: list[str] = []
    if frozen_skeleton is None:
        threads = candidate.get("research_threads")
        thread_count = len(threads) if isinstance(threads, list) else 0
        if thread_count == 0 and not blocking_exists and not effective_reason:
            errors.append(
                "/research_threads: must contain 1-4 research threads when the Skeleton "
                "has no blocking user question and no cannot_proceed_reason"
            )
    if effective_reason and blocking_exists:
        errors.append(
            "/cannot_proceed_reason: must be JSON null (without quotes), never the string \"null\", "
            "whenever frozen_skeleton or the current stage contains blocking=true user_questions; "
            "use blocking questions for answerable missing information and reserve "
            "cannot_proceed_reason for a blocker that cannot be resolved by those questions"
        )
    if inherited_reason and candidate_reason and inherited_reason != candidate_reason:
        errors.append(
            "/cannot_proceed_reason: conflicts with frozen_skeleton.cannot_proceed_reason"
        )

    inherited_question_keys = {
        key for key in (_question_key(item) for item in inherited_questions) if key
    }
    seen_questions = set(inherited_question_keys)
    duplicate_question_indexes: list[int] = []
    for index, item in enumerate(candidate_questions):
        key = _question_key(item)
        if not key:
            continue
        if key in seen_questions:
            duplicate_question_indexes.append(index)
        else:
            seen_questions.add(key)
    if duplicate_question_indexes:
        owner = "frozen_skeleton/current-stage" if frozen_skeleton is not None else "current-stage"
        errors.append(
            "/user_questions: duplicate question indexes "
            + str(duplicate_question_indexes)
            + f" are already owned by {owner}; keep only genuinely new questions"
        )

    inherited_gap_keys = {
        key
        for key in (_gap_key(item) for item in inherited.get("evidence_gaps") or [])
        if key is not None
    }
    seen_gap_keys = set(inherited_gap_keys)
    duplicate_gap_indexes: list[int] = []
    for index, item in enumerate(candidate.get("evidence_gaps") or []):
        key = _gap_key(item)
        if key is None:
            continue
        if key in seen_gap_keys:
            duplicate_gap_indexes.append(index)
        else:
            seen_gap_keys.add(key)
    if duplicate_gap_indexes:
        errors.append(
            "/evidence_gaps: duplicate gap indexes "
            + str(duplicate_gap_indexes)
            + " repeat an existing deterministic gap identity; keep only genuinely new design-stage gaps"
        )
    return errors


def _argument_stage_validation_errors(
    *,
    stage: str,
    model_input: dict[str, Any],
    candidate: Any,
    frozen_skeleton: dict[str, Any] | None,
) -> tuple[list[str], list[str], list[str], list[str], str]:
    if stage == ARGUMENT_SKELETON_STAGE:
        shape_errors = argument_skeleton_model_output_errors(candidate)
        reference_errors = (
            _skeleton_reference_errors(model_input, candidate)
            if isinstance(candidate, dict)
            else []
        )
        cross_stage_errors = (
            _readiness_conflict_errors(candidate)
            if isinstance(candidate, dict)
            else []
        )
    elif stage == ARGUMENT_DESIGN_STAGE:
        if frozen_skeleton is None:
            raise ValueError("Design stage requires frozen_skeleton")
        shape_errors = argument_design_model_output_errors(candidate)
        reference_errors = (
            _design_reference_errors(model_input, candidate, frozen_skeleton)
            if isinstance(candidate, dict)
            else []
        )
        cross_stage_errors = (
            _readiness_conflict_errors(
                candidate, frozen_skeleton=frozen_skeleton
            )
            if isinstance(candidate, dict)
            else []
        )
    else:
        raise ValueError(f"Unknown Argument stage: {stage}")

    errors = [*shape_errors, *reference_errors, *cross_stage_errors]
    if shape_errors:
        phase = "structure_validation"
    elif cross_stage_errors:
        phase = "cross_stage_validation"
    else:
        phase = "reference_validation"
    return shape_errors, reference_errors, cross_stage_errors, errors, phase


def _bounded_stage_retry_errors(
    shape_errors: list[str],
    reference_errors: list[str],
    cross_stage_errors: list[str],
    *,
    limit: int = 20,
) -> list[str]:
    """Keep retry feedback small without hiding an entire error category."""

    groups = [shape_errors, reference_errors, cross_stage_errors]
    selected: list[str] = []
    offsets = [0, 0, 0]
    while len(selected) < limit:
        added = False
        for index, group in enumerate(groups):
            offset = offsets[index]
            if offset >= len(group):
                continue
            selected.append(group[offset])
            offsets[index] += 1
            added = True
            if len(selected) >= limit:
                break
        if not added:
            break
    return selected


def _pointer_text(path: tuple[str, ...]) -> str:
    return "/" + "/".join(
        token.replace("~", "~0").replace("/", "~1") for token in path
    )


def _set_stage_repair_pointer(root: Any, path: tuple[str, ...], value: Any) -> None:
    if not path or not isinstance(root, dict):
        raise ValueError("stage repair cannot replace the Draft root")
    parent = root
    for token in path[:-1]:
        if isinstance(parent, dict) and token in parent:
            parent = parent[token]
            continue
        if isinstance(parent, list) and token.isdigit():
            index = int(token)
            if 0 <= index < len(parent):
                parent = parent[index]
                continue
        raise ValueError(f"stage repair parent does not exist: {_pointer_text(path)}")
    leaf = path[-1]
    if isinstance(parent, dict):
        parent[leaf] = copy.deepcopy(value)
        return
    if isinstance(parent, list) and leaf.isdigit():
        index = int(leaf)
        if 0 <= index < len(parent):
            parent[index] = copy.deepcopy(value)
            return
    raise ValueError(f"stage repair target does not exist: {_pointer_text(path)}")


def _delete_stage_repair_property(root: Any, path: tuple[str, ...]) -> None:
    if not path or not isinstance(root, dict):
        raise ValueError("stage repair cannot delete the Draft root")
    parent = root
    for token in path[:-1]:
        if isinstance(parent, dict) and token in parent:
            parent = parent[token]
            continue
        if isinstance(parent, list) and token.isdigit():
            index = int(token)
            if 0 <= index < len(parent):
                parent = parent[index]
                continue
        raise ValueError(f"stage repair parent does not exist: {_pointer_text(path)}")
    leaf = path[-1]
    if isinstance(parent, dict) and leaf in parent:
        del parent[leaf]
        return
    raise ValueError(f"stage repair property does not exist: {_pointer_text(path)}")


def _stage_contract_at_path(stage: str, path: tuple[str, ...]) -> Any:
    target_schema: Any = argument_stage_output_schema(stage)
    for token in path:
        if not isinstance(target_schema, dict):
            return _MISSING
        if token.isdigit():
            target_schema = target_schema.get("items")
        else:
            target_schema = (target_schema.get("properties") or {}).get(token)
        if target_schema is None:
            return _MISSING
    return target_schema


def _stage_repair_local_context(
    stage: str,
    candidate: dict[str, Any],
    path: tuple[str, ...],
    model_input: dict[str, Any],
) -> dict[str, Any]:
    context: dict[str, Any] = {"stage": stage}
    if path:
        context["collection"] = path[0]
    target_schema = _stage_contract_at_path(stage, path)
    if isinstance(target_schema, dict):
        context["target_contract"] = copy.deepcopy(target_schema)
    elif _value_at_pointer(candidate, path) is not _MISSING:
        context["null_value_action"] = "DELETE_INVALID_PROPERTY"
    if len(path) >= 2 and path[1].isdigit():
        context["null_value_action"] = "DELETE_INVALID_LIST_ROW"
        row = _value_at_pointer(candidate, path[:2])
        if isinstance(row, dict):
            thread_index = row.get("thread_index")
            if isinstance(thread_index, int) and not isinstance(thread_index, bool):
                frozen = model_input.get("frozen_skeleton") or {}
                threads = frozen.get("research_threads") or []
                if 0 <= thread_index < len(threads):
                    context["research_thread"] = copy.deepcopy(threads[thread_index])

            parent_specs: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
                "methods": (("work_packages", ("thread_index", "work_package_index")),),
                "theoretical_properties": (
                    ("work_packages", ("thread_index", "work_package_index")),
                    ("methods", ("thread_index", "work_package_index", "method_index")),
                ),
                "evaluations": (
                    ("work_packages", ("thread_index", "work_package_index")),
                    ("methods", ("thread_index", "work_package_index", "method_index")),
                ),
                "baselines": (
                    ("work_packages", ("thread_index", "work_package_index")),
                    ("methods", ("thread_index", "work_package_index", "method_index")),
                    ("evaluations", ("thread_index", "work_package_index", "method_index", "evaluation_index")),
                ),
                "ablations": (
                    ("work_packages", ("thread_index", "work_package_index")),
                    ("methods", ("thread_index", "work_package_index", "method_index")),
                    ("evaluations", ("thread_index", "work_package_index", "method_index", "evaluation_index")),
                ),
                "innovation_prior_work": (
                    ("innovations", ("thread_index", "innovation_index")),
                ),
                "innovation_evaluation_refs": (
                    ("innovations", ("thread_index", "innovation_index")),
                    ("evaluations", ("thread_index", "work_package_index", "method_index", "evaluation_index")),
                ),
                "foundation_supports": (
                    ("foundation", ("thread_index", "foundation_index")),
                    ("work_packages", ("thread_index", "work_package_index")),
                    ("methods", ("thread_index", "work_package_index", "method_index")),
                ),
            }
            related_records: dict[str, Any] = {}
            candidate_parent_records: dict[str, list[Any]] = {}
            for parent_collection, key_fields in parent_specs.get(path[0], ()):
                if any(row.get(field) is None for field in key_fields):
                    continue
                parent = next(
                    (
                        item
                        for item in candidate.get(parent_collection) or []
                        if isinstance(item, dict)
                        and all(item.get(field) == row.get(field) for field in key_fields)
                    ),
                    None,
                )
                if parent is not None:
                    related_records[parent_collection] = copy.deepcopy(parent)
                    continue
                candidates = [
                    copy.deepcopy(item)
                    for item in candidate.get(parent_collection) or []
                    if isinstance(item, dict)
                    and (
                        thread_index is None
                        or item.get("thread_index") == thread_index
                    )
                ][:4]
                if candidates:
                    candidate_parent_records[parent_collection] = candidates
            if related_records:
                context["direct_parent_records"] = related_records
            if candidate_parent_records:
                context["candidate_parent_records"] = candidate_parent_records
    return context


def _build_stage_repair_input(
    *,
    stage: str,
    candidate: dict[str, Any],
    errors: list[str],
    model_input: dict[str, Any],
) -> tuple[dict[str, Any], tuple[tuple[str, ...], ...]]:
    all_scopes = argument_stage_repair_scope_paths(errors)
    errored_collections = {path[0] for path in all_scopes if path}

    def is_derived_parent_error(error: str) -> bool:
        pointer_text, separator, message = str(error).partition(": ")
        path = _decode_error_pointer(pointer_text) if separator else None
        if not path:
            return False
        collection = path[0]
        parent_collection = None
        if "unresolved parent index" in message:
            parent_collection = {
                "methods": "work_packages",
                "theoretical_properties": "methods",
                "evaluations": "methods",
                "baselines": "evaluations",
                "ablations": "evaluations",
                "innovation_prior_work": "innovations",
                "innovation_evaluation_refs": "innovations",
                "foundation_supports": "foundation",
            }.get(collection)
        elif "unresolved evaluation index" in message:
            parent_collection = "evaluations"
        elif "unresolved work-package index" in message:
            parent_collection = "work_packages"
        elif "unresolved method index" in message:
            parent_collection = "methods"
        return bool(parent_collection and parent_collection in errored_collections)

    active_errors = [error for error in errors if not is_derived_parent_error(error)]
    scopes = argument_stage_repair_scope_paths(active_errors)
    if not scopes:
        raise ArgumentStageContractError(
            stage,
            "repair_scope_resolution",
            errors,
            candidate=candidate,
        )
    targets: list[dict[str, Any]] = []
    for index, path in enumerate(scopes, 1):
        current = _value_at_pointer(candidate, path)
        related = [
            error
            for error in active_errors
            if any(
                error_path[: len(path)] == path
                for error_path in argument_stage_repair_scope_paths([error])
            )
        ]
        targets.append(
            {
                "target_id": f"stage-repair-{index:03d}",
                "path": _pointer_text(path),
                "path_exists": current is not _MISSING,
                "current_value": None if current is _MISSING else copy.deepcopy(current),
                "problem": related or list(active_errors[:8]),
                "local_context": _stage_repair_local_context(
                    stage, candidate, path, model_input
                ),
            }
        )
    referenced_evidence_ids: set[str] = set()

    def collect_evidence_ids(value: Any, parent_key: str | None = None) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                collect_evidence_ids(child, str(key))
            return
        if isinstance(value, list):
            if parent_key and parent_key.endswith("evidence_ids"):
                referenced_evidence_ids.update(
                    str(item) for item in value if isinstance(item, str) and item
                )
                return
            for child in value:
                collect_evidence_ids(child)

    collect_evidence_ids(targets)
    evidence_cards = [
        card
        for card in model_input.get("evidence_cards") or []
        if isinstance(card, dict) and card.get("evidence_id")
    ]
    evidence_cards.sort(
        key=lambda card: (
            str(card.get("evidence_id")) not in referenced_evidence_ids,
            str(card.get("evidence_id")),
        )
    )
    entities = [
        {
            "entity_id": str(card.get("evidence_id") or ""),
            "entity_type": str(card.get("evidence_type") or card.get("kind") or "EVIDENCE"),
            "statement": str(card.get("statement") or card.get("claim_text") or ""),
            "status": str(card.get("knowledge_status") or "AVAILABLE"),
        }
        for card in evidence_cards[:16]
    ]
    human_resolutions: list[dict[str, Any]] = []
    for resolution in model_input.get("human_resolutions") or []:
        if not isinstance(resolution, dict):
            continue
        targets = resolution.get("target_paths") or resolution.get("resolved_target_paths") or []
        if isinstance(targets, str):
            targets = [targets]
        answer = (
            resolution.get("answer")
            if "answer" in resolution
            else resolution.get("resolved_value")
            if "resolved_value" in resolution
            else resolution.get("value")
        )
        human_resolutions.extend(
            {"target": str(target), "answer": copy.deepcopy(answer)}
            for target in targets
            if str(target).strip()
        )
    return (
        {
            "task_context": {
                "producer_role": "Argument Architecture Agent",
                "object_type": f"ARGUMENT_{stage}_DRAFT",
            },
            "repair_targets": targets,
            "reference_context": {
                "related_findings": [],
                "candidate_entities": entities,
                "semantic_neighborhoods": [],
                "status_context": {
                    "current_status": "DRAFT_INVALID",
                    "blocking_user_questions": sum(
                        1
                        for item in candidate.get("user_questions") or []
                        if isinstance(item, dict) and bool(item.get("blocking"))
                    ),
                    "blocking_user_findings": 0,
                },
                "human_resolutions": human_resolutions,
            },
            "previous_attempt_feedback": list(active_errors[:20]),
        },
        scopes,
    )


def _apply_stage_repair_changes(
    *,
    stage: str,
    candidate: dict[str, Any],
    repair_output: Any,
    scopes: tuple[tuple[str, ...], ...],
) -> dict[str, Any]:
    schema_errors = sorted(
        Draft202012Validator(argument_stage_repair_output_schema()).iter_errors(
            repair_output
        ),
        key=lambda error: list(error.absolute_path),
    )
    if schema_errors:
        raise ArgumentStageContractError(
            stage,
            "repair_response_validation",
            [
                "/" + "/".join(str(token) for token in error.absolute_path)
                + f": {error.message}"
                for error in schema_errors
            ],
            candidate=candidate,
        )
    if repair_output.get("decision") != "APPLY":
        raise ArgumentStageContractError(
            stage,
            "repair_escalated",
            [str(repair_output.get("escalation_reason") or "stage repair escalated")],
            candidate=candidate,
        )
    changes = repair_output.get("changes") or []
    if not changes:
        raise ArgumentStageContractError(
            stage,
            "repair_response_validation",
            ["/changes: APPLY requires at least one exact Draft replacement"],
            candidate=candidate,
        )
    repaired = copy.deepcopy(candidate)
    seen: set[tuple[str, ...]] = set()
    row_deletions: list[tuple[str, ...]] = []
    property_deletions: list[tuple[str, ...]] = []
    for index, change in enumerate(changes):
        path = _decode_error_pointer(str(change.get("path") or ""))
        if path is None or path not in scopes:
            raise ArgumentStageContractError(
                stage,
                "repair_scope_validation",
                [f"/changes/{index}/path: must equal one authorized Draft target"],
                candidate=candidate,
            )
        if path in seen:
            raise ArgumentStageContractError(
                stage,
                "repair_scope_validation",
                [f"/changes/{index}/path: duplicate repair target"],
                candidate=candidate,
            )
        seen.add(path)
        if (
            change.get("value") is None
            and len(path) == 2
            and path[1].isdigit()
        ):
            row_deletions.append(path)
            continue
        if (
            change.get("value") is None
            and _stage_contract_at_path(stage, path) is _MISSING
        ):
            property_deletions.append(path)
            continue
        _set_stage_repair_pointer(repaired, path, change.get("value"))
    for path in sorted(row_deletions, key=lambda item: (item[0], -int(item[1]))):
        rows = repaired.get(path[0])
        index = int(path[1])
        if not isinstance(rows, list) or not (0 <= index < len(rows)):
            raise ArgumentStageContractError(
                stage,
                "repair_scope_validation",
                [f"{_pointer_text(path)}: invalid row deletion target"],
                candidate=candidate,
            )
        rows.pop(index)
    for path in property_deletions:
        _delete_stage_repair_property(repaired, path)
    return repaired


async def _invoke_validated_stage(
    *,
    stage: str,
    model_input: dict[str, Any],
    stage_gateway: ArgumentStageGateway,
    max_attempts: int,
    frozen_skeleton: dict[str, Any] | None = None,
    initial_retry_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    previous_structural_candidate: Any = None
    previous_candidate: Any = None
    previous_errors: list[str] = []
    previous_retry_errors: list[str] = []
    previous_phase = "structure_validation"
    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            retry_context = copy.deepcopy(initial_retry_context)
        elif (
            stage == ARGUMENT_DESIGN_STAGE
            and isinstance(initial_retry_context, dict)
            and initial_retry_context.get("mode") == "WHOLE_DESIGN_REVISE"
        ):
            # Semantic regeneration is different from an initial stage retry:
            # it has an accepted, persisted baseline.  Every provider retry must
            # therefore restart from that exact baseline, never from the failed
            # response and never from a blank Design.
            retry_context = copy.deepcopy(initial_retry_context)
            retry_context.update(
                {
                    "attempt": attempt,
                    "recovery_mode": "WHOLE_DESIGN_REVISE_RETRY",
                    "validation_errors": (
                        list(previous_retry_errors)
                        if previous_retry_errors
                        else [
                            "Previous revision response was unusable; return the complete Design from the accepted baseline."
                        ]
                    ),
                }
            )
        else:
            retry_context = {
                "attempt": attempt,
                "recovery_mode": "FULL_STAGE_RETRY",
                "validation_errors": (
                    list(previous_retry_errors)
                    if _stage_candidate_has_substantive_draft(
                        stage, previous_structural_candidate
                    )
                    else [
                        "Previous response contained no usable Draft; generate the complete stage output."
                    ]
                ),
            }
        raw_candidate = await stage_gateway.invoke_stage(
            stage,
            copy.deepcopy(model_input),
            argument_stage_output_schema(stage),
            retry_context=retry_context,
            desired_output_tokens=argument_stage_desired_output_tokens(stage),
        )
        structural_candidate = _normalize_stage_structural_artifacts(
            stage,
            raw_candidate,
            frozen_skeleton=frozen_skeleton,
        )

        candidate, _current_defaults = (
            _normalize_stage_mechanical_defaults_with_provenance(
                structural_candidate
            )
        )

        (
            shape_errors,
            reference_errors,
            cross_stage_errors,
            errors,
            phase,
        ) = _argument_stage_validation_errors(
            stage=stage,
            model_input=model_input,
            candidate=candidate,
            frozen_skeleton=frozen_skeleton,
        )

        if reference_errors:
            repaired_structural_candidate, resolved_reference_errors = (
                _drop_reported_unknown_evidence_ids(
                    structural_candidate, reference_errors, stage=stage
                )
            )
            if resolved_reference_errors:
                structural_candidate = repaired_structural_candidate
                candidate, _current_defaults = (
                    _normalize_stage_mechanical_defaults_with_provenance(
                        structural_candidate
                    )
                )
                (
                    shape_errors,
                    reference_errors,
                    cross_stage_errors,
                    errors,
                    phase,
                ) = _argument_stage_validation_errors(
                    stage=stage,
                    model_input=model_input,
                    candidate=candidate,
                    frozen_skeleton=frozen_skeleton,
                )

        revision_errors = _argument_whole_design_revision_errors(
            candidate,
            retry_context=retry_context,
        )
        if revision_errors:
            cross_stage_errors = [*cross_stage_errors, *revision_errors]
            errors = [*errors, *revision_errors]
            phase = "whole_design_revision_validation"

        if not errors:
            return copy.deepcopy(candidate)
        previous_structural_candidate = copy.deepcopy(structural_candidate)
        previous_candidate = copy.deepcopy(candidate)
        previous_errors = list(errors)
        previous_retry_errors = _bounded_stage_retry_errors(
            shape_errors, reference_errors, cross_stage_errors
        )
        previous_phase = phase

    raise ArgumentStageContractError(
        stage, previous_phase, previous_errors, candidate=previous_candidate
    )


def _argument_blocking_gaps(canonical_output: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in (canonical_output.get("result") or {}).get(
            "evidence_gap_report"
        )
        or []
        if isinstance(item, dict) and bool(item.get("blocking"))
    ]


def _argument_gap_signature(gap: dict[str, Any]) -> tuple[Any, ...]:
    return (
        gap.get("thread_index"),
        str(gap.get("defect_family") or ""),
        str(gap.get("finding_code") or ""),
        str(gap.get("semantic_component") or ""),
        str(gap.get("semantic_object_id") or ""),
    )


def _argument_design_collection_regressions(
    baseline_design: dict[str, Any],
    candidate_design: dict[str, Any],
) -> list[str]:
    """Report any whole-candidate collection shrinkage, including per thread."""

    regressions: list[str] = []
    for collection in _ARGUMENT_DESIGN_THREAD_COLLECTIONS:
        baseline_rows = [
            item
            for item in baseline_design.get(collection) or []
            if isinstance(item, dict)
        ]
        candidate_rows = [
            item
            for item in candidate_design.get(collection) or []
            if isinstance(item, dict)
        ]
        if len(candidate_rows) < len(baseline_rows):
            regressions.append(collection)
        baseline_threads = {
            int(item["thread_index"])
            for item in baseline_rows
            if isinstance(item.get("thread_index"), int)
            and not isinstance(item.get("thread_index"), bool)
        }
        for thread_index in sorted(baseline_threads):
            baseline_count = sum(
                1
                for item in baseline_rows
                if item.get("thread_index") == thread_index
            )
            candidate_count = sum(
                1
                for item in candidate_rows
                if item.get("thread_index") == thread_index
            )
            if candidate_count < baseline_count:
                regressions.append(f"{collection}:thread={thread_index}")
    return regressions


def _argument_design_inventory(design: dict[str, Any]) -> dict[str, Any]:
    """Return a compact, deterministic whole-Design inventory for the model."""

    totals: dict[str, int] = {}
    per_thread: dict[str, dict[str, int]] = {}
    for collection in _ARGUMENT_DESIGN_THREAD_COLLECTIONS:
        rows = [
            item for item in design.get(collection) or [] if isinstance(item, dict)
        ]
        totals[collection] = len(rows)
        for item in rows:
            thread_index = item.get("thread_index")
            if not isinstance(thread_index, int) or isinstance(thread_index, bool):
                continue
            thread_counts = per_thread.setdefault(str(thread_index), {})
            thread_counts[collection] = thread_counts.get(collection, 0) + 1
    return {"totals": totals, "per_thread": per_thread}


def _argument_design_revision_targets(
    baseline_design: dict[str, Any],
    revision_issues: list[Any],
) -> list[dict[str, Any]]:
    """Turn deterministic semantic findings into concise Design targets.

    The special case below is deterministic: the guard says which semantic
    obligation failed, while the accepted Design supplies the exact evaluation
    coordinates.  No model-authored IDs or JSON pointers are required.
    """

    evaluations = [
        item
        for item in baseline_design.get("evaluations") or []
        if isinstance(item, dict)
    ]
    baselines = [
        item
        for item in baseline_design.get("baselines") or []
        if isinstance(item, dict)
    ]
    target_fields = (
        "thread_index",
        "work_package_index",
        "method_index",
        "evaluation_index",
    )

    def key(row: dict[str, Any], fields: tuple[str, ...]) -> tuple[Any, ...]:
        return tuple(row.get(field) for field in fields)

    baseline_support = {
        key(item, target_fields)
        for item in baselines
        if any(str(value).strip() for value in item.get("evidence_ids") or [])
    }
    targets: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for issue in revision_issues:
        if not isinstance(issue, dict):
            continue
        thread_index = issue.get("thread")
        if not isinstance(thread_index, int) or isinstance(thread_index, bool):
            thread_index = None
        code = str(issue.get("code") or "").upper()
        defect_key = str(issue.get("defect_key") or "").upper()
        component = str(issue.get("component") or "RESEARCH_DESIGN").upper()
        is_baseline_support = (
            code == "ARGUMENT_METRIC_JUSTIFICATION_MISSING"
            and "EVALUATION_BASELINE_SUPPORT" in defect_key
        )

        if is_baseline_support:
            missing_evaluations = [
                item
                for item in evaluations
                if (thread_index is None or item.get("thread_index") == thread_index)
                and key(item, target_fields) not in baseline_support
            ]
            for evaluation in missing_evaluations:
                coordinates = key(evaluation, target_fields)
                identity = ("EVALUATION_BASELINE_SUPPORT", *coordinates)
                if identity in seen:
                    continue
                seen.add(identity)
                targets.append(
                    {
                        "target_kind": "EVALUATION_BASELINE_SUPPORT",
                        "thread_index": coordinates[0],
                        "work_package_index": coordinates[1],
                        "method_index": coordinates[2],
                        "evaluation_index": coordinates[3],
                        "required_change": (
                            "Add at least one representative baseline with non-empty evidence_ids for this evaluation."
                        ),
                        "blocking": True,
                        "route": "ORIGINAL_PRODUCER",
                    }
                )
            if missing_evaluations:
                continue

        identity = (
            "SEMANTIC_ISSUE",
            thread_index,
            code,
            component,
            str(issue.get("review_unit_key") or ""),
            str(issue.get("problem") or ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        targets.append(
            {
                "target_kind": "SEMANTIC_ISSUE",
                "thread_index": thread_index,
                "finding_code": code or None,
                "component": component,
                "review_unit_key": issue.get("review_unit_key"),
                "problem": str(issue.get("problem") or "").strip(),
                "required_change": str(issue.get("required_action") or "").strip(),
                "blocking": True,
                "route": "ORIGINAL_PRODUCER",
            }
        )
    return targets


def _argument_revision_error_messages(
    targets: list[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    for target in targets:
        if target.get("target_kind") == "EVALUATION_BASELINE_SUPPORT":
            errors.append(
                "evaluation("
                f"thread={target.get('thread_index')},"
                f"work_package={target.get('work_package_index')},"
                f"method={target.get('method_index')},"
                f"evaluation={target.get('evaluation_index')}"
                "): add at least one evidence-backed baseline"
            )
            continue
        errors.append(
            (
                f"thread={target.get('thread_index')} "
                f"component={target.get('component')}: "
                f"{target.get('required_change') or target.get('problem')}"
            ).strip()
        )
    return errors


def _argument_whole_design_revision_errors(
    candidate_design: dict[str, Any],
    *,
    retry_context: dict[str, Any] | None,
) -> list[str]:
    """Reject partial or regressive responses before deterministic assembly."""

    if not isinstance(retry_context, dict) or retry_context.get("mode") != "WHOLE_DESIGN_REVISE":
        return []
    baseline_design = retry_context.get("previous_candidate")
    if not isinstance(baseline_design, dict):
        return ["WHOLE_DESIGN_REVISE requires the accepted previous_candidate"]

    errors = [
        f"Design collection regressed below accepted baseline: {item}"
        for item in _argument_design_collection_regressions(
            baseline_design, candidate_design
        )
    ]
    required_threads = {
        item
        for item in retry_context.get("required_thread_indices") or []
        if isinstance(item, int) and not isinstance(item, bool)
    }
    present_threads = {
        item.get("thread_index")
        for item in candidate_design.get("work_packages") or []
        if isinstance(item, dict)
        and isinstance(item.get("thread_index"), int)
        and not isinstance(item.get("thread_index"), bool)
    }
    for thread_index in sorted(required_threads - present_threads):
        errors.append(
            f"Complete Design is missing required thread {thread_index}"
        )

    baselines = [
        item
        for item in candidate_design.get("baselines") or []
        if isinstance(item, dict)
    ]
    for target in retry_context.get("exact_revision_targets") or []:
        if not isinstance(target, dict) or target.get("target_kind") != "EVALUATION_BASELINE_SUPPORT":
            continue
        matched = any(
            all(
                item.get(field) == target.get(field)
                for field in (
                    "thread_index",
                    "work_package_index",
                    "method_index",
                    "evaluation_index",
                )
            )
            and any(str(value).strip() for value in item.get("evidence_ids") or [])
            for item in baselines
        )
        if not matched:
            errors.append(
                "Required evidence-backed baseline still missing for evaluation("
                f"thread={target.get('thread_index')},"
                f"work_package={target.get('work_package_index')},"
                f"method={target.get('method_index')},"
                f"evaluation={target.get('evaluation_index')})"
            )
    return errors


def _sanitize_baseline_design_foundation(
    candidate: dict[str, Any],
    model_input: dict[str, Any],
) -> dict[str, Any]:
    """Drop only baseline foundation facts no longer qualified by current evidence policy."""
    normalized = copy.deepcopy(candidate)
    eligible = {
        str(value)
        for value in model_input.get("foundation_eligible_evidence_ids") or []
        if str(value).strip()
    }
    rows = normalized.get("foundation")
    if not isinstance(rows, list):
        return normalized
    removed: set[tuple[int, int]] = set()
    kept: list[Any] = []
    for item in rows:
        key = _local_index_tuple(item, ("thread_index", "foundation_index"))
        evidence_ids = {str(value) for value in (item.get("evidence_ids") or [])} if isinstance(item, dict) else set()
        if key is not None and not (evidence_ids & eligible):
            removed.add(key)
            continue
        kept.append(item)
    normalized["foundation"] = kept
    supports = normalized.get("foundation_supports")
    if isinstance(supports, list) and removed:
        normalized["foundation_supports"] = [
            item for item in supports
            if _local_index_tuple(item, ("thread_index", "foundation_index")) not in removed
        ]
    return normalized


def _reference_close_baseline_stage(
    *,
    stage: str,
    model_input: dict[str, Any],
    baseline_candidate: dict[str, Any],
    frozen_skeleton: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-close accepted historical Stage state against the current reference registries."""
    structural_candidate = _normalize_stage_structural_artifacts(
        stage, baseline_candidate, frozen_skeleton=frozen_skeleton
    )
    if stage == ARGUMENT_DESIGN_STAGE:
        structural_candidate = _sanitize_baseline_design_foundation(
            structural_candidate, model_input
        )
    candidate, _defaults = _normalize_stage_mechanical_defaults_with_provenance(
        structural_candidate
    )
    shape_errors, reference_errors, cross_stage_errors, errors, phase = (
        _argument_stage_validation_errors(
            stage=stage,
            model_input=model_input,
            candidate=candidate,
            frozen_skeleton=frozen_skeleton,
        )
    )
    if reference_errors:
        repaired_structural, resolved = _drop_reported_unknown_evidence_ids(
            structural_candidate, reference_errors, stage=stage
        )
        if resolved:
            structural_candidate = repaired_structural
            candidate, _defaults = _normalize_stage_mechanical_defaults_with_provenance(
                structural_candidate
            )
            shape_errors, reference_errors, cross_stage_errors, errors, phase = (
                _argument_stage_validation_errors(
                    stage=stage,
                    model_input=model_input,
                    candidate=candidate,
                    frozen_skeleton=frozen_skeleton,
                )
            )
    if errors:
        raise ArgumentStageContractError(
            stage, f"baseline_{phase}", errors, candidate=candidate
        )
    return candidate


async def orchestrate_argument_architecture_two_stage(
    canonical_envelope: dict[str, Any],
    *,
    stage_gateway: ArgumentStageGateway,
    pack: Any,
    max_stage_attempts: int = 3,
    stage_input_envelope: dict[str, Any] | None = None,
    regeneration_baseline_authored_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the two internal semantic stages and project against trusted canonical state.

    ``stage_input_envelope`` is the provider-facing business projection used to
    construct Skeleton/Design inputs and to validate stage-local references against
    exactly what the model was allowed to see. Deterministic assembly and final
    canonical projection still use ``canonical_envelope`` as authoritative state.
    """
    provider_source = stage_input_envelope if stage_input_envelope is not None else canonical_envelope
    baseline_skeleton: dict[str, Any] | None = None
    baseline_design: dict[str, Any] | None = None
    reference_closed_baseline_authored_state: dict[str, Any] | None = None
    revision_issues = build_argument_architecture_model_input(provider_source).get(
        "revision_issues"
    ) or []
    if regeneration_baseline_authored_state is not None:
        baseline_skeleton, baseline_design = split_argument_authored_state(
            regeneration_baseline_authored_state
        )

    design_only_regeneration = bool(
        baseline_skeleton is not None
        and revision_issues
        and all(
            str(item.get("component") or "").upper()
            not in _ARGUMENT_SKELETON_COMPONENTS
            for item in revision_issues
            if isinstance(item, dict)
        )
    )
    skeleton_input = build_argument_skeleton_model_input(provider_source)
    if design_only_regeneration:
        frozen_skeleton = _reference_close_baseline_stage(
            stage=ARGUMENT_SKELETON_STAGE,
            model_input=skeleton_input,
            baseline_candidate=copy.deepcopy(baseline_skeleton),
        )
    else:
        frozen_skeleton = await _invoke_validated_stage(
            stage=ARGUMENT_SKELETON_STAGE,
            model_input=skeleton_input,
            stage_gateway=stage_gateway,
            max_attempts=max_stage_attempts,
        )

    design_input = build_argument_design_model_input(provider_source, frozen_skeleton)
    design_input["foundation_eligible_evidence_ids"] = (
        argument_foundation_eligible_evidence_ids(canonical_envelope)
    )
    if design_only_regeneration and baseline_design is not None:
        baseline_design = _reference_close_baseline_stage(
            stage=ARGUMENT_DESIGN_STAGE,
            model_input=design_input,
            baseline_candidate=baseline_design,
            frozen_skeleton=frozen_skeleton,
        )
        reference_closed_baseline_authored_state = assemble_argument_authored_state(
            frozen_skeleton, baseline_design
        )
    initial_design_retry_context = None
    if design_only_regeneration and baseline_design is not None:
        exact_revision_targets = _argument_design_revision_targets(
            baseline_design, revision_issues
        )
        initial_design_retry_context = {
            "attempt": 1,
            "mode": "WHOLE_DESIGN_REVISE",
            "required_output": "COMPLETE_DESIGN",
            "required_thread_indices": list(
                range(len(frozen_skeleton.get("research_threads") or []))
            ),
            "previous_candidate": copy.deepcopy(baseline_design),
            "baseline_inventory": _argument_design_inventory(baseline_design),
            "exact_revision_targets": exact_revision_targets,
            "acceptance_rules": [
                "Return every Design collection for every required thread, not a patch.",
                "Preserve all unaffected accepted-baseline records and semantics.",
                "No total or per-thread collection count may shrink below baseline_inventory.",
                "Resolve every exact_revision_target without introducing a new blocking gap.",
            ],
            "validation_errors": _argument_revision_error_messages(
                exact_revision_targets
            ),
        }
    revision_stage_rejection: dict[str, Any] | None = None
    try:
        design_candidate = await _invoke_validated_stage(
            stage=ARGUMENT_DESIGN_STAGE,
            model_input=design_input,
            stage_gateway=stage_gateway,
            max_attempts=max_stage_attempts,
            frozen_skeleton=frozen_skeleton,
            initial_retry_context=initial_design_retry_context,
        )
    except ArgumentStageContractError as exc:
        if not design_only_regeneration or baseline_design is None:
            raise
        # A semantically revised response is optional evidence, whereas the
        # accepted baseline is durable state.  Exhausting malformed/partial
        # revision responses must therefore retain the baseline whole instead
        # of turning a recoverable semantic REVISE into an output-contract crash.
        design_candidate = copy.deepcopy(baseline_design)
        revision_stage_rejection = {
            "phase": exc.phase,
            "validation_errors": list(exc.errors[:20]),
        }

    regeneration_merge = None

    try:
        authored_state = assemble_argument_authored_state(frozen_skeleton, design_candidate)
    except ValueError as exc:
        raise ArgumentStageContractError(
            "ASSEMBLER", "deterministic_assembly", [str(exc)]
        ) from exc

    authored_schema_errors = pack.validate_model(
        "P-ARGUMENT-ARCHITECTURE", "output", authored_state
    )
    authored_semantic_errors = semantic_model_reference_errors(
        "P-ARGUMENT-ARCHITECTURE", canonical_envelope, authored_state
    )
    if authored_schema_errors or authored_semantic_errors:
        raise ArgumentStageContractError(
            "ASSEMBLER",
            "authored_state_validation",
            [*authored_schema_errors, *authored_semantic_errors],
        )

    canonical_output = expand_argument_architecture_model_output(
        canonical_envelope, authored_state
    )
    if (
        regeneration_baseline_authored_state is not None
        and baseline_skeleton is not None
        and baseline_design is not None
    ):
        effective_baseline_authored_state = (
            reference_closed_baseline_authored_state
            if reference_closed_baseline_authored_state is not None
            else regeneration_baseline_authored_state
        )
        baseline_output = expand_argument_architecture_model_output(
            canonical_envelope, effective_baseline_authored_state
        )
        baseline_blocking = _argument_blocking_gaps(baseline_output)
        candidate_blocking = _argument_blocking_gaps(canonical_output)
        candidate_skeleton, candidate_design = split_argument_authored_state(
            authored_state
        )
        regressed_collections = _argument_design_collection_regressions(
            baseline_design, candidate_design
        )
        skeleton_shrank = len(candidate_skeleton.get("research_threads") or []) < len(
            baseline_skeleton.get("research_threads") or []
        )
        baseline_gap_signatures = {
            _argument_gap_signature(item) for item in baseline_blocking
        }
        candidate_gap_signatures = {
            _argument_gap_signature(item) for item in candidate_blocking
        }
        new_blocking_gaps = sorted(
            candidate_gap_signatures - baseline_gap_signatures,
            key=repr,
        )
        if (
            len(candidate_blocking) >= len(baseline_blocking)
            or new_blocking_gaps
            or skeleton_shrank
            or regressed_collections
        ):
            frozen_skeleton = copy.deepcopy(baseline_skeleton)
            design_candidate = copy.deepcopy(baseline_design)
            authored_state = copy.deepcopy(effective_baseline_authored_state)
            canonical_output = baseline_output
            regeneration_merge = {
                "accepted": False,
                "reason": "NO_STRICT_NON_REGRESSIVE_HARD_GAP_IMPROVEMENT",
                "blocking_before": len(baseline_blocking),
                "blocking_after_candidate": len(candidate_blocking),
                "new_blocking_gaps": new_blocking_gaps,
                "skeleton_shrank": skeleton_shrank,
                "regressed_collections": regressed_collections,
            }
            if revision_stage_rejection is not None:
                regeneration_merge["stage_rejection"] = copy.deepcopy(
                    revision_stage_rejection
                )
        else:
            regeneration_merge = {
                "accepted": True,
                "reason": "STRICT_NON_REGRESSIVE_HARD_GAP_IMPROVEMENT",
                "blocking_before": len(baseline_blocking),
                "blocking_after": len(candidate_blocking),
                "new_blocking_gaps": [],
            }
    canonical_errors = pack.validate(
        "P-ARGUMENT-ARCHITECTURE", "output", canonical_output
    )
    if canonical_errors:
        raise ArgumentStageContractError(
            "PROJECTOR", "canonical_validation", canonical_errors
        )

    return {
        "skeleton": copy.deepcopy(frozen_skeleton),
        "design": copy.deepcopy(design_candidate),
        "authored_state": copy.deepcopy(authored_state),
        "canonical_output": canonical_output,
        "regeneration_merge": copy.deepcopy(regeneration_merge),
    }
