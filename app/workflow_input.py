from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .json_pointer import JsonPointerError, format_pointer, parse_pointer
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


def human_resolution_scope_key(
    prompt_id: str,
    *,
    section_id: str | None = None,
    workflow_step: int | str | None = None,
) -> str:
    """Return the immutable visibility scope for one human resolution.

    Authoring prompts are reused across many proposal sections.  Indexing a
    resolution by ``prompt_id`` alone therefore lets an answer from one
    section leak into another.  Section identity takes precedence; non-section
    prompts are isolated by workflow step.  The raw prompt id is deliberately
    not returned for new writes and remains a read-only migration format.
    """

    normalized_prompt = str(prompt_id or "").strip()
    if not normalized_prompt:
        raise ValueError("human resolution prompt_id cannot be empty")
    normalized_section = str(section_id or "").strip()
    if normalized_section:
        return f"section:{normalized_section}:{normalized_prompt}"
    if workflow_step is None or str(workflow_step).strip() == "":
        raise ValueError("human resolution workflow_step cannot be empty")
    return f"step:{workflow_step}:{normalized_prompt}"


def section_id_for_run(state: dict[str, Any], run_id: str | None) -> str | None:
    """Infer the owning section only from persisted workflow checkpoints."""

    target = str(run_id or "").strip()
    if not target:
        return None
    gate = state.get("section_input_gate")
    if isinstance(gate, dict) and str(gate.get("run_id") or "") == target:
        section_id = str(gate.get("section_id") or "").strip()
        if section_id:
            return section_id

    for section_id, progress in (state.get("section_progress") or {}).items():
        if not isinstance(progress, dict):
            continue
        for item in progress.get("runs") or []:
            if isinstance(item, dict) and str(item.get("run_id") or "") == target:
                return str(section_id)

    for section in state.get("section_results") or []:
        if not isinstance(section, dict):
            continue
        for item in section.get("runs") or []:
            if isinstance(item, dict) and str(item.get("run_id") or "") == target:
                section_id = str(section.get("section_id") or "").strip()
                if section_id:
                    return section_id
    return None


_POINTER_ROOTS = {"payload", "scope", "task", "security_context"}
_LEGACY_PATH_TOKEN = re.compile(r"[^.\[\]]+|\[(0|[1-9][0-9]*)\]")


def canonical_target_pointer(path: object, *, default_root: str = "payload") -> str:
    """Return one canonical RFC 6901 target pointer.

    Human-question definitions historically used dotted paths while prompt
    schemas require JSON Pointer.  The conversion is deliberately syntax-only:
    it never guesses aliases or creates missing schema members.
    """

    raw = str(path or "").strip()
    if not raw:
        raise ValueError("human resolution target path cannot be empty")
    if raw.startswith("/"):
        try:
            return format_pointer(parse_pointer(raw))
        except JsonPointerError as exc:
            raise ValueError(f"invalid human resolution JSON Pointer: {raw!r}") from exc

    tokens: list[str] = []
    cursor = 0
    for match in _LEGACY_PATH_TOKEN.finditer(raw):
        if match.start() != cursor:
            separator = raw[cursor:match.start()]
            if separator != ".":
                raise ValueError(f"unsupported legacy target path syntax: {raw!r}")
        token = match.group(0)
        tokens.append(token[1:-1] if token.startswith("[") else token)
        cursor = match.end()
    if cursor != len(raw) or not tokens:
        raise ValueError(f"unsupported legacy target path syntax: {raw!r}")
    if tokens[0] not in _POINTER_ROOTS:
        if default_root not in _POINTER_ROOTS:
            raise ValueError(f"unsupported default target root: {default_root!r}")
        tokens.insert(0, default_root)
    return format_pointer(tokens)


def canonicalize_human_resolution(resolution: dict[str, Any]) -> dict[str, Any]:
    """Return a copy whose target paths use the canonical pointer contract."""

    normalized = copy.deepcopy(resolution)
    pointers: list[str] = []
    for item in normalized.get("target_paths") or []:
        pointer = canonical_target_pointer(item)
        if pointer not in pointers:
            pointers.append(pointer)
    normalized["target_paths"] = pointers
    return normalized


