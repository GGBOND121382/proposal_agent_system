from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from docx import Document
from pypdf import PdfWriter

from app.context_base import ContextBuilder
from app.delivery_validator import DeliveryValidator
from app.g3_runtime_executor import G3RuntimePromptExecutor
from app.human_gate_bridge import FileHumanGateBridge
from app.post_export_validator import PostExportDeliveryValidator
from app.proposal_constraints import extract_hard_constraints, merge_contract_constraints
from app.runtime_gateway_factory import PortableModelGateway
from app.task_instruction import instruction_text, intended_uses


def test_task_instruction_object_normalizes_to_scalar_and_string_list():
    value = {
        "objective": "生成完整申请书",
        "constraints": ["不得虚构成果", "保持UNKNOWN"],
        "deliverables": ["DOCX", "PDF"],
    }
    assert instruction_text(value) == "生成完整申请书"
    assert intended_uses(value) == [
        "生成完整申请书",
        "不得虚构成果",
        "保持UNKNOWN",
        "DOCX",
        "PDF",
    ]
    assert all(isinstance(item, str) for item in intended_uses(value))


def test_gateway_factory_requires_explicit_bridge_mode(monkeypatch, tmp_path: Path):
    import app.runtime_gateway_factory as module

    class FakeHttp:
        def __init__(self, settings, pack):
            self.kind = "http"

    class FakeBridge:
        def __init__(self, settings, pack):
            self.kind = "bridge"

    monkeypatch.setattr(module, "G3AuditedModelGateway", FakeHttp)
    monkeypatch.setattr(module, "ChatBridgeModelGateway", FakeBridge)
    settings = SimpleNamespace(model_gateway_mode="OPENAI_COMPATIBLE", chat_bridge_dir=tmp_path)
    assert PortableModelGateway(settings, object()).kind == "http"
    settings.model_gateway_mode = "CHAT_BRIDGE"
    assert PortableModelGateway(settings, object()).kind == "bridge"
    settings.chat_bridge_dir = None
    monkeypatch.delenv("CHAT_BRIDGE_DIR", raising=False)
    try:
        PortableModelGateway(settings, object())
    except ValueError as exc:
        assert "CHAT_BRIDGE_DIR" in str(exc)
    else:
        raise AssertionError("bridge mode must require a directory")


