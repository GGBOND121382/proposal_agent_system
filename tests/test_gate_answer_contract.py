from __future__ import annotations

import pytest

from app.gate_answer_contract import (
    semantic_question_answer_schema,
    widen_gate_question_answer_schema,
)
from app.workflow_input import _coerce_answer, build_human_resolutions


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


@pytest.mark.parametrize(
    ("schema", "raw", "expected"),
    [
        ({"type": "BOOLEAN"}, "true", True),
        ({"type": "BOOLEAN"}, "false", False),
        ({"type": "BOOLEAN"}, "是", True),
        ({"type": "BOOLEAN"}, "否", False),
        ({"type": "NUMBER"}, "12.5", 12.5),
        ({"type": "OBJECT", "properties": {"name": {"type": "STRING"}}}, '{"name":"x"}', {"name": "x"}),
        ({"type": "ARRAY", "items": {"type": "STRING"}}, '["a","b"]', ["a", "b"]),
    ],
)
def test_wf3_browser_string_answers_are_deterministically_typed(schema, raw, expected):
    assert _coerce_answer(raw, {"answer_schema": schema}) == expected


@pytest.mark.parametrize(
    ("schema", "raw"),
    [
        ({"type": "BOOLEAN"}, ""),
        ({"type": "NUMBER"}, "true"),
        ({"type": "OBJECT", "properties": {}}, "[]"),
        ({"type": "ARRAY", "items": {"type": "STRING"}}, "{}"),
    ],
)
def test_wf3_gate_rejects_blank_or_wrong_typed_browser_values(schema, raw):
    with pytest.raises(ValueError):
        value = _coerce_answer(raw, {"answer_schema": schema})
        if raw == "":
            # Blank-required handling occurs in the resolution builder.
            build_human_resolutions(
                gate_id="gate-wf3",
                prompt_id="P-PUBLIC-RESEARCH-PLAN",
                questions=[
                    {
                        "question_id": "q1",
                        "question": "是否确认？",
                        "answer_schema": schema,
                        "blocking": True,
                        "target_paths": ["/payload/value"],
                    }
                ],
                answers=[{"question_id": "q1", "value": value}],
                decided_by="pytest",
                decided_role="PROJECT_OWNER",
                require_any_answer=True,
            )


def test_same_target_path_questions_keep_distinct_question_identity_and_typed_context():
    questions = [
        {
            "question_id": "plan-scope",
            "question": "是否确认计划范围？",
            "answer_schema": {"type": "BOOLEAN"},
            "blocking": True,
            "target_paths": ["/payload/research_scope"],
        },
        {
            "question_id": "synthesis-scope",
            "question": "请说明综合范围。",
            "answer_schema": {"type": "STRING"},
            "blocking": True,
            "target_paths": ["/payload/research_scope"],
        },
    ]
    resolutions = build_human_resolutions(
        gate_id="gate-wf3",
        prompt_id="P-PUBLIC-RESEARCH-SYNTHESIS",
        questions=questions,
        answers=[
            {"question_id": "plan-scope", "value": "true"},
            {"question_id": "synthesis-scope", "value": "仅综合公开文献"},
        ],
        decided_by="pytest",
        decided_role="PROJECT_OWNER",
        require_any_answer=True,
    )
    assert [item["question_id"] for item in resolutions] == [
        "plan-scope",
        "synthesis-scope",
    ]
    assert resolutions[0]["answer"] is True
    assert resolutions[0]["question"] == "是否确认计划范围？"
    assert resolutions[1]["answer"] == "仅综合公开文献"