def _runtime_dotted_path(pointer: str) -> str | None:
    """Translate an authorized object-member pointer for the legacy setter.

    Array members and keys containing dots are intentionally not translated,
    because ``_set_path_if_valid`` only supports unambiguous object paths.
    """

    try:
        tokens = parse_pointer(pointer)
    except JsonPointerError:
        return None
    if not tokens or tokens[0] not in {"payload", "scope", "task"}:
        return None
    if any(not token or "." in token or token.isdigit() for token in tokens):
        return None
    return ".".join(tokens)


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
    for index, item in enumerate(answers or []):
        if not isinstance(item, dict):
            raise ValueError(f"第{index + 1}个回答必须是对象。")
        key = str(
            item.get("question_id")
            or item.get("field_path")
            or item.get("id")
            or ""
        ).strip()
        if not key:
            raise ValueError(f"第{index + 1}个回答缺少 question_id 或 field_path。")
        if key in mapped:
            raise ValueError(f"同一问题不能提交多个回答：{key}")
        mapped[key] = item.get("value", item.get("answer"))
    return mapped


_SUPPORTED_ANSWER_TYPES = {
    "STRING",
    "LONG_TEXT",
    "SELECT",
    "ENUM",
    "BOOLEAN",
    "NUMBER",
    "OBJECT",
    "ARRAY",
}


def _is_blank_answer(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _parse_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
    normalized = str(value or "").strip().casefold()
    if normalized in {"true", "1", "yes", "y", "是", "确认"}:
        return True
    if normalized in {"false", "0", "no", "n", "否", "不确认"}:
        return False
    raise ValueError(f"无法将回答转换为布尔值：{value!r}")


def _parse_number(value: Any) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"无法将回答转换为数值：{value!r}")
    if isinstance(value, (int, float)):
        parsed = value
    else:
        text = str(value or "").strip()
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"无法将回答转换为数值：{value!r}") from exc
        if not isinstance(parsed, (int, float)) or isinstance(parsed, bool):
            raise ValueError(f"无法将回答转换为数值：{value!r}")
    if isinstance(parsed, float) and not math.isfinite(parsed):
        raise ValueError(f"数值回答必须是有限值：{value!r}")
    return parsed


def _enum_allowed_values(question: dict[str, Any], schema: dict[str, Any]) -> list[Any]:
    allowed = schema.get("allowed_values")
    if not isinstance(allowed, list):
        allowed = question.get("options") if isinstance(question.get("options"), list) else None
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("ENUM 回答必须提供非空 allowed_values")
    return allowed


def _coerce_enum_answer(value: Any, allowed: list[Any]) -> Any:
    exact_matches = [
        item
        for item in allowed
        if type(item) is type(value) and item == value
    ]
    if exact_matches:
        return copy.deepcopy(exact_matches[0])

    # Browser form controls always submit strings.  First recover the type of
    # scalar enum members, then retain the old textual fallback for legacy API
    # clients.  Type-sensitive exact matching above keeps [1, "1"] unambiguous.
    if isinstance(value, str):
        recovered_matches: list[Any] = []
        bool_items = [item for item in allowed if isinstance(item, bool)]
        if bool_items:
            try:
                parsed_bool = _parse_boolean(value)
            except ValueError:
                pass
            else:
                recovered_matches.extend(
                    item for item in bool_items if item is parsed_bool
                )

        number_items = [
            item
            for item in allowed
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        ]
        if number_items:
            try:
                parsed_number = _parse_number(value)
            except ValueError:
                pass
            else:
                matches = [
                    item
                    for item in number_items
                    if type(item) is type(parsed_number) and item == parsed_number
                ]
                if not matches:
                    matches = [item for item in number_items if item == parsed_number]
                recovered_matches.extend(matches)
        if len(recovered_matches) == 1:
            return copy.deepcopy(recovered_matches[0])
        if len(recovered_matches) > 1:
            raise ValueError(f"回答不在允许范围内或存在歧义：{value!r}")

    normalized = "" if value is None else str(value).strip()
    textual_matches = [
        item
        for item in allowed
        if (
            ("true" if item is True else "false" if item is False else str(item))
            == normalized
        )
    ]
    if len(textual_matches) != 1:
        raise ValueError(f"回答不在允许范围内或存在歧义：{value!r}")
    return copy.deepcopy(textual_matches[0])


