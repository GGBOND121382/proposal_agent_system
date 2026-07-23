from app.context_base import ContextBuilder


def test_default_task_instruction_is_stage_neutral():
    result = ContextBuilder._structured_task_instruction(
        "仅用于验证智能体运行，不形成正式申请书。",
        ["section-stage0"],
        {},
    )
    requirements = result["specific_requirements"]
    assert "按已确认的任务范围与对象合同完成当前阶段产物" in requirements
    assert all("完整申请书" not in item for item in requirements)
    assert all("公开调研" not in item for item in requirements)


def test_explicit_task_requirements_are_preserved():
    result = ContextBuilder._structured_task_instruction(
        "完成指定任务。",
        ["section-1"],
        {"specific_requirements": ["仅生成章节一"]},
    )
    assert result["specific_requirements"] == ["仅生成章节一"]
