from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .util import sha256_json


WF3_INPUT_GATE_TYPE = "PUBLIC_RESEARCH_NEED_INPUT"
VALID_TARGET_TASK_TYPES = {
    "PUBLIC_RESEARCH",
    "PUBLIC_TEMPLATE_ANALYSIS",
    "GENERIC_LANGUAGE_ASSIST",
}
DEFAULT_REASON_ONLINE_NEEDED = (
    "需要检索可公开访问且可核验的资料，以补足内部材料无法独立确认的相关工作、比较基线或评价依据。"
)
DEFAULT_DESIRED_OUTPUT = (
    "返回带来源绑定的代表性公开资料、方法或基线、评价指标、适用边界及可用于申请书论证的结论摘要。"
)


@dataclass(frozen=True)
class WF3InputResolution:
    research_need: dict[str, Any] | None
    source_items: list[dict[str, Any]]
    target_task_type: str
    origin: str
    missing_paths: tuple[str, ...] = ()


class WorkflowInputRequired(ValueError):
    def __init__(
        self,
        prompt_id: str,
        *,
        gate_type: str,
        missing_paths: list[str],
        questions: list[dict[str, Any]],
        message: str,
    ):
        self.prompt_id = prompt_id
        self.gate_type = gate_type
        self.missing_paths = list(missing_paths)
        self.questions = copy.deepcopy(questions)
        super().__init__(message)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _nested_options(options: dict[str, Any] | None) -> dict[str, Any]:
    source = copy.deepcopy(options or {})
    nested = source.get("wf3")
    if isinstance(nested, dict):
        merged = copy.deepcopy(source)
        merged.update(nested)
        return merged
    return source


def normalize_target_task_type(value: Any) -> str:
    normalized = _clean_text(value).upper() or "PUBLIC_RESEARCH"
    if normalized not in VALID_TARGET_TASK_TYPES:
        raise ValueError(
            "WF-3 target_task_type 必须为 " + "、".join(sorted(VALID_TARGET_TASK_TYPES))
        )
    return normalized


def explicit_research_need(options: dict[str, Any] | None) -> dict[str, Any] | None:
    source = _nested_options(options)
    nested = source.get("research_need")
    need = copy.deepcopy(nested) if isinstance(nested, dict) else {}
    aliases = {
        "question": ("research_question", "question", "public_research_question"),
        "reason_online_needed": ("reason_online_needed", "online_reason"),
        "desired_output": ("desired_output", "research_desired_output"),
        "need_id": ("need_id",),
    }
    for field, keys in aliases.items():
        if _clean_text(need.get(field)):
            continue
        for key in keys:
            if _clean_text(source.get(key)):
                need[field] = source[key]
                break
    question = _clean_text(need.get("question"))
    if not question:
        return None
    reason = _clean_text(need.get("reason_online_needed")) or DEFAULT_REASON_ONLINE_NEEDED
    desired = _clean_text(need.get("desired_output")) or DEFAULT_DESIRED_OUTPUT
    need_id = _clean_text(need.get("need_id"))
    return {
        "need_id": need_id,
        "question": question,
        "reason_online_needed": reason,
        "desired_output": desired,
    }


def derive_research_question(
    *,
    project: dict[str, Any],
    config: dict[str, Any],
    argument_graph: dict[str, Any] | None,
) -> tuple[str | None, str]:
    configured = _clean_text(
        config.get("public_research_question")
        or config.get("research_question")
    )
    if configured:
        return configured, "PROJECT_CONFIG"

    graph = argument_graph if isinstance(argument_graph, dict) else {}
    research_questions = [
        _clean_text(item.get("statement"))
        for item in graph.get("research_questions") or []
        if isinstance(item, dict) and _clean_text(item.get("statement"))
    ]
    nodes = [item for item in graph.get("nodes") or [] if isinstance(item, dict)]
    gaps = [
        _clean_text(item.get("statement"))
        for item in nodes
        if item.get("node_type") == "RESEARCH_GAP" and _clean_text(item.get("statement"))
    ]
    closest = [
        _clean_text(item.get("statement"))
        for item in nodes
        if item.get("node_type") == "CLOSEST_PRIOR_WORK" and _clean_text(item.get("statement"))
    ]
    if research_questions:
        focus = "；".join(research_questions[:2])
        question = (
            "请检索并比较与以下研究问题相关的公开研究、代表性方法、比较基线和评价指标："
            + focus
        )
        if gaps:
            question += "。重点核验的研究差距为：" + gaps[0]
        return question, "ARGUMENT_GRAPH_RESEARCH_QUESTIONS"
    if gaps or closest:
        focus = gaps[0] if gaps else closest[0]
        return (
            "请检索与以下问题最接近的公开研究、代表性方法、比较基线、评价指标及其适用边界："
            + focus,
            "ARGUMENT_GRAPH_GAP",
        )

    # A plain project description is not automatically treated as a research
    # question. Doing so would make an empty or administrative description look
    # like an approved outbound research task. The user gate is safer here.
    return None, "UNRESOLVED"