def _answer_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate the Gate answer dialect into ordinary JSON Schema."""

    translated = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        raw_type = node.get("type")
        if isinstance(raw_type, str) and raw_type.upper() in {
            "STRING", "NUMBER", "BOOLEAN", "OBJECT", "ARRAY", "INTEGER", "NULL"
        }:
            node["type"] = raw_type.lower()
        allowed = node.pop("allowed_values", None)
        if isinstance(allowed, list) and allowed:
            node["enum"] = copy.deepcopy(allowed)
        properties = node.get("properties")
        if isinstance(properties, dict):
            for child in properties.values():
                visit(child)
        items = node.get("items")
        if isinstance(items, dict):
            visit(items)
        for keyword in ("allOf", "anyOf", "oneOf"):
            children = node.get(keyword)
            if isinstance(children, list):
                for child in children:
                    visit(child)

    visit(translated)
    return translated


def _validate_composite_answer(value: Any, schema: dict[str, Any]) -> None:
    json_schema = _answer_json_schema(schema)
    try:
        validator = Draft202012Validator(json_schema)
        validator.check_schema(json_schema)
    except SchemaError as exc:
        raise ValueError(f"Gate answer_schema 无效：{exc.message}") from exc
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    if errors:
        first = errors[0]
        location = "/" + "/".join(str(item) for item in first.absolute_path)
        if location == "/":
            location = "根值"
        raise ValueError(f"回答不符合 answer_schema（{location}）：{first.message}")


def validate_gate_questions(questions: list[Any]) -> None:
    """Reject Gate definitions that cannot be rendered or answered safely."""

    seen_ids: set[str] = set()
    seen_aliases: set[str] = set()
    for index, raw_question in enumerate(questions):
        if isinstance(raw_question, str):
            if not raw_question.strip():
                raise ValueError(f"Gate 第 {index + 1} 个问题不能为空")
            continue
        if not isinstance(raw_question, dict):
            raise ValueError(f"Gate 第 {index + 1} 个问题必须是字符串或对象")

        question_id = str(
            raw_question.get("question_id")
            or raw_question.get("id")
            or f"question-{index}"
        ).strip()
        if not question_id:
            raise ValueError(f"Gate 第 {index + 1} 个问题缺少 question_id")
        if question_id in seen_ids:
            raise ValueError(f"Gate 问题 question_id 重复：{question_id}")
        seen_ids.add(question_id)

        field_path = str(raw_question.get("field_path") or "").strip()
        for alias in dict.fromkeys([question_id] + ([field_path] if field_path else [])):
            if alias in seen_aliases:
                raise ValueError(f"Gate 问题回答标识重复：{alias}")
            seen_aliases.add(alias)

        raw_schema = raw_question.get("answer_schema")
        if raw_schema is not None and not isinstance(raw_schema, dict):
            raise ValueError(f"Gate 问题 answer_schema 必须是对象：{question_id}")
        schema = raw_schema if isinstance(raw_schema, dict) else {}
        answer_type = str(
            schema.get("type") or raw_question.get("answer_type") or "STRING"
        ).upper()
        if answer_type not in _SUPPORTED_ANSWER_TYPES:
            raise ValueError(f"不支持的 Gate 回答类型：{answer_type}")
        if answer_type in {"SELECT", "ENUM"}:
            allowed = _enum_allowed_values(raw_question, schema)
            for item in allowed:
                if (
                    not isinstance(item, (str, int, float, bool))
                    or item is None
                    or (isinstance(item, float) and not math.isfinite(item))
                ):
                    raise ValueError(
                        f"Gate ENUM allowed_values 只能包含字符串、有限数值或布尔值：{item!r}"
                    )
            for position, item in enumerate(allowed):
                for previous in allowed[:position]:
                    same_typed_value = type(item) is type(previous) and item == previous
                    same_json_number = (
                        isinstance(item, (int, float))
                        and not isinstance(item, bool)
                        and isinstance(previous, (int, float))
                        and not isinstance(previous, bool)
                        and item == previous
                    )
                    if same_typed_value or same_json_number:
                        raise ValueError(f"Gate ENUM allowed_values 存在重复值：{item!r}")
        elif answer_type == "OBJECT":
            if not isinstance(schema.get("properties"), dict):
                raise ValueError("OBJECT 回答必须定义 properties schema")
            try:
                Draft202012Validator.check_schema(_answer_json_schema(schema))
            except SchemaError as exc:
                raise ValueError(f"Gate answer_schema 无效：{exc.message}") from exc
        elif answer_type == "ARRAY":
            if not isinstance(schema.get("items"), dict):
                raise ValueError("ARRAY 回答必须定义 items schema")
            try:
                Draft202012Validator.check_schema(_answer_json_schema(schema))
            except SchemaError as exc:
                raise ValueError(f"Gate answer_schema 无效：{exc.message}") from exc

        if "default" in raw_question and not _is_blank_answer(raw_question.get("default")):
            _coerce_answer(raw_question["default"], raw_question)
        raw_targets = raw_question.get("target_paths")
        if raw_targets is not None and not isinstance(raw_targets, list):
            raise ValueError(f"Gate 问题 target_paths 必须是数组：{question_id}")
        targets = raw_targets or ([field_path] if field_path else [])
        for target in targets:
            canonical_target_pointer(target)


def _coerce_answer(value: Any, question: dict[str, Any]) -> Any:
    schema = question.get("answer_schema") if isinstance(question.get("answer_schema"), dict) else {}
    answer_type = str(schema.get("type") or question.get("answer_type") or "STRING").upper()
    if answer_type not in _SUPPORTED_ANSWER_TYPES:
        raise ValueError(f"不支持的 Gate 回答类型：{answer_type}")
    if answer_type in {"STRING", "LONG_TEXT"}:
        if not isinstance(value, str):
            raise ValueError(f"回答类型必须为 {answer_type}")
        return value.strip()
    if answer_type in {"SELECT", "ENUM"}:
        return _coerce_enum_answer(value, _enum_allowed_values(question, schema))
    if answer_type == "BOOLEAN":
        return _parse_boolean(value)
    if answer_type == "NUMBER":
        return _parse_number(value)
    if answer_type in {"OBJECT", "ARRAY"}:
        if answer_type == "OBJECT" and not isinstance(schema.get("properties"), dict):
            raise ValueError("OBJECT 回答必须定义 properties schema")
        if answer_type == "ARRAY" and not isinstance(schema.get("items"), dict):
            raise ValueError("ARRAY 回答必须定义 items schema")
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
        _validate_composite_answer(parsed, schema)
        return parsed
    raise ValueError(f"不支持的 Gate 回答类型：{answer_type}")


def build_human_resolutions(
    *,
    gate_id: str,
    prompt_id: str,
    questions: list[dict[str, Any]],
    answers: list[dict[str, Any]] | None,
    decided_by: str,
    decided_role: str,
    require_any_answer: bool = False,
) -> list[dict[str, Any]]:
    values = _answer_map(answers)
    resolutions: list[dict[str, Any]] = []
    recognized_answer_keys: set[str] = set()
    seen_question_ids: set[str] = set()
    seen_answer_aliases: set[str] = set()
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
        question_id = str(
            question.get("question_id")
            or question.get("id")
            or f"question-{index}"
        ).strip()
        if question_id in seen_question_ids:
            raise ValueError(f"Gate 问题 question_id 重复：{question_id}")
        seen_question_ids.add(question_id)
        field_path = str(question.get("field_path") or "").strip()
        aliases = [question_id] + ([field_path] if field_path else [])
        for alias in dict.fromkeys(aliases):
            if alias in seen_answer_aliases:
                raise ValueError(f"Gate 问题回答标识重复：{alias}")
            seen_answer_aliases.add(alias)
            recognized_answer_keys.add(alias)
        provided_aliases = [alias for alias in dict.fromkeys(aliases) if alias in values]
        if len(provided_aliases) > 1:
            raise ValueError(
                f"同一问题不能同时通过 question_id 和 field_path 提交回答：{question_id}"
            )
        value = values.get(question_id, values.get(field_path))
        required = bool(question.get("required") or question.get("blocking"))
        if _is_blank_answer(value) and required:
            raise ValueError(f"必须回答：{question.get('prompt') or question.get('question') or question_id}")
        if _is_blank_answer(value):
            continue
        coerced = _coerce_answer(value, question)
        if required and isinstance(coerced, str) and not coerced.strip():
            raise ValueError(f"必须回答：{question.get('prompt') or question.get('question') or question_id}")
        target_paths = []
        for item in (question.get("target_paths") or ([field_path] if field_path else [])):
            if not str(item).strip():
                continue
            pointer = canonical_target_pointer(item)
            if pointer not in target_paths:
                target_paths.append(pointer)
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
    unknown_answer_keys = sorted(set(values) - recognized_answer_keys)
    if unknown_answer_keys:
        raise ValueError(
            "回答不属于当前 Gate 的问题列表：" + "、".join(unknown_answer_keys)
        )
    if require_any_answer:
        substantive = any(
            not (
                resolution.get("answer") is None
                or (
                    isinstance(resolution.get("answer"), str)
                    and not resolution.get("answer").strip()
                )
                or (
                    isinstance(resolution.get("answer"), (list, dict))
                    and not resolution.get("answer")
                )
            )
            for resolution in resolutions
        )
        if not substantive:
            raise ValueError("当前 NEED_USER_INPUT 检查点必须至少提交一个有效回答。")
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
        raw_paths = [item for item in resolution.get("target_paths") or [] if str(item).strip()]
        if len(raw_paths) != 1:
            continue
        try:
            pointer = canonical_target_pointer(raw_paths[0])
        except ValueError:
            continue
        path = _runtime_dotted_path(pointer)
        if path is None:
            continue
        overrides[path] = copy.deepcopy(resolution.get("answer"))
    return overrides
