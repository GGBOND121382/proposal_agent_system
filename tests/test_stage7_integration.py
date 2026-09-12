from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from stage7_tools.stage7_integration import (
    deterministic_report,
    load_schema,
    paragraphs,
    repair_request,
    sha256_text,
)

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def prepare_run(tmp_path: Path, record_name: str = "stage7_repaired_sections_round2.json"):
    (tmp_path / "source_snapshots").mkdir(parents=True)
    (tmp_path / "intermediate").mkdir(parents=True)
    (tmp_path / "source_snapshots" / "stage5_section_plan.json").write_text(
        (FIX / "stage7_section_plan.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "source_snapshots" / "stage4a_evidence_completion.json").write_text(
        (FIX / "stage7_evidence_completion.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    record = load(record_name)
    (tmp_path / "intermediate" / "repaired_sections.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    return record


def sections(record):
    return {x["section_id"]: x["candidate"] for x in record["sections"]}


def test_final_integrated_candidate_passes(tmp_path: Path):
    record = prepare_run(tmp_path)
    report = deterministic_report(tmp_path, sections(record), True)
    assert report["verdict"] == "PASS", report
    assert report["section_count"] == 14
    assert report["max_pages"] <= 20
    assert report["visible_citation_count"] > 0


def test_first_repair_round_keeps_expression_findings(tmp_path: Path):
    record = prepare_run(tmp_path, "stage7_repaired_sections_round1.json")
    report = deterministic_report(tmp_path, sections(record), True)
    assert {x["code"] for x in report["findings"]} == {"POST_REPAIR_EXPRESSION_ARTIFACTS"}


def test_heading_terms_are_checked(tmp_path: Path):
    record = prepare_run(tmp_path)
    broken = sections(copy.deepcopy(record))
    broken["SEC-06"]["subsections"][3]["title"] = "Trace评价与跨场景验证"
    report = deterministic_report(tmp_path, broken, True)
    assert any(x["code"] == "DOCUMENT_TYPE_DRIFT" for x in report["findings"])


def test_final_headings_use_proposal_language():
    final = sections(load("stage7_repaired_sections_round2.json"))
    assert final["SEC-06"]["subsections"][3]["title"] == "全过程证据评价与跨场景验证"
    assert final["SEC-09"]["subsections"][2]["title"] == "全过程证据与责任收益归因"
    visual = next(x for x in final["SEC-12"]["visual_placeholders"] if x["visual_id"] == "TAB-07")
    assert visual["caption"] == "工作包依赖、阶段验收条件与验证证据表"


def test_visual_caption_terms_are_checked(tmp_path: Path):
    record = prepare_run(tmp_path)
    broken = sections(copy.deepcopy(record))
    visual = next(x for x in broken["SEC-12"]["visual_placeholders"] if x["visual_id"] == "TAB-07")
    visual["caption"] = "工作包依赖、阶段Gate与验收证据表"
    report = deterministic_report(tmp_path, broken, True)
    assert any(x["code"] == "DOCUMENT_TYPE_DRIFT" for x in report["findings"])


def test_second_repair_request_reads_active_candidate(tmp_path: Path):
    record = prepare_run(tmp_path, "stage7_repaired_sections_round1.json")
    critic = {
        "findings": [{"code": "POST_REPAIR_EXPRESSION_ARTIFACTS", "target_section_ids": ["SEC-05"]}]
    }
    req = repair_request(tmp_path, critic, 2)
    p = next(
        x for s in req["input_envelope"]["sections"] if s["section_id"] == "SEC-05"
        for x in s["paragraphs"] if x["paragraph_id"] == "P-SEC-05-02"
    )
    active = next(
        x for x in paragraphs(sections(record)["SEC-05"])
        if x["paragraph_id"] == "P-SEC-05-02"
    )
    assert p["text"] == active["text"]
    assert sha256_text(p["text"]) == hashlib.sha256(active["text"].encode()).hexdigest()


def test_repair_request_includes_subsection_titles(tmp_path: Path):
    prepare_run(tmp_path, "stage7_repaired_sections_round1.json")
    req = repair_request(tmp_path, {"findings": [{"code": "X"}]}, 2)
    sec6 = next(x for x in req["input_envelope"]["sections"] if x["section_id"] == "SEC-06")
    assert sec6["subsection_titles"]
    assert {x["subsection_id"] for x in sec6["subsection_titles"]} == {
        "SEC-06-01", "SEC-06-02", "SEC-06-03", "SEC-06-04", "SEC-06-05"
    }


def test_repair_request_includes_visual_placeholders(tmp_path: Path):
    prepare_run(tmp_path, "stage7_repaired_sections_round1.json")
    req = repair_request(tmp_path, {"findings": [{"code": "X"}]}, 2)
    sec12 = next(x for x in req["input_envelope"]["sections"] if x["section_id"] == "SEC-12")
    assert any(x["visual_id"] == "TAB-07" for x in sec12["visual_placeholders"])


def test_document_repair_schema_accepts_second_round_heading_edits():
    schema = load_schema("document_repair.schema.json")
    payload = {
        "schema_version": "1.0",
        "prompt_id": "P-STAGE7-TARGETED-DOCUMENT-EDIT",
        "actual_model_id": "external-model",
        "endpoint_id": "file-bridge",
        "repair_round": 2,
        "basis_finding_codes": ["POST_REPAIR_EXPRESSION_ARTIFACTS"],
        "edits": [{
            "section_id": "SEC-05", "paragraph_id": "P-1",
            "old_text_sha256": "a" * 64,
            "new_text": "这是一个长度足够且保持原意不变的局部表达修正文本，用于验证输出结构。",
            "change_type": "TERMINOLOGY_NORMALIZATION", "reason": "消除重复。"
        }],
        "heading_edits": [{
            "section_id": "SEC-06", "subsection_id": "SEC-06-04",
            "old_title_sha256": "b" * 64, "new_title": "全过程证据评价与跨场景验证",
            "reason": "规范标题。"
        }],
        "visual_caption_edits": [{
            "section_id": "SEC-12", "visual_id": "TAB-07",
            "old_caption_sha256": "c" * 64, "new_caption": "工作包依赖、阶段验收条件与验证证据表",
            "reason": "规范图表标题。"
        }],
        "citation_map": [
            {"source_id": f"SRC-PUB-{i:02d}", "citation_number": i} for i in range(1, 14)
        ] + [{"source_id": "SRC-STD-01", "citation_number": 14}],
        "summary": "验证第二轮段落和标题局部修正结构。"
    }
    assert not list(Draft202012Validator(schema).iter_errors(payload))


def test_second_round_preserves_paragraph_metadata():
    r1 = sections(load("stage7_repaired_sections_round1.json"))
    r2 = sections(load("stage7_repaired_sections_round2.json"))
    fields = ["paragraph_id", "role", "node_ids", "rq_ids", "source_ids", "novel_content_key", "claim_status"]
    for sid in r1:
        p1 = {x["paragraph_id"]: x for x in paragraphs(r1[sid])}
        p2 = {x["paragraph_id"]: x for x in paragraphs(r2[sid])}
        assert set(p1) == set(p2)
        for pid in p1:
            for field in fields:
                assert p1[pid][field] == p2[pid][field]