def test_human_gate_bridge_binds_context_hash_and_role(tmp_path: Path):
    gate = {
        "id": "gate-1",
        "project_id": "project-1",
        "workflow_id": "wf-1",
        "gate_type": "SCHEME_CONFIRMATION",
        "target_id": "artifact-1",
        "required_role": "PROJECT_OWNER",
        "allowed_actions": ["CONFIRM", "RETURN"],
        "questions": [{"question_id": "q1", "question": "确认？"}],
        "context_hash": "a" * 64,
    }

    class Engine:
        def __init__(self):
            self.calls = []

        def decide_gate(self, gate_id, **kwargs):
            self.calls.append((gate_id, kwargs))
            return {"id": gate_id, "status": "APPROVED"}

    bridge = FileHumanGateBridge(tmp_path, poll_seconds=0.01, timeout_seconds=2)
    request_path = bridge.publish(gate)
    assert json.loads(request_path.read_text(encoding="utf-8"))["context_hash"] == "a" * 64
    bridge.responses_dir.mkdir(parents=True, exist_ok=True)
    (bridge.responses_dir / "gate-1.json").write_text(
        json.dumps(
            {
                "gate_id": "gate-1",
                "context_hash": "a" * 64,
                "action": "CONFIRM",
                "decided_by": "gpt",
                "decided_role": "PROJECT_OWNER",
                "answers": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    engine = Engine()
    result = asyncio.run(bridge.wait_and_apply(engine, gate))
    assert result["status"] == "APPROVED"
    assert engine.calls[0][1]["context_hash"] == "a" * 64
    assert (bridge.consumed_dir / "gate-1.json").is_file()


def test_live_compaction_preserves_structural_arrays():
    value = {
        "linked_sections": [{"section_id": f"sec-{i}"} for i in range(14)],
        "mandatory_sections": [f"sec-{i}" for i in range(14)],
        "tasks": [{"revision_task_id": f"task-{i}"} for i in range(14)],
        "items": [{"item_id": f"item-{i}"} for i in range(34)],
        "relations": [{"relation_id": f"rel-{i}"} for i in range(19)],
    }
    compact = G3RuntimePromptExecutor._compact_live_value(value, aggressive=True)
    assert len(compact["linked_sections"]) == 14
    assert len(compact["mandatory_sections"]) == 14
    assert len(compact["tasks"]) == 14
    assert len(compact["items"]) == 34
    assert len(compact["relations"]) == 19


def test_scoped_plan_uses_contract_order_not_first_task_fallback():
    contracts = [
        {"section_id": f"sec-{i}", "title": f"章节{i}", "argument_function": f"职责{i}"}
        for i in range(14)
    ]
    tasks = [
        {
            "revision_task_id": f"task-{i}",
            "objective": f"其他表述{i}",
            "required_input_ids": [],
            "issue_ids": [f"issue-{i}"],
            "acceptance_rules": ["完成本章"],
        }
        for i in range(14)
    ]
    plan = {"tasks": tasks, "dependencies": []}
    architecture = {"section_contracts": contracts}
    scoped = ContextBuilder._scoped_plan(plan, architecture, contracts[11])
    assert [item["revision_task_id"] for item in scoped["tasks"]] == ["task-11"]


def test_scheme_constraints_are_normalized_and_merged():
    scheme = {
        "rules": [
            {"rule_id": "r-page", "mandatory": True, "rule_type": "PAGE_OR_WORD_LIMIT", "statement": "正文16—20页"},
            {"rule_id": "r-ref", "mandatory": True, "rule_type": "DOCUMENT_STRUCTURE", "statement": "参考文献30—40篇"},
            {"rule_id": "r-visual", "mandatory": True, "rule_type": "DOCUMENT_STRUCTURE", "statement": "至少5图6表"},
        ]
    }
    constraints = extract_hard_constraints(scheme)
    assert constraints["main_body_pages"] == {"min": 16, "max": 20}
    assert constraints["references"] == {"min": 30, "max": 40}
    assert constraints["minimum_figures"] == 5
    assert constraints["minimum_tables"] == 6
    merged = merge_contract_constraints(
        {
            "contract_id": "c1",
            "document_type": "RESEARCH_PROPOSAL",
            "funding_scheme": None,
            "primary_evaluation_logic": "SCIENTIFIC_MERIT",
            "target_evaluators": [],
            "max_main_pages": None,
            "max_core_research_questions": 4,
            "mandatory_sections": [],
            "appendix_only_topics": [],
            "forbidden_main_body_topics": [],
            "status": "CONFIRMED",
        },
        constraints,
    )
    assert merged["min_main_pages"] == 16
    assert merged["max_main_pages"] == 20
    assert merged["min_reference_count"] == 30
    assert merged["min_figure_count"] == 5


def test_placeholder_detector_distinguishes_boundary_statement():
    pattern = DeliveryValidator.PLACEHOLDER_PATTERNS["PLACEHOLDER_WORD"]
    assert not pattern.search("对于尚未获得的数据，设置待补充条件并保持UNKNOWN状态。")
    assert pattern.search("待补充：申请人论文清单")
    assert pattern.search("TODO")


def test_delivery_validator_enforces_guide_counts(tmp_path: Path):
    docx = tmp_path / "proposal.docx"
    pdf = tmp_path / "proposal.pdf"
    document = Document()
    document.add_heading("摘要", level=1)
    document.add_paragraph("本项目研究需求变更影响分析与智能测试推荐。")
    document.save(docx)
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with pdf.open("wb") as handle:
        writer.write(handle)

    validator = PostExportDeliveryValidator(SimpleNamespace())
    report = validator.validate_structure(
        docx,
        pdf,
        expected_sections=["摘要"],
        expected_candidates=[
            {
                "section_id": "sec-abstract",
                "section_title": "摘要",
                "candidate_id": "candidate-1",
                "paragraphs": ["本项目研究需求变更影响分析与智能测试推荐。"],
            }
        ],
        expected_constraints={
            "source_rule_ids": ["r-page", "r-ref", "r-visual"],
            "main_body_pages": {"min": 16, "max": 20},
            "references": {"min": 30, "max": 40},
            "minimum_figures": 5,
            "minimum_tables": 6,
        },
    )
    codes = {item["code"] for item in report["findings"]}
    assert "D5_GUIDE_PAGE_COUNT_OUT_OF_RANGE" in codes
    assert "D5_GUIDE_REFERENCE_COUNT_OUT_OF_RANGE" in codes
    assert "D5_GUIDE_FIGURE_MINIMUM_UNMET" in codes
    assert "D5_GUIDE_TABLE_MINIMUM_UNMET" in codes


def test_repair_allowlist_accepts_named_selector_but_keeps_explicit_index_strict():
    from app.track_b import TrackBAgentPromptValidator

    validator = object.__new__(TrackBAgentPromptValidator)
    payload = {
        "allowed_paths": ["content.section_contracts.word_budget"],
        "protected_paths": [],
        "findings_to_repair": [{"code": "QG_BUDGET"}],
    }
    result = {
        "changed_paths": ["content.section_contracts[LITERATURE_REVIEW].word_budget"],
        "resolved_finding_codes": ["QG_BUDGET"],
    }
    assert validator._audit_repair_scope(payload, result) == []
    strict_payload = {**payload, "allowed_paths": ["content.section_contracts[3].word_budget"]}
    findings = validator._audit_repair_scope(
        strict_payload,
        {**result, "changed_paths": ["content.section_contracts[4].word_budget"]},
    )
    assert any(item.code == "QG_REPAIR_PATH_OUTSIDE_ALLOWLIST" for item in findings)
