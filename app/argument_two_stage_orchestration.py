from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Protocol

from .model_semantic_contracts import (
    argument_design_model_output_errors,
    argument_design_model_reference_errors,
    argument_skeleton_model_output_errors,
    assemble_argument_authored_state,
    build_argument_design_model_input,
    build_argument_skeleton_model_input,
    expand_argument_architecture_model_output,
    semantic_model_reference_errors,
)


ARGUMENT_SKELETON_STAGE = "SKELETON"
ARGUMENT_DESIGN_STAGE = "DESIGN"
ARGUMENT_TWO_STAGE_CONTRACT_VERSION = "ARGUMENT_TWO_STAGE_V2"

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
            "用户问题或不能继续的原因。若输入包含 `retry_context`，以上一轮 "
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
    thread_count = len(candidate.get("research_threads") or [])
    for index, gap in enumerate(candidate.get("evidence_gaps") or []):
        if not isinstance(gap, dict):
            continue
        thread_index = gap.get("thread_index")
        if thread_index is not None and not (0 <= int(thread_index) < thread_count):
            errors.append(f"/evidence_gaps/{index}/thread_index: out of range")
    return errors


def _design_reference_errors(
    model_input: dict[str, Any],
    candidate: dict[str, Any],
    frozen_skeleton: dict[str, Any],
) -> list[str]:
    return [
        *argument_design_model_reference_errors(candidate, frozen_skeleton),
        *_stage_evidence_reference_errors(model_input, candidate),
    ]


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
                "validation_errors": copy.deepcopy(previous_errors[:6]),
            }
        candidate = await stage_gateway.invoke_stage(
            stage,
            copy.deepcopy(model_input),
            argument_stage_output_schema(stage),
            retry_context=retry_context,
            desired_output_tokens=argument_stage_desired_output_tokens(stage),
        )

        if stage == ARGUMENT_SKELETON_STAGE:
            shape_errors = argument_skeleton_model_output_errors(candidate)
            if shape_errors:
                phase = "structure_validation"
                errors = shape_errors
            else:
                phase = "reference_validation"
                errors = _skeleton_reference_errors(model_input, candidate)
        elif stage == ARGUMENT_DESIGN_STAGE:
            shape_errors = argument_design_model_output_errors(candidate)
            if shape_errors:
                phase = "structure_validation"
                errors = shape_errors
            else:
                if frozen_skeleton is None:
                    raise ValueError("Design stage requires frozen_skeleton")
                phase = "reference_validation"
                errors = _design_reference_errors(model_input, candidate, frozen_skeleton)
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