def build_research_need(
    *,
    project_id: str,
    options: dict[str, Any] | None,
    project: dict[str, Any],
    config: dict[str, Any],
    argument_graph: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    explicit = explicit_research_need(options)
    if explicit:
        origin = "WORKFLOW_OPTIONS"
        need = explicit
    else:
        question, origin = derive_research_question(
            project=project,
            config=config,
            argument_graph=argument_graph,
        )
        if not question:
            return None, origin
        need = {
            "need_id": "",
            "question": question,
            "reason_online_needed": DEFAULT_REASON_ONLINE_NEEDED,
            "desired_output": DEFAULT_DESIRED_OUTPUT,
        }
    if not _clean_text(need.get("need_id")):
        need["need_id"] = "need-" + sha256_json(
            {
                "project_id": project_id,
                "question": need["question"],
                "target_task_type": normalize_target_task_type(
                    _nested_options(options).get("target_task_type")
                ),
            }
        )[:20]
    return need, origin


def input_gate_questions() -> list[dict[str, Any]]:
    return [
        {
            "question_id": "wf3-research-question",
            "field_path": "research_need.question",
            "prompt": "需要联网检索并核验的公开问题是什么？",
            "answer_type": "LONG_TEXT",
            "required": True,
            "placeholder": "例如：检索与某研究问题最接近的公开工作、代表性基线和评价指标。",
        },
        {
            "question_id": "wf3-reason-online-needed",
            "field_path": "research_need.reason_online_needed",
            "prompt": "为什么必须使用公开联网资料？",
            "answer_type": "LONG_TEXT",
            "required": False,
            "default": DEFAULT_REASON_ONLINE_NEEDED,
        },
        {
            "question_id": "wf3-desired-output",
            "field_path": "research_need.desired_output",
            "prompt": "期望联网研究返回什么结果？",
            "answer_type": "LONG_TEXT",
            "required": False,
            "default": DEFAULT_DESIRED_OUTPUT,
        },
        {
            "question_id": "wf3-target-task-type",
            "field_path": "target_task_type",
            "prompt": "在线任务类型",
            "answer_type": "SELECT",
            "required": True,
            "default": "PUBLIC_RESEARCH",
            "options": [
                "PUBLIC_RESEARCH",
                "PUBLIC_TEMPLATE_ANALYSIS",
                "GENERIC_LANGUAGE_ASSIST",
            ],
        },
    ]


def _answer_map(answers: list[dict[str, Any]] | None) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for item in answers or []:
        if not isinstance(item, dict):
            continue
        key = _clean_text(item.get("question_id") or item.get("field_path") or item.get("id"))
        if not key:
            continue
        value = item.get("value", item.get("answer"))
        mapped[key] = value
    return mapped


def options_from_gate_answers(
    *,
    current_options: dict[str, Any] | None,
    questions: list[dict[str, Any]],
    answers: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    values = _answer_map(answers)
    by_id = {
        _clean_text(item.get("question_id")): item
        for item in questions
        if isinstance(item, dict) and _clean_text(item.get("question_id"))
    }

    def value_for(question_id: str) -> Any:
        question = by_id.get(question_id) or {}
        return values.get(
            question_id,
            values.get(_clean_text(question.get("field_path")), question.get("default")),
        )

    question = _clean_text(value_for("wf3-research-question"))
    if not question:
        raise ValueError("必须填写需要联网检索并核验的公开研究问题")
    reason = _clean_text(value_for("wf3-reason-online-needed")) or DEFAULT_REASON_ONLINE_NEEDED
    desired = _clean_text(value_for("wf3-desired-output")) or DEFAULT_DESIRED_OUTPUT
    target_task_type = normalize_target_task_type(value_for("wf3-target-task-type"))

    options = copy.deepcopy(current_options or {})
    previous_need = options.get("research_need") if isinstance(options.get("research_need"), dict) else {}
    options["research_need"] = {
        "need_id": _clean_text(previous_need.get("need_id")),
        "question": question,
        "reason_online_needed": reason,
        "desired_output": desired,
    }
    options["target_task_type"] = target_task_type
    return options
