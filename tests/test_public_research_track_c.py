from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.skills.base import SkillContext
from app.skills.public_research import PublicResearchArchiveError, PublicResearchArchiveSkill


@dataclass
class SettingsStub:
    public_search_provider: str = "connector"
    public_search_max_results: int = 20
    public_research_record_file: str = ""
    public_research_connector_file: str = ""
    public_search_base_url: str = ""
    research_fetch_timeout_seconds: int = 5
    research_max_source_bytes: int = 1024 * 1024


def plan(*, innovation: bool = False) -> dict:
    questions = [
        "近五年该技术的权威标准、代表性方法与可比较基线是什么？",
        "现有方法的局限机制和可验证研究差距是什么？",
    ]
    if innovation:
        questions.append("拟议创新点相对最近工作的实质增量是什么？")
    return {
        "plan_id": "plan-c-test",
        "task_type": "PUBLIC_RESEARCH",
        "research_questions": questions,
        "queries": [
            "2021 2026 authoritative standard comparable baseline method",
            "2021 2026 recent work limitation mechanism research gap",
        ],
        "source_priorities": ["official standards", "peer reviewed papers", "project pages"],
        "time_scope": "2021-01-01/2026-12-31",
        "evidence_requirements": ["recent work", "limitation mechanism", "comparable baseline"],
        "prohibited_inferences": ["不得补写未归档 DOI 或题名"],
    }


