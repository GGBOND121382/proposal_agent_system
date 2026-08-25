from __future__ import annotations

import copy
import re
from typing import Any


_BOOLEAN_BRANCH_PATTERN = re.compile(
    r"(?:如果|若(?!干)|假如|倘若|否则|不然|如(?:未|不|无|有|需|能|可)|"
    r"\bif\b|\bunless\b|\botherwise\b)",
    re.IGNORECASE,
)
_ALTERNATIVE_PATTERN = re.compile(
    r"(?:还是|或者|或是|\bor\b|\bversus\b|\bvs\.?\b)",
    re.IGNORECASE,
)
_BOOLEAN_INTERROGATIVE_PATTERN = re.compile(
    r"(?:是否|能否|可否|要不要|有没有|是不是|"
    r"\b(?:should|do|does|did|can|could|would|will|is|are)\b)",
    re.IGNORECASE,
)
_OPEN_ANSWER_PATTERN = re.compile(
    r"(?:是什么|有哪些|哪些|哪几|如何|多少|"
    r"请(?:提供|说明|列出|给出|填写|描述|补充)|"
    r"\b(?:what|which|how|how many|how much)\b|"
    r"\bplease\s+(?:provide|describe|list|specify|explain|enter)\b)",
    re.IGNORECASE,
)
_MULTI_VALUE_PATTERN = re.compile(
    r"(?:哪些|哪几|分别|各自|逐项|多选|可同时|同时选择|分组|归类|分类|"
    r"\bselect\s+all\b|\bwhich\s+(?:ones|items|options)\b)",
    re.IGNORECASE,
)
_MULTI_CLAUSE_PATTERN = re.compile(
    r"(?:[，,；;]\s*(?:并|同时|且)|[?？]\s*(?:并|同时|且)|\b,?\s+and\s+)",
    re.IGNORECASE,
)


def boolean_question_requires_text(question: Any) -> bool:
    """Return true when a Boolean cannot carry the complete human answer."""

    text = " ".join(str(question or "").split())
    if not text:
        return True
    if len(re.findall(r"[?？]", text)) > 1:
        return True
    if len(_BOOLEAN_INTERROGATIVE_PATTERN.findall(text)) > 1:
        return True
    return bool(
        _BOOLEAN_BRANCH_PATTERN.search(text)
        or _ALTERNATIVE_PATTERN.search(text)
        or _OPEN_ANSWER_PATTERN.search(text)
        or _MULTI_CLAUSE_PATTERN.search(text)
    )


def enum_question_requires_text(question: Any) -> bool:
    """Return true when one enum value cannot carry the requested answer."""

    text = " ".join(str(question or "").split())
    if not text:
        return True
    return bool(
        len(re.findall(r"[?？]", text)) > 1
        or _BOOLEAN_BRANCH_PATTERN.search(text)
        or _OPEN_ANSWER_PATTERN.search(text)
        or _MULTI_VALUE_PATTERN.search(text)
        or _MULTI_CLAUSE_PATTERN.search(text)
    )


def semantic_question_answer_schema(question: dict[str, Any]) -> dict[str, Any]:
    """Project a semantic question without narrowing its authored answer shape.

    Semantic Argument contracts do not describe OBJECT properties or ARRAY
    items. Those shapes therefore become free text instead of an invalid JSON
    editor or a narrower Boolean/single-select control.
    """

    question_type = str(question.get("question_type") or "").upper()
    answer_shape = str(question.get("answer_shape") or "").upper()
    allowed_values = list(question.get("allowed_values") or [])
    question_text = question.get("question")

    if answer_shape in {"OBJECT", "ARRAY"}:
        return {"type": "STRING"}
    if answer_shape == "STRING" and question_type != "CHOICE":
        return {"type": "STRING"}
    if answer_shape == "NUMBER" and question_type != "CHOICE":
        return {"type": "NUMBER", "allowed_values": allowed_values}

    boolean_values = (
        len(allowed_values) == 2
        and all(isinstance(item, bool) for item in allowed_values)
        and set(allowed_values) == {True, False}
    )
    boolean_intent = answer_shape == "BOOLEAN" or (
        not answer_shape and question_type == "CONFIRMATION"
    ) or (question_type == "CHOICE" and boolean_values)
    if boolean_intent:
        if boolean_question_requires_text(question_text):
            return {"type": "STRING"}
        return {"type": "BOOLEAN", "allowed_values": []}

    if question_type == "CHOICE":
        if not allowed_values or enum_question_requires_text(question_text):
            return {"type": "STRING"}
        return {"type": "ENUM", "allowed_values": allowed_values}

    if answer_shape in {"BOOLEAN", "NUMBER", "STRING"}:
        return {"type": answer_shape, "allowed_values": allowed_values}
    return {"type": "STRING"}


def widen_gate_question_answer_schema(question: Any) -> Any:
    """Return a Gate question whose control can express the requested answer.

    This function is monotonic: it preserves valid structured controls and only
    widens BOOLEAN or single-value ENUM controls to STRING when the question
    requires explanation, multiple values, grouping, or conditional branches.
    """

    normalized = copy.deepcopy(question)
    if not isinstance(normalized, dict):
        return normalized
    schema = normalized.get("answer_schema")
    if not isinstance(schema, dict):
        return normalized
    answer_type = str(schema.get("type") or "").upper()
    question_text = normalized.get("prompt") or normalized.get("question")
    incomplete_object = answer_type == "OBJECT" and not isinstance(
        schema.get("properties"), dict
    )
    incomplete_array = answer_type == "ARRAY" and not isinstance(
        schema.get("items"), dict
    )
    if incomplete_object or incomplete_array:
        normalized["answer_schema"] = {"type": "STRING"}
    elif answer_type == "BOOLEAN" and boolean_question_requires_text(question_text):
        normalized["answer_schema"] = {"type": "STRING"}
    elif answer_type in {"ENUM", "SELECT"} and enum_question_requires_text(
        question_text
    ):
        normalized["answer_schema"] = {"type": "STRING"}
    return normalized


def widen_gate_questions(questions: list[Any]) -> list[Any]:
    return [widen_gate_question_answer_schema(question) for question in questions]
