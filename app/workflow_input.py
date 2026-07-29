from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from .util import sha256_json


PROJECT_MATERIAL_INPUT = "PROJECT_MATERIAL_INPUT"
APPLICATION_GUIDE_INPUT = "APPLICATION_GUIDE_INPUT"
REFERENCE_TEMPLATE_INPUT = "REFERENCE_TEMPLATE_INPUT"
CURRENT_PROPOSAL_INPUT = "CURRENT_PROPOSAL_INPUT"

MATERIAL_INPUT_GATE_TYPES = {
    PROJECT_MATERIAL_INPUT,
    APPLICATION_GUIDE_INPUT,
    REFERENCE_TEMPLATE_INPUT,
    CURRENT_PROPOSAL_INPUT,
}


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


def material_input_questions(gate_type: str) -> list[dict[str, Any]]:
    labels = {
        PROJECT_MATERIAL_INPUT: (
            "请先上传项目材料，然后确认继续。",
            "至少上传一份与项目有关的真实材料。",
        ),
        APPLICATION_GUIDE_INPUT: (
            "请上传或重新标注申报指南，然后确认继续。",
            "材料角色必须为 APPLICATION_GUIDE；系统不会再把任意材料冒充申报指南。",
        ),
        REFERENCE_TEMPLATE_INPUT: (
            "请上传参考申请书，然后确认继续。",
            "材料角色必须为 REFERENCE_PROPOSAL；当前申请书不会被静默当作参考模板。",
        ),
        CURRENT_PROPOSAL_INPUT: (
            "请上传当前申请书或明确待写章节，然后确认继续。",
            "材料角色必须为 CURRENT_PROPOSAL；其他材料不会被替代为待写章节。",
        ),
    }
    prompt, detail = labels.get(
        gate_type,
        ("请补充当前步骤所需的真实输入，然后确认继续。", "不得使用 Schema 占位值代替真实输入。"),
    )
    return [
        {
            "question_id": "material-ready",
            "field_path": "material_ready",
            "prompt": prompt,
            "answer_type": "SELECT",
            "required": True,
            "default": "READY",
            "options": ["READY"],
            "help": detail,
        }
    ]


def _answer_map(answers: list[dict[str, Any]] | None) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for item in answers or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("question_id") or item.get("field_path") or item.get("id") or "").strip()
        if not key:
            continue
        mapped[key] = item.get("value", item.get("answer"))
    return mapped


def _coerce_answer(value: Any, question: dict[str, Any]) -> Any:
    schema = question.get("answer_schema") if isinstance(question.get("answer_schema"), dict) else {}
    answer_type = str(schema.get("type") or question.get("answer_type") or "STRING").upper()
    if answer_type in {"STRING", "LONG_TEXT", "SELECT", "ENUM"}:
        normalized = str(value or "").strip()
        allowed = schema.get("allowed_values")
        if not isinstance(allowed, list):
            allowed = question.get("options") if isinstance(question.get("options"), list) else None
        if answer_type in {"SELECT", "ENUM"} and allowed is not None and normalized not in {str(item) for item in allowed}:
            raise ValueError(f"回答不在允许范围内：{value!r}")
        return normalized
    if answer_type == "BOOLEAN":
        if isinstance(value, bool):
            return value
        normalized = str(value or "").strip().lower()
        if normalized in {"true", "1", "yes", "y", "是", "确认"}:
            return True
        if normalized in {"false", "0", "no", "n", "否", "不确认"}:
            return False
        raise ValueError(f"无法将回答转换为布尔值：{value!r}")
    if answer_type == "NUMBER":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        text = str(value or "").strip()
        try:
            return float(text) if "." in text else int(text)
        except ValueError as exc:
            raise ValueError(f"无法将回答转换为数值：{value!r}") from exc
    if answer_type in {"OBJECT", "ARRAY"}:
        if isinstance(value, (dict, list)):
            parsed = copy.deepcopy(value)
        else:
            try:
                parsed = json.loads(str(value or ""))
            except json.JSONDecodeError as exc:
                raise ValueError(f"回答必须为合法 JSON：{value!r}") from exc
        expected = dict if answer_type == "OBJECT" else list
        if not isinstance(parsed, expected):
            raise ValueError(f"回答类型必须为 {answer_type}")
        return parsed
    return copy.deepcopy(value)


def build_human_resolutions(
    *,
    gate_id: str,
    prompt_id: str,
    questions: list[dict[str, Any]],
    answers: list[dict[str, Any]] | None,
    decided_by: str,
    decided_role: str,
) -> list[dict[str, Any]]:
    values = _answer_map(answers)
    resolutions: list[dict[str, Any]] = []
    for index, raw_question in enumerate(questions):
        if isinstance(raw_question, str):
            question = {
                "question_id": f"question-{index}",
                "question": raw_question,
                "target_paths": [],
                "answer_schema": {"type": "STRING"},
            }
        elif isinstance(raw_question, dict):
            question = raw_question
        else:
            continue
        question_id = str(question.get("question_id") or question.get("id") or f"question-{index}").strip()
        field_path = str(question.get("field_path") or "").strip()
        value = values.get(question_id, values.get(field_path))
        required = bool(question.get("required") or question.get("blocking"))
        if value is None and required:
            raise ValueError(f"必须回答：{question.get('prompt') or question.get('question') or question_id}")
        if value is None:
            continue
        coerced = _coerce_answer(value, question)
        if required and isinstance(coerced, str) and not coerced.strip():
            raise ValueError(f"必须回答：{question.get('prompt') or question.get('question') or question_id}")
        target_paths = [
            str(item).strip()
            for item in (question.get("target_paths") or ([field_path] if field_path else []))
            if str(item).strip()
        ]
        resolution = {
            "resolution_id": "human-" + sha256_json(
                {
                    "gate_id": gate_id,
                    "question_id": question_id,
                    "answer": coerced,
                }
            )[:20],
            "gate_id": gate_id,
            "prompt_id": prompt_id,
            "question_id": question_id,
            "question": str(question.get("prompt") or question.get("question") or question_id),
            "target_paths": target_paths,
            "answer": coerced,
            "decided_by": decided_by,
            "decided_role": decided_role,
        }
        resolutions.append(resolution)
    return resolutions


def resolution_overrides(resolutions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return only unambiguous leaf-path overrides.

    Human answers are always preserved in ``payload.human_resolutions``.  A value
    is additionally written into the live input only when the question identifies
    exactly one concrete schema path.  Root paths such as ``payload`` are not
    guessed or expanded.
    """
    overrides: dict[str, Any] = {}
    for resolution in resolutions:
        paths = [str(item).strip() for item in resolution.get("target_paths") or [] if str(item).strip()]
        if len(paths) != 1:
            continue
        path = paths[0]
        if path in {"payload", "scope", "task", "security_context"}:
            continue
        if not path.startswith(("payload.", "scope.", "task.")):
            continue
        overrides[path] = copy.deepcopy(resolution.get("answer"))
    return overrides
