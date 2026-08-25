from __future__ import annotations

import pytest

from app.gate_answer_contract import (
    semantic_question_answer_schema,
    widen_gate_question_answer_schema,
)


@pytest.mark.parametrize(
    ("question_type", "answer_shape", "question", "allowed_values"),
    [
        (
            "CONFIRMATION",
            "OBJECT",
            "软件模块的名称与状态是什么？请给出语料规模区间。",
            [],
        ),
        (
            "CHOICE",
            "ARRAY",
            "哪些指标仅采用离线回放，哪些保留人员实验？",
            ["速度", "覆盖度", "质量", "可信性", "人员", "工程"],
        ),
        (
            "CONFIRMATION",
            "BOOLEAN",
            "是否具备实验条件？若不具备，是否改用离线回放？",
            [True, False],
        ),
    ],
)
def test_semantic_projection_never_narrows_composite_answer_to_one_value(
    question_type, answer_shape, question, allowed_values
):
    schema = semantic_question_answer_schema({
        "question_type": question_type,
        "answer_shape": answer_shape,
        "question": question,
        "allowed_values": allowed_values,
    })

    assert schema == {"type": "STRING"}


def test_semantic_projection_preserves_simple_boolean_and_scalar_choice():
    assert semantic_question_answer_schema({
        "question_type": "CONFIRMATION",
        "answer_shape": "BOOLEAN",
        "question": "是否确认当前范围？",
        "allowed_values": [True, False],
    }) == {"type": "BOOLEAN", "allowed_values": []}
    assert semantic_question_answer_schema({
        "question_type": "CHOICE",
        "answer_shape": "STRING",
        "question": "请选择一个验收基线。",
        "allowed_values": ["内部基线", "公开基线"],
    }) == {
        "type": "ENUM",
        "allowed_values": ["内部基线", "公开基线"],
    }


@pytest.mark.parametrize(
    "question",
    [
        {
            "question": "模块名称和状态是什么？请给出规模区间。",
            "answer_schema": {"type": "BOOLEAN", "allowed_values": []},
        },
        {
            "question": "哪些指标采用离线回放，哪些保留人员实验？",
            "answer_schema": {
                "type": "ENUM",
                "allowed_values": ["速度", "覆盖度", "质量"],
            },
        },
    ],
)
def test_generic_gate_boundary_widens_controls_that_cannot_carry_answer(question):
    normalized = widen_gate_question_answer_schema(question)

    assert normalized["answer_schema"] == {"type": "STRING"}


def test_generic_gate_boundary_preserves_sufficient_controls():
    questions = [
        {
            "question": "是否确认当前范围？",
            "answer_schema": {"type": "BOOLEAN", "allowed_values": []},
        },
        {
            "question": "请选择一个验收基线。",
            "answer_schema": {
                "type": "ENUM",
                "allowed_values": ["内部基线", "公开基线"],
            },
        },
        {
            "question": "请填写结构化配置。",
            "answer_schema": {
                "type": "OBJECT",
                "properties": {"name": {"type": "STRING"}},
            },
        },
        {
            "question": "请填写任务列表。",
            "answer_schema": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
        },
    ]

    assert [
        widen_gate_question_answer_schema(question)["answer_schema"]
        for question in questions
    ] == [question["answer_schema"] for question in questions]