def write_connector(path: Path, *, weak_content: bool = False) -> None:
    q1, q2 = plan()["queries"]
    standard_text = (
        "Authoritative standard published in 2024. It defines a comparable baseline and documents a limitation mechanism."
        if not weak_content
        else "Authoritative project description with implementation notes only."
    )
    paper_text = (
        "Recent work from 2025 compares the baseline and explains the limitation mechanism and research gap."
        if not weak_content
        else "A method description with implementation notes only."
    )
    payload = {
        "run_id": "connector-run-c",
        "connector": "approved-test-connector",
        "created_at": "2026-07-15T00:00:00Z",
        "agent_generated_queries": [q1, q2],
        "responses": [
            {
                "query": q1,
                "retrieved_at": "2026-07-15T00:00:00Z",
                "results": [
                    {
                        "title": "Official Standard" if not weak_content else "Official Project Description",
                        "url": "https://www.rfc-editor.org/rfc/rfc9000.html?utm_source=test",
                        "published_at": "2024-01-01",
                        "publisher": "RFC Editor",
                        "content_text": standard_text,
                    }
                ],
            },
            {
                "query": q2,
                "retrieved_at": "2026-07-15T00:00:01Z",
                "results": [
                    {
                        "title": "Official Standard duplicate" if not weak_content else "Official Project Description duplicate",
                        "url": "https://www.rfc-editor.org/rfc/rfc9000.html",
                        "published_at": "2024-01-01",
                        "publisher": "RFC Editor",
                        "content_text": standard_text,
                    },
                    {
                        "title": "Recent Work on the Method" if not weak_content else "Method Description",
                        "url": "https://arxiv.org/abs/2501.00001",
                        "published_at": "2025-01-02",
                        "authors": ["A. Researcher"],
                        "content_text": paper_text,
                    },
                ],
            },
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def run_skill(tmp_path: Path, research_plan: dict | None = None, *, weak_content: bool = False):
    connector = tmp_path / "connector.json"
    write_connector(connector, weak_content=weak_content)
    skill = PublicResearchArchiveSkill(SettingsStub(public_research_connector_file=str(connector)))
    context = SkillContext("project-c", "wf-c", "PUBLIC", str(tmp_path / "data"))
    return skill.run(
        {"provider": "connector", "connector_file": str(connector), "max_results": 20, "plan": research_plan or plan()},
        context,
    )


def test_plan_rejects_broad_keyword_stack(tmp_path: Path) -> None:
    connector = tmp_path / "connector.json"
    write_connector(connector)
    skill = PublicResearchArchiveSkill(SettingsStub(public_research_connector_file=str(connector)))
    context = SkillContext("project-c", "wf-c", "PUBLIC", str(tmp_path / "data"))
    bad = plan()
    bad["queries"] = ["研究"]
    with pytest.raises(PublicResearchArchiveError, match="too broad"):
        skill.run({"provider": "connector", "connector_file": str(connector), "plan": bad}, context)


def test_connector_archive_deduplicates_ranks_and_covers_all_queries(tmp_path: Path) -> None:
    result = run_skill(tmp_path)
    assert result.status == "PASS"
    assert len(result.output["sources"]) == 2
    assert all(item["source_ids"] for item in result.output["query_coverage"])
    manifest = json.loads(Path(result.output["archive_manifest"]).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "2.0"
    assert manifest["records"][0]["source_type"] == "OFFICIAL_STANDARD"
    assert manifest["records"][1]["source_type"] == "PREPRINT"
    assert manifest["records"][0]["canonical_url"].endswith("/rfc/rfc9000.html")
    assert len(manifest["records"][0]["matched_queries"]) == 2
    assert any(item["code"] == "DUPLICATE_CANONICAL_URL" for item in result.output["issues"])
    assert Path(result.output["evidence_ledger"]).exists()
    assert Path(manifest["retrieval_capture"]).exists()


def test_claim_binding_distinguishes_quote_summary_and_synthesis(tmp_path: Path) -> None:
    result = run_skill(tmp_path)
    source_a, source_b = result.output["sources"]
    passage_a = next(item for item in result.output["passages"] if item["source_ref"]["source_id"] == source_a["source_id"])
    synthesis = {
        "claims": [
            {"claim_id": "claim-quote", "claim_text": passage_a["text"][:40], "source_refs": [source_a]},
            {"claim_id": "claim-summary", "claim_text": "The standard defines an auditable comparison basis.", "source_refs": [source_a]},
            {"claim_id": "claim-synthesis", "claim_text": "Recent work and the standard jointly expose a comparable gap.", "source_refs": [source_a, source_b]},
        ],
        "source_comparisons": [],
        "conflicts": [],
    }
    report = PublicResearchArchiveSkill.validate_claim_bindings(synthesis, result.output)
    assert report["status"] == "PASS"
    assert {item["claim_id"]: item["evidence_mode"] for item in report["bindings"]} == {
        "claim-quote": "DIRECT_QUOTE",
        "claim-summary": "SOURCE_SUMMARY",
        "claim-synthesis": "MULTI_SOURCE_SYNTHESIS",
    }


def test_claim_binding_blocks_unknown_or_tampered_source(tmp_path: Path) -> None:
    result = run_skill(tmp_path)
    bad_ref = dict(result.output["sources"][0])
    bad_ref["source_hash"] = "0" * 64
    report = PublicResearchArchiveSkill.validate_claim_bindings(
        {"claims": [{"claim_id": "claim-bad", "claim_text": "Unsupported claim", "source_refs": [bad_ref]}], "source_comparisons": [], "conflicts": []},
        result.output,
    )
    assert report["status"] == "BLOCK"
    codes = {item["code"] for item in report["findings"]}
    assert "SOURCE_HASH_MISMATCH" in codes
    assert "UNBOUND_PUBLIC_CLAIM" in codes


def test_innovation_plan_blocks_when_recent_limit_and_baseline_evidence_are_missing(tmp_path: Path) -> None:
    research_plan = plan(innovation=True)
    research_plan["evidence_requirements"] = []
    result = run_skill(tmp_path, research_plan, weak_content=True)
    assert result.status == "BLOCK"
    unmet = [item for item in result.output["blocking_issues"] if item["code"] == "EVIDENCE_REQUIREMENT_UNMET"]
    assert {item["details"]["intent"] for item in unmet} == {"RECENT_WORK", "LIMITATION_MECHANISM", "COMPARABLE_BASELINE"}


def test_archive_verifier_detects_tampering(tmp_path: Path) -> None:
    result = run_skill(tmp_path)
    assert PublicResearchArchiveSkill.verify_archive(result.output["archive_root"])["status"] == "PASS"
    manifest = json.loads(Path(result.output["archive_manifest"]).read_text(encoding="utf-8"))
    Path(manifest["records"][0]["text_path"]).write_text("tampered", encoding="utf-8")
    report = PublicResearchArchiveSkill.verify_archive(result.output["archive_root"])
    assert report["status"] == "BLOCK"
    assert any(item["code"] == "ARCHIVE_HASH_MISMATCH" for item in report["findings"])
