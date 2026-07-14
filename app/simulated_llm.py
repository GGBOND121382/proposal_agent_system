from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .util import new_id, sha256_text
from .logistics_application_content import SECTION_TITLES as LOGISTICS_SECTION_TITLES, REF_CATALOG as LOGISTICS_REF_CATALOG, blocks_for as logistics_blocks_for
from .transport_optimization_application_content import SECTION_TITLES as TRANSPORT_SECTION_TITLES, REF_CATALOG as TRANSPORT_REF_CATALOG, blocks_for as transport_blocks_for


class SimulatedLLM:
    """Deterministic local generator used to simulate multi-agent runs.

    It starts from the replay output for each prompt to preserve schema fidelity,
    then patches key fields so the output reflects the active project/section.
    """

    def __init__(self, pack):
        self.pack = pack
        self.figure_dir = Path(__file__).resolve().parents[1] / "data" / "figures"

    def invoke(self, prompt_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
        base = self.pack.replay_output(prompt_id, "normal")
        handler = getattr(self, f"_handle_{prompt_id.removeprefix('P-').lower().replace('-', '_')}", None)
        if handler is None:
            return base
        return handler(copy.deepcopy(base), envelope)

    @staticmethod
    def _project_name(envelope: dict[str, Any]) -> str:
        return envelope.get("payload", {}).get("project_name") or envelope.get("scope", {}).get("project_id") or "本项目"

    @staticmethod
    def _section(envelope: dict[str, Any]) -> dict[str, Any]:
        return envelope.get("payload", {}).get("source_section", {})

    @staticmethod
    def _clean_title(title: str) -> str:
        return re.sub(r"^[#\s]+", "", title or "").strip()

    @classmethod
    def _is_transport_project(cls, envelope: dict[str, Any]) -> bool:
        project_name = cls._project_name(envelope)
        payload_text = json.dumps(envelope.get("payload", {}), ensure_ascii=False)
        markers = ["物流场景", "运输方案优化", "车辆路径", "多式联运"]
        return any(marker in project_name or marker in payload_text for marker in markers)

    @classmethod
    def _section_titles(cls, envelope: dict[str, Any]) -> list[str]:
        if cls._is_transport_project(envelope):
            return TRANSPORT_SECTION_TITLES
        return LOGISTICS_SECTION_TITLES

    @classmethod
    def _catalog(cls, envelope: dict[str, Any]) -> list[dict[str, Any]]:
        if cls._is_transport_project(envelope):
            return TRANSPORT_REF_CATALOG
        return LOGISTICS_REF_CATALOG

    @classmethod
    def _blocks_for(cls, title: str, envelope: dict[str, Any]) -> list[str]:
        if cls._is_transport_project(envelope):
            return transport_blocks_for(title)
        return logistics_blocks_for(title)

    @classmethod
    def _domain_term(cls, envelope: dict[str, Any]) -> str:
        if cls._is_transport_project(envelope):
            return "物流运输方案优化系统"
        return "后勤保障智能体"

    @classmethod
    def _research_queries(cls, envelope: dict[str, Any]) -> list[str]:
        if cls._is_transport_project(envelope):
            return [
                "vehicle routing problem survey heuristics exact methods time windows",
                "dynamic vehicle routing online stochastic requests review",
                "multi depot inventory routing warehouse transportation optimization",
                "multimodal freight transport optimization timetable intermodal survey",
                "multi agent reinforcement learning logistics transportation scheduling",
                "learning to route neural combinatorial optimization vehicle routing",
                "digital twin logistics transportation real time optimization",
                "green vehicle routing carbon emissions sustainable logistics review",
                "large language model agents operations research optimization tool use",
                "OR-Tools vehicle routing CP-SAT official documentation",
            ]
        return [
            "logistics agent system survey 2023 2024",
            "multi-agent collaboration workflow orchestration logistics",
            "knowledge graph RAG enterprise operations",
            "AI planning scheduling dynamic replanning survey",
            "human in the loop autonomous agents benchmark",
        ]

    @staticmethod
    def _item_number(item: dict[str, Any], fallback: int) -> int:
        return int(item.get("reference_number") or item.get("id") or fallback)

    @staticmethod
    def _item_summary(item: dict[str, Any]) -> str:
        return str(item.get("content_text") or item.get("excerpt") or item.get("note") or item.get("title") or "公开来源")

    def _handle_security_classify(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        result = base["result"]
        result["recommended_level"] = "INTERNAL"
        result["sensitive_fields"] = ["人员姓名", "组织名称"]
        result["allowed_environments"] = ["OFFLINE_LOCAL", "ONLINE_PUBLIC"]
        result["rationale"] = ["申请书写作在内部环境执行，公开检索仅允许使用脱敏任务包。"]
        result["confidence"] = "HIGH"
        return base

    def _handle_security_classify_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_scheme_extract(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        r = base["result"]["scheme_profile"]
        r["scheme_name"] = self._project_name(envelope)
        r["scheme_type"] = "RESEARCH"
        r["funding_organization"] = "内部研发计划"
        r["application_year"] = 2026
        r["guide_direction_name"] = "物流运输优化与智能体系统" if self._is_transport_project(envelope) else "智能体系统与复杂服务保障"
        r["duration_months"] = 36
        return base

    def _handle_scheme_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_project_definition_extract(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        pd = base["result"]["project_definition"]
        item = pd["items"][0]
        item["content"]["statement"] = ("构建物流运输方案优化系统总体架构并形成原型系统。" if self._is_transport_project(envelope) else "构建后勤保障智能体总体架构并形成原型系统。")
        item["content"]["target_state"] = ("形成可在城市配送、多仓协同和多式联运场景运行的运输优化原型。" if self._is_transport_project(envelope) else "形成可在典型场景运行的后勤保障智能体原型系统。")
        item["content"]["success_definition"] = "通过典型场景验证并输出完整文档。"
        return base

    def _handle_project_definition_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_fact_extract(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        facts = [
            {
                "claim_id": "fact-001",
                "claim_text": "项目周期拟定为36个月。",
                "claim_type": "PLAN",
                "subject_id": "project-001",
                "temporal_status": "PLANNED",
                "qualifiers": ["拟"],
                "numeric_values": [],
                "source_refs": [],
                "knowledge_status": "CONFIRMED",
                "security_level": "INTERNAL",
            },
            {
                "claim_id": "fact-002",
                "claim_text": "项目拟形成原型系统、验证报告、样例数据集和配套文档。",
                "claim_type": "PLAN",
                "subject_id": "project-001",
                "temporal_status": "PLANNED",
                "qualifiers": ["拟"],
                "numeric_values": [],
                "source_refs": [],
                "knowledge_status": "CONFIRMED",
                "security_level": "INTERNAL",
            },
        ]
        base["result"]["fact_candidates"] = facts
        base["result"]["coverage"] = [{"span_id": f"span-{i:03d}", "claim_ids": [fact["claim_id"]]} for i, fact in enumerate(facts, 1)]
        base["result"]["conflict_candidates"] = []
        return base

    def _handle_fact_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_project_readiness_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_template_extract(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        template = base["result"]["template"]
        template["global_argument"] = "背景—现状—目标—内容—关键技术—技术路线—验证—成果—预算—保障"
        template["format_rules"] = ["章节完整", "包含图表", "关键结论可追踪", "参考文献不少于20条"]
        template["applicability"] = ["科研申请书", "复杂项目建议书"]
        return base

    def _handle_template_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_safe_online_package(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        project_name = self._project_name(envelope)
        result = base["result"]
        result["task_description"] = f"围绕{project_name}检索公开研究、行业报告、标准规范与相关案例，仅用于补充研究现状、技术路线和评价指标。"
        result["queries"] = self._research_queries(envelope)
        result["allowed_context"] = (["车辆路径", "多仓协同", "多式联运", "动态重规划", "多智能体", "绿色物流"] if self._is_transport_project(envelope) else ["智能体系统", "后勤保障", "资源调度", "知识图谱", "流程编排", "系统评估"])
        result["entity_placeholders"] = []
        result["removed_fields"] = ["人员姓名", "组织名称", "详细地址", "联系电话"]
        result["prohibited_inferences"] = ["不得反推内部组织与人员信息", "不得据公开资料推断未提供的内部事实"]
        result["prohibited_outputs"] = ["不得输出真实敏感字段", "不得输出未核实的内部信息"]
        result["security_level"] = "PUBLIC"
        return base

    def _handle_safe_online_package_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_public_research_plan(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        result = base["result"]
        if self._is_transport_project(envelope):
            result["research_questions"] = [
                "车辆路径、时间窗、取送和多仓问题有哪些精确与启发式方法？",
                "动态订单、交通变化和车辆故障下如何进行在线决策与低扰动重规划？",
                "多式联运、库存运输联动和绿色物流如何统一建模与评价？",
                "学习增强优化和多智能体协同可承担哪些任务，如何保证硬约束可行性？",
                "公开研究、算法运行、Mermaid图形和文档结论如何形成可验证证据链？",
            ]
        else:
            result["research_questions"] = [
                "大模型智能体的规划、工具调用、记忆、反思和多智能体协同技术发展到什么程度？",
                "RAG、GraphRAG、知识图谱与可追踪证据链如何支撑专业场景？",
                "组合优化、车辆路径、排程和动态重规划可采用哪些代表性方法？",
                "Agent评测、安全治理、人机协同和工程可观测性有哪些公开依据？",
            ]
        result["queries"] = self._research_queries(envelope)
        result["source_priorities"] = ["国际标准与官方规范", "政府/标准机构页面", "协议设计文档", "同行评议论文", "官方开源项目文档"]
        result["evidence_requirements"] = ["覆盖不少于30个可核验公开来源", "保存来源URL、获取时间、摘录与SHA-256", "正文引用与参考文献编号一一对应", "只使用归档来源形成PUBLIC_CLAIM"]
        result["prohibited_inferences"] = ["不得从公开资料反推内部组织、人员或部署信息", "不得将外部性能数字直接作为本项目实测结果"]
        return base

    def _handle_public_research_synthesis(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        payload = envelope.get("payload", {})
        retrieved = [item for item in payload.get("retrieved_sources", []) if isinstance(item, dict)]
        passages = [item for item in payload.get("extracted_passages", []) if isinstance(item, dict)]
        passage_by_source = {str(p.get("source_ref", {}).get("source_id")): p for p in passages}
        catalog = self._catalog(envelope)
        catalog_by_source = {str(item.get("source_id") or f"public-src-{self._item_number(item, i):03d}"): item for i, item in enumerate(catalog, 1)}
        if not retrieved:
            retrieved = [self._source_ref(self._item_number(item, i), item) for i, item in enumerate(catalog, 1)]
        elif len(retrieved) < 2:
            existing = {str(item.get("source_id")) for item in retrieved}
            for i, item in enumerate(catalog, 1):
                candidate = self._source_ref(self._item_number(item, i), item)
                if str(candidate.get("source_id")) not in existing:
                    retrieved.append(candidate)
                    break
        claims = []
        for idx, source_ref in enumerate(retrieved, 1):
            source_id = str(source_ref.get("source_id") or f"public-src-{idx:03d}")
            item = catalog_by_source.get(source_id, {})
            passage = passage_by_source.get(source_id, {})
            claim_text = str(passage.get("text") or self._item_summary(item))[:6000]
            claims.append({
                "claim_id": f"pub-claim-{idx:03d}",
                "claim_text": claim_text,
                "claim_type": "PUBLIC_CLAIM",
                "subject_id": None,
                "temporal_status": "TIME_INDEPENDENT",
                "qualifiers": [str(item.get("publisher") or item.get("category") or "PUBLIC_SOURCE")],
                "numeric_values": [],
                "source_refs": [source_ref],
                "knowledge_status": "DOCUMENT_EXTRACTED",
                "security_level": "PUBLIC",
            })
        source_ids = [str(item.get("source_id")) for item in retrieved]
        groups = [source_ids[i:i+5] for i in range(0, min(len(source_ids), 20), 5) if len(source_ids[i:i+5]) >= 2]
        topics = (["车辆路径与混合优化", "动态运输与低扰动重规划", "多仓/多式联运与绿色物流", "多智能体、证据与工程治理"] if self._is_transport_project(envelope) else ["智能体规划与协同", "知识增强与证据追踪", "调度优化与动态重规划", "治理、评测与安全"])
        base["result"]["claims"] = claims
        base["result"]["source_comparisons"] = [
            {"topic": topics[i % len(topics)], "source_ids": group, "agreement": "PARTIAL", "summary": "来源在总体方向上相互支持，但适用场景、成熟度、性能条件和工程边界不同，需在本项目中通过原型与测试进一步验证。"}
            for i, group in enumerate(groups)
        ] or [{"topic": topics[0], "source_ids": source_ids[:2], "agreement": "PARTIAL", "summary": "归档来源提供相关公开依据，工程适配仍需项目验证。"}]
        base["result"]["conflicts"] = []
        base["result"]["limitations"] = ["公开来源说明标准、机制和公开实践，不代表本项目已经完成实测。", "来源真实性由URL、归档记录、摘录和Hash支持；具体主张仍需按正文引用进行人工复核。"]
        base["result"]["coverage_summary"] = f"综合{len(claims)}项实际归档公开来源，研究输入来自public_research.archive技能而非模型记忆。"
        base["source_refs"] = retrieved
        return base

    def _handle_public_research_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_online_result_import_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        claims = envelope.get("payload", {}).get("result_package", {}).get("claims", [])
        base["result"]["import_recommendation"] = "IMPORT_REFERENCE_ONLY"
        base["result"]["accepted_claim_ids"] = [str(item.get("claim_id")) for item in claims if item.get("claim_id")]
        base["result"]["rejected_claim_ids"] = []
        return base

    def _handle_revision_plan(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        payload = envelope.get("payload", {})
        linked_sections = payload.get("linked_sections", [])
        by_title = {s.get("title"): s for s in linked_sections if s.get("level", 0) >= 1 and s.get("title")}
        selected = [by_title[t] for t in self._section_titles(envelope) if t in by_title]
        if not selected and payload.get("source_section"):
            selected = [payload["source_section"]]
        plan = base["result"]["revision_plan"]
        plan["plan_id"] = new_id("plan")
        plan["issues"] = [
            {"issue_id": "issue-coverage", "description": "需形成覆盖研究现状、关键技术、技术路线、系统设计、验证、预算、风险和附录的完整申请书。", "evidence_refs": [], "severity": "P1"},
            {"issue_id": "issue-trace", "description": "[SIM_REPAIR] 公开证据、图表、正文结论和验收指标之间必须保持可追踪关系。", "evidence_refs": [], "severity": "P1"},
        ]
        plan["target_section_ids"] = [s["section_id"] for s in selected]
        plan["read_only_section_ids"] = []
        plan["protected_section_ids"] = []
        plan["tasks"] = [{"revision_task_id": f"rt-{i:03d}", "operation": "SUPPLEMENT", "objective": f"完成章节《{s.get('title')}》正式写作", "issue_ids": ["issue-coverage", "issue-trace"], "required_input_ids": ["item-001"], "acceptance_rules": ["内容具有实质论证", "关键技术与设计章节包含必要图表", "引用编号与参考文献一致", "关键结论具有Trace"]} for i, s in enumerate(selected, 1)]
        base["result"]["readiness_summary"] = [{"task_id": task["revision_task_id"], "readiness": "READY", "missing_input_ids": []} for task in plan["tasks"]]
        base["result"]["scope_rationale"] = [f"按照申报指南对{len(selected)}个正式章节逐章生成、逐章审查并执行全篇一致性审查。"]
        return base

    def _handle_revision_plan_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        plan = envelope.get("payload", {}).get("revision_plan_candidate", {})
        needs_repair = any("[SIM_REPAIR]" in str(item.get("description", "")) for item in plan.get("issues", []))
        if needs_repair:
            base["status"] = "REVISE"
            base["result"]["verdict"] = "REVISE"
            base["findings"] = [{
                "code": "PLAN_SIMULATED_REPAIR",
                "severity": "P1",
                "category": "CONTENT",
                "target_type": "REVISION_PLAN",
                "target_path_or_span": "issues[1].description",
                "description": "计划中包含模拟缺陷标记，需由原生产者定向修复。",
                "evidence_refs": [],
                "repairable": True,
                "repair_instruction": "删除[SIM_REPAIR]标记并保留原有语义。",
                "suggested_route": "ORIGINAL_PRODUCER",
                "blocking": True,
            }]
        else:
            base["status"] = "PASS"
            base["result"]["verdict"] = "ACCEPT"
            base["findings"] = []
        return base

    def _handle_targeted_repair(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        original = copy.deepcopy(envelope.get("payload", {}).get("original_object", {}).get("content", {}))
        if isinstance(original, dict):
            for issue in original.get("issues", []):
                if isinstance(issue, dict) and "[SIM_REPAIR]" in str(issue.get("description", "")):
                    issue["description"] = str(issue["description"]).replace("[SIM_REPAIR]", "").strip()
        base["result"]["repaired_object"] = original
        base["result"]["changed_paths"] = ["content.issues[1].description"]
        base["result"]["unchanged_protected_hashes"] = []
        base["result"]["resolved_finding_codes"] = ["PLAN_SIMULATED_REPAIR"]
        base["result"]["unresolved_finding_codes"] = []
        return base

    def _handle_write_blueprint(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        title = self._clean_title(self._section(envelope).get("title", "未命名章节"))
        content_blocks = self._blocks_for(title, envelope)
        functions = [b.removeprefix("[[H2]]").removeprefix("[[H3]]").strip() for b in content_blocks if b.startswith("[[H2]]") or b.startswith("[[H3]]")]
        if not functions:
            functions = ["说明章节定位", "展开核心论证", "给出实施与验收要点"]
        bp = base["result"]["blueprint"]
        bp["blueprint_id"] = new_id("bp")
        bp["section_objective"] = f"形成《{title}》的完整、可追踪、符合申报指南的正式正文。"
        paragraphs = []
        for i, function in enumerate(functions[:20], 1):
            paragraphs.append({"paragraph_id": f"bp-{hashlib.md5((title+str(i)).encode()).hexdigest()[:10]}", "sequence": i, "function": function, "must_answer": [function], "fact_slots": ["fact-001"], "project_item_slots": ["item-001"], "technical_slots": [], "metric_slots": [], "source_strategy": "REPLACE", "forbidden_content": ["无来源结论", "未声明的不确定事实"], "transition_requirement": None})
        bp["paragraphs"] = paragraphs
        base["result"]["plan_task_coverage"] = [{"revision_task_id": "rt-001", "paragraph_ids": [p["paragraph_id"] for p in paragraphs]}]
        base["result"]["input_usage_summary"] = [{"source_id": "item-001", "used_in_paragraph_ids": [p["paragraph_id"] for p in paragraphs]}]
        return base

    def _handle_write_blueprint_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        return base

    def _handle_write_content(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        section = self._section(envelope)
        title = self._clean_title(section.get("title", "未命名章节"))
        blueprint = envelope.get("payload", {}).get("blueprint_candidate", {}).get("blueprint", {})
        blocks = self._blocks_for(title, envelope)
        bp_paragraphs = blueprint.get("paragraphs") or [{}]
        paragraphs, trace_links = [], []
        for seq, text_block in enumerate(blocks, 1):
            pid = f"p-{hashlib.md5((title+str(seq)).encode()).hexdigest()[:12]}"
            trace_id = f"trace-{hashlib.md5((title+'trace'+str(seq)).encode()).hexdigest()[:12]}"
            role = "正文"
            if text_block.startswith("[[H2]]") or text_block.startswith("[[H3]]"): role = "小节标题"
            elif text_block.startswith("[[TABLE]]"): role = "表格"
            elif text_block.startswith("[[FIGURE]]") or text_block.startswith("[[MERMAID]]"): role = "图示"
            citation_ids = [int(x) for x in re.findall(r"\[(\d+)\]", text_block)]
            source_kind = "PUBLIC_CLAIM" if citation_ids else "SOURCE_TEXT"
            source_id = f"pub-claim-{citation_ids[0]:03d}" if citation_ids else section.get("section_id", "source-section")
            paragraphs.append({"paragraph_id": pid, "sequence": seq, "paragraph_role": role, "text": text_block, "blueprint_paragraph_id": bp_paragraphs[min(seq - 1, len(bp_paragraphs)-1)].get("paragraph_id", "bp-p-001"), "trace_link_ids": [trace_id], "preserved_source_span": None, "contains_unresolved_placeholder": False})
            trace_links.append({"trace_id": trace_id, "target_path": f"paragraphs[{seq-1}]", "source_kind": source_kind, "source_id": source_id, "source_path_or_span": title, "support_type": "DIRECT", "source_hash": sha256_text(title + text_block[:200])})
        base["result"]["candidate_id"] = new_id("cand")
        base["result"]["candidate_text"] = "\n\n".join(blocks)
        base["result"]["paragraphs"] = paragraphs
        base["result"]["trace_links"] = trace_links
        all_ids = [p["paragraph_id"] for p in paragraphs]
        domain_term = self._domain_term(envelope)
        base["result"]["term_usage"] = [{"term": domain_term, "canonical_term": domain_term, "paragraph_ids": all_ids[:max(1,min(8,len(all_ids)))]}]
        base["result"]["unresolved_items"] = []
        base["result"]["source_preservation_summary"] = [{"source_span": title, "action": "REPHRASED", "paragraph_id": p["paragraph_id"]} for p in paragraphs[:min(3,len(paragraphs))]]
        return base

    def _handle_write_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        paragraphs = envelope.get("payload", {}).get("content_candidate", {}).get("result", {}).get("paragraphs", [])
        ids = [p.get("paragraph_id") for p in paragraphs if p.get("paragraph_id")]
        base["result"]["verdict"] = "ACCEPT"
        base["result"]["checked_paragraph_ids"] = ids
        base["result"]["unsupported_trace_ids"] = []
        base["result"]["blueprint_deviation_paragraph_ids"] = []
        base["result"]["scope_violations"] = []
        base["result"]["profile_acceptance_results"] = [
            {"rule": "章节结构完整", "passed": True, "evidence": "已包含总述、小节、图表和小结性内容。"},
            {"rule": "关键结论可追踪", "passed": True, "evidence": "段落均关联了Trace链接。"},
        ]
        return base

    def _handle_integration_critic(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        csecs = envelope.get("payload", {}).get("candidate_sections", [])
        base["result"]["verdict"] = "ACCEPT"
        base["result"]["terminology_checks"] = [
            {"term": self._domain_term(envelope), "consistent": True, "sections": [item.get("section_id") for item in csecs]}
        ]
        base["result"]["mapping_checks"] = [
            {"mapping_type": "OBJECTIVE_TO_WORK_PACKAGE", "source_id": "item-obj-1", "target_ids": ["item-k1", "item-k2", "item-k3"], "complete": True}
        ]
        return base

    def _handle_final_confidentiality_review(self, base: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
        base["result"]["review_outcome"] = "READY_FOR_HUMAN_REVIEW"
        base["result"]["recipient_fit"] = "FIT"
        return base

    def _source_ref(self, idx: int, item: dict[str, Any]) -> dict[str, Any]:
        title = str(item.get("title") or "公开来源")
        url = str(item.get("url") or "https://example.invalid")
        publisher = str(item.get("publisher") or item.get("venue") or "公开发布机构")
        year = str(item.get("published_at") or item.get("year") or "")
        source_id = str(item.get("source_id") or f"public-src-{idx:03d}")
        return {"source_id": source_id, "source_type": "PUBLIC_SOURCE", "document_version_id": None, "section_id": None, "span_start": None, "span_end": None, "quoted_text": f"{title} | {publisher} | {year} | {url}", "source_hash": sha256_text(title + url), "authority_rank": int(item.get("authority_rank") or (70 if publisher == "arXiv" else 80)), "security_level": "PUBLIC"}

    def _public_sources(self) -> list[dict[str, Any]]:
        return LOGISTICS_REF_CATALOG

    def _section_outline(self, title: str) -> list[str]:
        mapping = {
            "项目概述": ["概述项目定位", "说明应用场景", "总结主要研究任务", "说明预期成果"],
            "研究背景与意义": ["阐述业务背景", "说明痛点问题", "论证研究意义", "总结项目价值"],
            "国内外研究现状": ["梳理国外研究", "梳理国内研究", "比较差距", "提出本项目切入点"],
            "需求分析": ["梳理用户需求", "分解业务流程", "归纳约束条件", "提炼核心能力需求"],
            "研究目标": ["总体目标", "分目标1", "分目标2", "分目标3"],
            "研究内容": ["内容总体结构", "内容一", "内容二", "内容三"],
            "关键技术": ["关键技术总述", "技术1", "技术2", "技术3", "技术耦合关系"],
            "技术路线": ["路线总览", "阶段一", "阶段二", "阶段三", "阶段衔接"],
            "总体架构设计": ["架构设计思想", "层次结构", "模块关系", "部署形态"],
            "智能体协同机制": ["角色划分", "消息机制", "协同闭环", "异常处理"],
            "数据与知识工程": ["数据源设计", "知识图谱", "RAG与记忆", "治理与更新机制"],
            "评估与验证方案": ["验证目标", "试验环境", "评估指标", "场景设计", "结果判据"],
            "创新点": ["方法创新", "架构创新", "工程创新"],
            "预期成果": ["系统成果", "方法成果", "数据与文档成果", "应用成果"],
            "进度计划": ["阶段划分", "第一阶段", "第二阶段", "第三阶段", "里程碑"],
            "预算与经费": ["预算原则", "经费分配", "使用说明", "绩效关联"],
            "研究基础与保障条件": ["研究基础", "团队能力", "已有平台", "组织保障"],
            "风险分析与对策": ["技术风险", "数据风险", "工程风险", "管理风险", "应对机制"],
            "伦理、安全与边界": ["伦理边界", "数据安全", "人工审批", "系统边界"],
            "参考文献": ["参考文献清单"],
        }
        return mapping.get(title, ["本节总述", "核心论证一", "核心论证二", "小结"])

    def _section_blocks(self, title: str) -> list[str]:
        t = title
        if t == "项目概述":
            return self._generic_blocks(t, [
                ("项目定位", "本项目面向大型活动保障、园区运营与应急保供等复杂场景，研究一套具备任务理解、资源匹配、动态调度和闭环评估能力的后勤保障智能体系统。该系统以业务流程为主线，以多智能体协同为核心，以知识增强和工具调用为支撑，形成“任务受理—方案生成—执行监控—异常处置—效果评估”的完整闭环。"),
                ("核心目标", "项目拟在三年内形成可运行的后勤保障智能体原型系统，突破任务语义建模、多智能体协同编排、时变约束下的资源调度与低扰动重规划等关键问题，建立覆盖研究、开发、测试和试运行的完整技术路径。"),
                ("主要成果", "预期成果包括总体架构设计方法、关键算法组件、原型系统、样例数据集、指标体系、测试评估报告以及项目管理与运维文档。通过原型系统验证，预期将方案生成时间缩短50%以上，并将异常响应时延降低30%以上。"),
            ])
        if t == "研究背景与意义":
            return self._generic_blocks(t, [
                ("背景分析", "随着业务场景复杂化和任务节奏加快，传统依赖人工经验的后勤保障流程面临信息碎片化、资源调配不及时、异常处置协同效率低等问题。与此同时，大模型、知识图谱和工作流编排技术快速演进，为构建具备理解、计划和执行能力的后勤保障智能体提供了新的技术基础。"),
                ("现实痛点", "首先，任务需求常以自然语言、表格和临时通知等多种形式到达，导致语义口径不统一；其次，资源台账、库存、人员班次、车辆路线等信息分散在不同系统中，难以形成统一视图；再次，执行过程中存在临时变更、道路拥堵、供应中断等不确定性，要求系统具备实时重规划能力。"),
                ("研究意义", "从理论层面看，本项目有助于推动智能体系统在复杂约束决策、知识增强协同与人机共驾流程中的应用研究；从工程层面看，本项目能够形成面向真实业务的后勤保障智能化基础能力，为后续在更广泛的资源保障与调度场景中推广打下基础。"),
            ])
        if t == "国内外研究现状":
            blocks = [
                "[[H2]]1. 智能体系统研究进展",
                self._long_para("国外研究普遍将大模型智能体划分为感知、规划、执行、记忆和反思等能力模块，代表性工作包括ReAct、Toolformer、AutoGen、CAMEL与Voyager等。这些研究证明，通过链式思维、工具调用和多角色协作，可以显著提升复杂任务的完成率，但在领域知识约束、长期稳定性和业务治理方面仍存在不足。"),
                self._long_para("国内研究近年来更加关注将大模型与业务工作流、知识库、表格处理和决策支持系统相结合。在政务、金融、制造、供应链等领域，涌现出一批面向流程自动化与智能问答的应用探索，但真正具备多智能体协同、动态调度和可审计治理能力的研究仍然较少。"),
                "[[H2]]2. 后勤与供应链智能化研究进展",
                self._long_para("在物流与供应链方向，动态车辆路径规划、库存优化、需求预测、控制塔与数字孪生等研究已经形成较为成熟的方法体系。近年来，研究热点从静态优化逐步转向实时感知、事件驱动重规划和人机协同决策。学界提出了多种结合运筹优化与机器学习的方法，但多数方案对非结构化任务输入、复杂流程协同和解释性支持不足。"),
                self._long_para("同时，RAG、知识图谱和企业流程自动化的结合逐渐成为工程实践趋势。公开研究表明，知识图谱适合管理任务、物资、地点、设备、规则和指标之间的关联关系，RAG适合为大模型提供可控外部知识，工作流引擎适合实现过程编排和人工审批。"),
                "[[H2]]3. 现有研究不足与本项目切入点",
                self._long_para("总体来看，现有研究存在三类不足：一是缺乏面向后勤保障全流程的统一智能体架构，常常仅覆盖问答、预测或调度单点能力；二是多智能体协同缺少严格的职责边界、日志审计和人工门禁；三是针对业务异常的低扰动重规划能力尚不完善。针对上述问题，本项目提出以知识增强、多智能体协同和动态重规划为核心的后勤保障智能体总体方案。"),
                "[[TABLE]]| 研究方向 | 代表方法 | 主要优势 | 局限性 |\n|---|---|---|---|\n| 大模型智能体 | ReAct/AutoGen/CAMEL | 任务分解与协同能力强 | 领域约束弱、稳定性不足 |\n| 物流优化 | VRP/排程/库存优化 | 约束处理成熟 | 非结构化输入处理弱 |\n| RAG与知识图谱 | 检索增强/图谱推理 | 提升知识可控性与解释性 | 更新维护成本较高 |\n| 数字孪生 | 仿真与状态映射 | 适合监控与预演 | 接入成本较高 |",
            ]
            blocks.extend(self._reference_annotation([1,2,3,4,5,6,7,8,9,10,11,12]))
            return blocks
        if t == "需求分析":
            return self._generic_blocks(t, [
                ("业务场景", "项目聚焦大型活动保障、园区运维补给和应急保供三类场景。三类场景的共同点是任务到达频繁、资源类型多、时效要求高、约束复杂，并且需要跨岗位协作与留痕审计。"),
                ("需求分解", "从业务流程看，后勤保障智能体至少需要具备任务受理、需求解析、资源匹配、方案编制、执行监控、异常处置和效果评估七类核心能力；从工程实现看，还需具备权限控制、日志留存、配置治理、模型观测和人工审批等支撑能力。"),
                ("约束条件", "系统需兼顾数据异构性、流程复杂性、时间敏感性和结果可解释性。在数据侧，需要同时处理结构化台账和非结构化通知；在流程侧，需要支持工作流串并行切换与人工审核；在执行侧，需要处理车辆、物资、人员和场地等多维约束。"),
                ("能力指标", "需求指标包括方案生成时间、调度成功率、异常重规划时延、解释完整性、日志完备性、人工审批覆盖率等。上述指标将作为系统设计与测试评估的重要依据。"),
            ])
        if t == "研究目标":
            return self._generic_blocks(t, [
                ("总体目标", "构建面向复杂后勤场景的智能体系统总体架构，形成一套支持任务理解、资源调配、执行监控和闭环评估的关键技术体系与原型系统。"),
                ("目标一", "形成统一的任务—资源—规则—指标知识建模方法，实现自然语言任务输入与结构化业务对象之间的可靠映射。"),
                ("目标二", "形成多智能体工作流编排方法，实现Planner、Researcher、Executor、Critic、Gatekeeper等角色的协同运行与人工可控介入。"),
                ("目标三", "形成动态资源调度和低扰动重规划方法，在任务变化和异常事件发生时快速给出可执行替代方案。"),
                ("目标四", "构建原型系统与评估框架，在典型场景中验证技术有效性、稳定性和工程可落地性。"),
            ])
        if t == "研究内容":
            return self._generic_blocks(t, [
                ("总体框架", "研究内容按照“基座能力—核心决策—闭环验证”三个层次组织。基座能力侧重知识建模、数据治理和智能体编排；核心决策侧重资源调度、异常处置和评估优化；闭环验证侧重原型实现、场景验证和指标评测。"),
                ("内容一", "研究任务语义理解与知识表示方法，围绕任务对象、资源对象、时空约束、执行规则和评估指标构建项目知识图谱及其更新机制。"),
                ("内容二", "研究多智能体协同工作流与工具调用框架，解决复杂任务分解、过程控制、结果审查、日志追踪和人工审批衔接等问题。"),
                ("内容三", "研究资源调度与动态重规划方法，重点处理任务增减、资源异常、时效冲突和多目标权衡问题。"),
                ("内容四", "研究原型系统实现与评估方法，建立从需求到验证的完整技术路线和实验体系。"),
            ])
        if t == "关键技术":
            blocks = [
                "[[H2]]1. 关键技术总体说明",
                self._long_para("本项目聚焦五项关键技术：任务语义理解与知识建模、公开研究辅助与检索增强、多智能体编排与协同治理、资源调度与低扰动重规划、可观测性与安全审计。这些技术既相互独立又彼此耦合，共同决定系统的有效性和可落地性。"),
                "[[H2]]2. 任务语义理解与知识建模",
                self._long_para("首先，系统需将自然语言任务通知解析为结构化对象，包括任务类型、服务对象、时间窗口、地点、需求量、优先级和约束条件等。其次，结合知识图谱将资源台账、流程模板、规则库和历史案例统一组织，提供可检索、可推理、可解释的业务知识底座。"),
                "[[H2]]3. 多智能体协同与治理",
                self._long_para("本项目采用Planner—Researcher—Writer—Critic—Gatekeeper的多智能体协作模式。Planner负责分解任务和组织工作流，Researcher负责公开资料检索与综合，Writer负责方案撰写与内容生成，Critic负责一致性和质量审查，Gatekeeper负责敏感性、权限与人工审批。该模式能够兼顾自动化效率与治理可控性。"),
                "[[H2]]4. 动态资源调度与低扰动重规划",
                self._long_para("在执行侧，系统需同时考虑任务优先级、资源类型匹配、时空约束、成本与稳定性等指标。针对突发需求或资源异常，系统采用低扰动重规划策略，优先在局部调整和最小变更成本下恢复可执行方案，从而减少对既有执行计划的冲击。"),
                "[[H2]]5. 关键技术关系图",
                f"[[FIGURE]]{(self.figure_dir/'图1_后勤保障智能体逻辑结构图.png').as_posix()}|图1 后勤保障智能体逻辑结构图|15",
                "[[TABLE]]| 关键技术 | 主要功能 | 关键输入 | 关键输出 | 评价指标 |\n|---|---|---|---|---|\n| 语义理解与知识建模 | 解析任务、构建图谱 | 通知、规则、台账 | 结构化任务对象、知识子图 | 抽取准确率、覆盖率 |\n| 多智能体编排 | 组织角色协作与门禁 | 任务包、流程模板 | 过程状态、阶段结果 | 成功率、人工介入比 |\n| 动态调度与重规划 | 资源匹配与异常处置 | 任务需求、资源状态 | 执行计划、替代方案 | 方案质量、重规划时延 |\n| 可观测与安全审计 | 留痕、审查与复盘 | Prompt、Trace、日志 | 审计包、风险提示 | 完整性、可追溯性 |",
            ]
            blocks.extend(self._reference_annotation([1,2,3,4,5,6,8,9,10,11,12,13,14,15]))
            return blocks
        if t == "技术路线":
            blocks = [
                "[[H2]]1. 技术路线总体说明",
                self._long_para("本项目技术路线遵循“需求牵引—知识建模—智能体编排—资源调度—系统验证”的逻辑。从需求出发，首先明确业务流程与约束；随后构建知识底座和数据治理机制；在此基础上设计多智能体协同工作流和调度优化引擎；最终通过原型系统和场景测试验证方法有效性。"),
                f"[[FIGURE]]{(self.figure_dir/'图2_后勤保障智能体技术路线图.png').as_posix()}|图2 后勤保障智能体技术路线图|15",
                "[[H2]]2. 阶段一：需求与知识基座",
                self._long_para("阶段一重点完成需求分析、对象建模、指标体系设计与知识图谱构建。需要沉淀任务词表、资源分类、规则模板、评价指标和历史案例，并形成统一的数据治理机制，为后续模型调用与工作流编排提供高质量上下文。"),
                "[[H2]]3. 阶段二：智能体编排与调度引擎",
                self._long_para("阶段二重点研究多智能体工作流引擎和调度优化引擎。前者解决角色分工、输入输出契约、门禁审批和日志追踪；后者解决任务分配、路径规划、执行协调和异常重规划等问题。两者通过共享状态、工具接口和指标反馈进行闭环耦合。"),
                "[[H2]]4. 阶段三：评估、迭代与示范验证",
                self._long_para("阶段三在典型场景中开展集成测试和示范验证，构建从离线案例回放到在线试运行的分层评估体系。通过多轮实验，持续优化模型提示词、知识库组织、调度规则和人工门禁设置，逐步提升系统的准确性、稳定性和可解释性。"),
            ]
            return blocks
        if t == "总体架构设计":
            blocks = [
                "[[H2]]1. 总体架构",
                self._long_para("系统总体架构分为交互层、编排层、知识层、执行层和治理层。交互层负责接收用户任务、展示过程状态与结果；编排层负责工作流组织、多智能体协同和状态管理；知识层负责知识图谱、检索增强和案例库；执行层负责规划求解、工具调用和监控反馈；治理层负责权限、审计与风险控制。"),
                f"[[FIGURE]]{(self.figure_dir/'图1_后勤保障智能体逻辑结构图.png').as_posix()}|图3 后勤保障智能体总体架构图|15",
                "[[H2]]2. 模块划分",
                self._long_para("交互层包含任务受理、看板展示、人工审批和结果确认等模块；编排层包含任务编排引擎、Prompt执行器、上下文构建器和状态机；知识层包含知识图谱、检索引擎、事实库与模板库；执行层包含计划求解器、重规划器、资源匹配器和评估器；治理层包含安全分类、隐私脱敏、日志追踪、指标审查和版本管理。"),
                "[[TABLE]]| 层次 | 模块 | 主要职责 |\n|---|---|---|\n| 交互层 | 任务受理/审批/看板 | 负责用户交互与人工确认 |\n| 编排层 | 工作流引擎/Prompt执行器 | 负责多智能体流程组织 |\n| 知识层 | 图谱/RAG/案例库 | 负责知识支撑与检索 |\n| 执行层 | 调度求解/重规划/评估 | 负责方案生成与调整 |\n| 治理层 | 安全/日志/审计/配置 | 负责合规与可观测性 |",
            ]
            return blocks
        if t == "智能体协同机制":
            blocks = [
                "[[H2]]1. 角色定义",
                self._long_para("系统将核心角色划分为Planner、Researcher、Writer、Critic、Executor和Gatekeeper。其中Planner负责分解任务、制定执行顺序；Researcher负责公开资料补充；Writer负责结构化内容生成；Critic负责一致性与质量审查；Executor负责具体求解与工具调用；Gatekeeper负责安全分类、人工审批和出口控制。"),
                "[[H2]]2. 协同流程",
                self._long_para("在一次完整运行中，Planner首先根据项目目标和输入材料生成修订计划；随后，Researcher完成脱敏后的公开检索与综合；Writer根据计划和知识上下文逐节生成内容；Critic对每节以及全局进行审查；最后由Gatekeeper完成保密与导出审批。整个过程要求所有输入、输出、Trace和Gate决策均可追踪。"),
                f"[[FIGURE]]{(self.figure_dir/'图3_后勤保障智能体关键执行流流程图.png').as_posix()}|图4 后勤保障智能体关键执行流流程图|16",
                self._long_para("这种协同机制的优势在于通过明确的角色职责和契约化输入输出，减少单模型端到端生成的不确定性；通过Critic和Gatekeeper的双重审查机制，提升结果可靠性；通过日志与Trace留存，为后续调优和问责提供依据。"),
            ]
            return blocks
        if t == "数据与知识工程":
            return self._generic_blocks(t, [
                ("数据源设计", "数据来源包括任务通知、资源台账、执行日志、规则文档、外部公开资料和历史案例。为提高可用性，项目对各类数据进行统一标准化和元数据治理，形成面向智能体运行的上下文视图。"),
                ("知识图谱构建", "知识图谱围绕任务、物资、车辆、人员、地点、时间窗、规则、指标和案例九类核心对象展开，定义对象属性和关联关系，支持查询、校验和推理。图谱将与事实库、模板库和向量检索库配合工作，以兼顾结构化约束和语义检索能力。"),
                ("RAG与记忆机制", "RAG机制用于为Researcher、Writer和Critic提供可控外部知识，避免无依据生成；记忆机制则用于沉淀高质量历史方案、异常处理经验和人工修订模式，形成可复用的经验资产。"),
                ("治理机制", "数据与知识工程还需支持版本管理、质量评估、冲突检测与增量更新，确保系统在长期迭代中保持一致性和可追溯性。"),
            ])
        if t == "评估与验证方案":
            blocks = [
                "[[H2]]1. 评估目标与原则",
                self._long_para("评估工作围绕有效性、效率、稳定性、可解释性和安全性五个维度展开，遵循“离线验证—联调测试—场景试运行”的渐进式原则。离线阶段重点验证知识抽取、任务解析和方案生成质量；联调阶段验证多智能体协同和调度闭环；试运行阶段验证在真实业务节奏下的稳定性和人工接纳度。"),
                "[[H2]]2. 评估指标体系",
                "[[TABLE]]| 维度 | 指标 | 说明 | 目标值 |\n|---|---|---|---|\n| 有效性 | 方案可执行率 | 输出方案满足约束并可执行 | ≥90% |\n| 效率 | 方案生成时间 | 从任务输入到初稿输出的耗时 | ≤10分钟 |\n| 稳定性 | 重规划成功率 | 异常情况下生成可替代方案的比例 | ≥85% |\n| 可解释性 | Trace覆盖率 | 关键结论关联来源的比例 | ≥95% |\n| 安全性 | 审计留痕完整率 | Prompt/输出/审批日志的留存完整率 | 100% |",
                "[[H2]]3. 场景设计",
                self._long_para("验证场景包括常态保障、临时加急、资源受限和异常处置四类。常态保障侧重标准流程效率；临时加急侧重任务插入与优先级调整；资源受限侧重多目标权衡；异常处置侧重低扰动重规划与人工干预机制。"),
                self._long_para("评估过程中，将同时记录方案质量指标和过程指标。前者包括满足率、成本、时效和稳定性；后者包括Prompt调用次数、人工介入节点、平均等待时间、审查通过率和Trace覆盖率。"),
            ]
            return blocks
        if t == "创新点":
            return self._generic_blocks(t, [
                ("方法创新", "项目提出将任务语义理解、知识图谱、RAG和多智能体编排融合到统一的后勤保障智能体框架中，突破传统单点算法难以覆盖全流程的问题。"),
                ("架构创新", "通过Planner—Researcher—Writer—Critic—Gatekeeper协作模式，形成契约化、可追踪、可审查的智能体流程架构，实现从生成到治理的一体化闭环。"),
                ("工程创新", "项目在动态调度与低扰动重规划、日志留痕、人工门禁和评估看板等方面实现系统级协同设计，强调可落地性和持续演进能力。"),
            ])
        if t == "预期成果":
            return self._generic_blocks(t, [
                ("系统成果", "完成后勤保障智能体原型系统1套，覆盖任务受理、方案生成、执行监控、异常处置和效果评估等功能。"),
                ("方法成果", "形成知识建模、智能体编排、资源调度与重规划等方法成果，沉淀算法设计文档、Prompt工程规范和评估规则。"),
                ("数据与文档成果", "形成样例数据集、测试案例库、知识模板库、系统手册、用户操作手册和运维手册等配套文档。"),
                ("应用成果", "在典型场景中完成验证并形成示范应用报告，为后续推广提供依据。"),
            ])
        if t == "进度计划":
            blocks = [
                "[[TABLE]]| 阶段 | 时间 | 主要任务 | 阶段成果 |\n|---|---|---|---|\n| 第一阶段 | 第1-6个月 | 需求分析、指标体系、知识建模与样例库建设 | 需求说明书、知识模型、初始数据集 |\n| 第二阶段 | 第7-14个月 | 智能体工作流引擎与RAG组件开发 | 编排引擎原型、检索组件、日志框架 |\n| 第三阶段 | 第15-24个月 | 资源调度、重规划与评估模块开发 | 调度算法、评估指标库、联调版本 |\n| 第四阶段 | 第25-30个月 | 场景测试、性能优化与治理完善 | 场景测试报告、优化方案 |\n| 第五阶段 | 第31-36个月 | 试运行、总结验收与成果凝练 | 原型系统、总结报告、成果文档 |",
                self._long_para("项目实施过程中将设置阶段性里程碑，并对需求、开发、联调、验证和验收活动分别制定完成判据。每个阶段均安排评审节点和风险复盘，以保证项目按计划推进并及时纠偏。"),
            ]
            return blocks
        if t == "预算与经费":
            blocks = [
                "[[TABLE]]| 经费科目 | 金额（万元） | 说明 |\n|---|---|---|\n| 设备与软件 | 18 | 服务器、开发工具和测试环境 |\n| 数据与材料 | 6 | 样例数据构建、资料购买与标注 |\n| 研发劳务 | 22 | 算法开发、工程实现与测试 |\n| 试验与差旅 | 8 | 现场调研、场景测试与交流 |\n| 专家咨询与出版 | 4 | 评审、咨询、成果整理 |\n| 预备费 | 2 | 不可预见支出 |",
                self._long_para("预算编制遵循目标导向、重点突出、结构合理和绩效可衡量的原则。设备与软件经费主要用于原型验证环境建设；数据与材料经费主要用于样例库构建和资料整理；研发劳务经费保障算法和工程开发任务；试验与差旅经费保障场景测试与调研；咨询与出版经费支撑评审和成果整理。"),
                self._long_para("项目将建立经费执行台账，按阶段开展预算执行情况评估，确保经费使用与研发任务同步推进，避免投入与产出脱节。"),
            ]
            return blocks
        if t == "研究基础与保障条件":
            return self._generic_blocks(t, [
                ("研究基础", "项目团队在智能体系统、工作流编排、知识图谱、RAG、优化调度和企业级应用开发等方面具有较好的研究与工程基础，具备将方法研究转化为原型系统的能力。"),
                ("团队能力", "团队成员覆盖算法研究、后端工程、前端交互、测试验证和项目管理等角色，能够支撑从需求分析到系统交付的完整研发链路。"),
                ("已有平台", "团队已具备基础开发环境、测试环境和样例数据资源，并积累了工作流引擎、检索组件、日志监控和权限控制等可复用模块，为项目开展提供了良好条件。"),
                ("组织保障", "项目将采用周例会、阶段评审、问题台账、版本管理和风险复盘等方式组织实施，确保研发过程透明、可控、可追踪。"),
            ])
        if t == "风险分析与对策":
            return self._generic_blocks(t, [
                ("技术风险", "技术风险主要包括大模型输出稳定性不足、知识库更新滞后和多智能体协同开销偏高等问题。应对策略是引入结构化约束、Critic审查、缓存机制和回退策略。"),
                ("数据风险", "数据风险主要包括数据质量不一致、样例覆盖不足和外部公开资料质量参差不齐。应对策略是建立数据分级、清洗校验、来源可信度标注和人工复核机制。"),
                ("工程风险", "工程风险主要包括模块耦合度高、接口变化频繁和部署环境不一致。应对策略是采用契约化Schema、分层架构、自动化测试和灰度验证。"),
                ("管理风险", "管理风险主要包括需求变更频繁、跨角色协同效率低和阶段目标偏移。应对策略是通过里程碑评审、问题清单和复盘机制进行治理。"),
            ])
        if t == "伦理、安全与边界":
            return self._generic_blocks(t, [
                ("伦理边界", "系统定位为辅助决策与流程自动化工具，不直接替代最终责任主体。涉及资源调配、异常处置和关键外发等环节必须保留人工确认机制。"),
                ("数据安全", "系统实行分级分类处理，对外公开检索前必须进行任务包脱敏；Prompt、响应、Trace和日志按照统一安全标签管理，并在安全域内留存。"),
                ("人工审批", "在公开检索、关键内容确认、终审与导出等步骤设置人工门禁，确保高风险动作均具备明确责任归属和可复盘记录。"),
                ("系统边界", "项目重点研究智能体辅助决策、流程编排与调度闭环，不覆盖底层数据采集终端和外部业务系统深度改造。"),
            ])
        if t == "参考文献":
            refs = [
                "[1] Yao S, Zhao J, Yu D, et al. ReAct: Synergizing Reasoning and Acting in Language Models. ICLR, 2023.",
                "[2] Schick T, Dwivedi-Yu J, Dessi R, et al. Toolformer: Language Models Can Teach Themselves to Use Tools. NeurIPS, 2023.",
                "[3] Wu Q, Bansal G, Zhang J, et al. AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation. Microsoft Research, 2023.",
                "[4] Li G, Hammoud H A A K, Itani H, et al. CAMEL: Communicative Agents for Mind Exploration of Large Scale Language Model Society. NeurIPS Workshop, 2023.",
                "[5] Wang G, Xie C, Li Z, et al. Voyager: An Open-Ended Embodied Agent with Large Language Models. arXiv preprint arXiv:2305.16291, 2023.",
                "[6] Xi Z, Chen W, Guo X, et al. The Rise and Potential of Large Language Model Based Agents: A Survey. arXiv preprint arXiv:2309.07864, 2024.",
                "[7] Lewis P, Perez E, Piktus A, et al. Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks. NeurIPS, 2020.",
                "[8] Singh R, Bansal R. Knowledge Graphs for Logistics and Supply Chain Management: A Survey. Computers & Industrial Engineering, 2023.",
                "[9] Min H. Digital Twins for Smart Logistics and Supply Chain Management. IEEE Access, 2022.",
                "[10] Pillac V, Gendreau M, Guéret C, et al. A Review of Dynamic Vehicle Routing Problems. European Journal of Operational Research, 2021.",
                "[11] Dellermann D, et al. Human-AI Collaboration in Decision Making. ACM Computing Surveys, 2023.",
                "[12] Shankar V, et al. Observability for LLM Applications. Technical Whitepaper, 2024.",
                "[13] Supply Chain Control Tower and AI Orchestration. Gartner/Industry Whitepaper, 2024.",
                "[14] Chen J, et al. Enterprise Workflow Automation with LLM Agents. arXiv preprint, 2024.",
                "[15] Wang X, et al. Benchmarking and Evaluating LLM Agents. arXiv preprint, 2024.",
                "[16] Zhang Y, et al. GraphRAG: Enhancing Retrieval-Augmented Generation with Knowledge Graphs. arXiv preprint, 2024.",
                "[17] Sun H, et al. Multi-Agent Planning and Coordination for Complex Tasks. Information Sciences, 2023.",
                "[18] Li X, et al. A Survey of AI for Operations and Supply Chain Management. International Journal of Production Research, 2024.",
                "[19] Sarker I H. Workflow Automation and AI Orchestration: Concepts and Applications. Future Generation Computer Systems, 2024.",
                "[20] Liu K, et al. RAG Systems in Enterprise Applications: A Survey. arXiv preprint, 2024.",
            ]
            blocks = ["[[H2]]参考文献"]
            blocks.extend(refs)
            return blocks
        return self._generic_blocks(t, [("本节说明", f"《{t}》章节围绕项目目标开展论证。"), ("主要内容", "本节结合项目场景、技术路线与实施计划展开详细说明。")])

    def _generic_blocks(self, title: str, parts: list[tuple[str, str]]) -> list[str]:
        blocks: list[str] = []
        for idx, (subtitle, body) in enumerate(parts, 1):
            blocks.append(f"[[H2]]{idx}. {subtitle}")
            blocks.append(self._long_para(body))
            blocks.append(self._long_para(body + " 进一步看，该问题不仅涉及模型能力本身，还涉及流程设计、数据组织、角色协作和评价反馈等工程化因素。因此，本项目将通过结构化建模、分层工作流和迭代验证机制，把上述要求转化为可实施、可检验、可复盘的研发任务。"))
        return blocks

    @staticmethod
    def _reference_annotation(indices: list[int]) -> list[str]:
        return [f"上述分析分别参考文献[{','.join(str(i) for i in indices[:len(indices)//2])}]以及[{','.join(str(i) for i in indices[len(indices)//2:])}]。"]

    @staticmethod
    def _long_para(text: str) -> str:
        extra = (
            "从系统工程角度看，单点能力并不能直接转化为稳定可用的业务价值，必须通过输入治理、角色协同、过程控制、结果审查和闭环评估形成整体能力。"
            "因此，本项目将坚持“结构化约束+知识增强+多智能体协同+人工门禁”的技术原则，避免将复杂业务问题简化为单轮问答或单模型端到端生成。"
            "与此同时，项目还将把性能指标、稳定性指标、可解释性指标和安全指标统一纳入评估框架，确保系统在效率提升的同时满足治理与审计要求。"
            "进一步地，项目将通过原型迭代把抽象方法落到可执行的模块、接口与测试用例之上，使研究内容既能够支撑学术论证，也能够支撑工程实施。"
            "在每个关键节点上，系统都会记录输入、输出、版本、责任角色与审查结论，并通过统一的Trace机制与评价指标进行关联，确保后续可以对生成过程进行审计、复盘和优化。"
            "这种将业务逻辑、知识支撑、工作流控制与评价反馈统一设计的方法，有助于提升后勤保障智能体在复杂场景中的稳定性、可迁移性和持续演进能力。"
        )
        return text + extra
