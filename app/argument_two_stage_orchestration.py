from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Protocol

from .model_semantic_contracts import (
    argument_design_model_output_errors,
    argument_design_model_reference_errors,
    argument_foundation_eligible_evidence_ids,
    argument_skeleton_model_output_errors,
    assemble_argument_authored_state,
    build_argument_design_model_input,
    build_argument_skeleton_model_input,
    expand_argument_architecture_model_output,
    semantic_model_reference_errors,
)


ARGUMENT_SKELETON_STAGE = "SKELETON"
ARGUMENT_DESIGN_STAGE = "DESIGN"
ARGUMENT_TWO_STAGE_CONTRACT_VERSION = "ARGUMENT_TWO_STAGE_V6"

# Stage-local ceilings replace the legacy one-size-fits-all 131072-token demand
# for the internal two-stage Argument producer.  They are intentionally kept
# here, rather than in the global prompt profile, so no other Producer changes.
ARGUMENT_STAGE_DESIRED_OUTPUT_TOKENS = {
    ARGUMENT_SKELETON_STAGE: 8_192,
    ARGUMENT_DESIGN_STAGE: 65_536,
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
            "只要本阶段存在 `blocking=true` 的用户问题，`cannot_proceed_reason` 必须输出 JSON 空值 "
            "`null`（无引号），不得输出字符串 `\"null\"`；可由用户回答解决的信息缺失用阻断性用户问题表达，"
            "只有无法通过这些问题解决、因而本阶段确实无法形成骨架时才使用非空 `cannot_proceed_reason`。"
            "若输入包含 `retry_context`，以上一轮 `previous_candidate` 为起点，只修正"
            "`validation_errors` 指出的当前阶段问题，其他已有效语义保持不变。"
        )
    if stage == ARGUMENT_DESIGN_STAGE:
        return (
            "# 论证架构：研究设计阶段\n\n"
            "只确定“怎么做、怎么验证、创新在哪里、已有基础支撑什么”。"
            "`frozen_skeleton` 是只读事实：不得重写中心命题、研究范围或研究线程。"
            "围绕冻结线程生成工作包、方法、必要理论性质、验证方案、代表性基线、必要消融、"
            "创新点及其最近工作/验证关联；只有存在合格证据时才声明团队基础。\n\n"
            "使用输出契约规定的局部整数索引建立这些语义记录之间的关联；"
            "不要生成机器 ID、关系 ID、Hash 或运行时字段。证据引用只能使用输入已有的 "
            "`evidence_id`。信息不足时允许相应记录为空，并如实记录 evidence gap、"
            "用户问题或不能继续的原因；不得重复 `frozen_skeleton` 中已有的问题或缺口。"
            "`foundation_eligible_evidence_ids` 是团队基础唯一允许使用的证据集合；该集合为空时 "
            "`foundation` 与 `foundation_supports` 必须为空。只要冻结骨架或本阶段仍存在 "
            "`blocking=true` 的用户问题，`cannot_proceed_reason` 必须为 `null`，由用户问题表达待补信息。"
            "若输入包含 `retry_context`，以上一轮 "
            "`previous_candidate` 为起点，只修正 `validation_errors` 指出的当前阶段问题，"
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


def _known_evidence_ids(model_input: dict[str, Any]) -> set[str]:
    return {
        str(card.get("evidence_id"))
        for card in model_input.get("evidence_cards") or []
        if isinstance(card, dict) and card.get("evidence_id")
    }


def _stage_evidence_reference_errors(
    model_input: dict[str, Any], candidate: dict[str, Any]
) -> list[str]:
    known = _known_evidence_ids(model_input)
    errors: list[str] = []

    def visit(node: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, (*path, str(index)))
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            child = (*path, str(key))
            if key == "evidence_ids" and isinstance(value, list):
                for index, evidence_id in enumerate(value):
                    if str(evidence_id) not in known:
                        errors.append(
                            "/" + "/".join((*child, str(index)))
                            + f": evidence_id {evidence_id!r} is not present in evidence_cards"
                        )
            else:
                visit(value, child)

    visit(candidate)
    return errors


def _skeleton_reference_errors(
    model_input: dict[str, Any], candidate: dict[str, Any]
) -> list[str]:
    errors = _stage_evidence_reference_errors(model_input, candidate)
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
        *_stage_evidence_reference_errors(model_input, candidate),
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


def _normalize_stage_mechanical_defaults(candidate: Any) -> Any:
    """Apply only unambiguous stage-boundary defaults on a deep copy.

    Provider evidence remains immutable; validation and deterministic assembly
    consume a deep copy of the candidate.  Other null-looking strings remain
    authored values. Missing fields are filled only when the surrounding
    authored values determine the sole contract-valid value.
    """
    normalized = copy.deepcopy(candidate)
    if not isinstance(normalized, dict):
        return normalized
    if (
        isinstance(normalized.get("cannot_proceed_reason"), str)
        and normalized["cannot_proceed_reason"] == "null"
    ):
        normalized["cannot_proceed_reason"] = None

    questions = normalized.get("user_questions")
    if isinstance(questions, list):
        blocking_question_exists = any(
            isinstance(item, dict) and item.get("blocking") is True
            for item in questions
        )
        if blocking_question_exists and "cannot_proceed_reason" not in normalized:
            normalized["cannot_proceed_reason"] = None
        for item in questions:
            if not isinstance(item, dict) or "allowed_values" in item:
                continue
            if str(item.get("question_type") or "") != "CHOICE":
                item["allowed_values"] = []
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


async def _invoke_validated_stage(
    *,
    stage: str,
    model_input: dict[str, Any],
    stage_gateway: ArgumentStageGateway,
    max_attempts: int,
    frozen_skeleton: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    previous_candidate: Any = None
    previous_errors: list[str] = []
    previous_phase = "structure_validation"

    for attempt in range(1, max_attempts + 1):
        retry_context = None
        if attempt > 1:
            retry_context = {
                "attempt": attempt,
                "previous_candidate": copy.deepcopy(previous_candidate),
                "validation_errors": copy.deepcopy(previous_errors),
            }
        raw_candidate = await stage_gateway.invoke_stage(
            stage,
            copy.deepcopy(model_input),
            argument_stage_output_schema(stage),
            retry_context=retry_context,
            desired_output_tokens=argument_stage_desired_output_tokens(stage),
        )
        candidate = _normalize_stage_mechanical_defaults(raw_candidate)
        if stage == ARGUMENT_DESIGN_STAGE and frozen_skeleton is not None:
            candidate = _normalize_design_mechanical_artifacts(
                candidate, frozen_skeleton
            )

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
            errors = [*shape_errors, *reference_errors, *cross_stage_errors]
            if shape_errors:
                phase = "structure_validation"
            elif cross_stage_errors:
                phase = "cross_stage_validation"
            else:
                phase = "reference_validation"
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
            errors = [*shape_errors, *reference_errors, *cross_stage_errors]
            if shape_errors:
                phase = "structure_validation"
            elif cross_stage_errors:
                phase = "cross_stage_validation"
            else:
                phase = "reference_validation"
        else:
            raise ValueError(f"Unknown Argument stage: {stage}")

        if not errors:
            return copy.deepcopy(candidate)
        previous_candidate = copy.deepcopy(candidate)
        previous_errors = list(errors)
        previous_phase = phase

    raise ArgumentStageContractError(
        stage, previous_phase, previous_errors, candidate=previous_candidate
    )


async def orchestrate_argument_architecture_two_stage(
    canonical_envelope: dict[str, Any],
    *,
    stage_gateway: ArgumentStageGateway,
    pack: Any,
    max_stage_attempts: int = 2,
    stage_input_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the two internal semantic stages and project against trusted canonical state.

    ``stage_input_envelope`` is the provider-facing business projection used to
    construct Skeleton/Design inputs and to validate stage-local references against
    exactly what the model was allowed to see. Deterministic assembly and final
    canonical projection still use ``canonical_envelope`` as authoritative state.
    """
    provider_source = stage_input_envelope if stage_input_envelope is not None else canonical_envelope
    skeleton_input = build_argument_skeleton_model_input(provider_source)
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
    design_candidate = await _invoke_validated_stage(
        stage=ARGUMENT_DESIGN_STAGE,
        model_input=design_input,
        stage_gateway=stage_gateway,
        max_attempts=max_stage_attempts,
        frozen_skeleton=frozen_skeleton,
    )

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
    }
