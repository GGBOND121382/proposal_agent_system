from __future__ import annotations

import copy

from app.context_base import ContextBuilder
from app.workflow_repair import repair_override_key
from app.workflows import technical_retry_key


class NullDB:
    def fetchone(self, sql, params=()):
        return None

    def fetchall(self, sql, params=()):
        return []


class PermissivePack:
    relation_matrix = {"version": "2.0", "allowed_relations": []}

    def validate(self, prompt_id, kind, value):
        return []

    def inlined_schema(self, prompt_id, kind):
        return {
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "properties": {
                        "argument_graph": {},
                        "fact_context": {},
                    },
                    "additionalProperties": {},
                }
            },
            "additionalProperties": {},
        }


class FixtureContextBuilder(ContextBuilder):
    def __init__(self, *, graph, internal_facts, public_claims):
        super().__init__(NullDB(), PermissivePack())
        self.graph = copy.deepcopy(graph)
        self.internal_facts = copy.deepcopy(internal_facts)
        self.public_claims = copy.deepcopy(public_claims)

    def _result(
        self,
        project_id,
        prompt_id,
        key=None,
        *,
        workflow_id=None,
        exact_workflow=False,
    ):
        if prompt_id == "P-ARGUMENT-ARCHITECTURE":
            value = {"argument_architecture": copy.deepcopy(self.graph)}
        elif prompt_id == "P-FACT-EXTRACT":
            value = {"fact_candidates": copy.deepcopy(self.internal_facts)}
        else:
            value = None
        if key and isinstance(value, dict):
            return copy.deepcopy(value.get(key))
        return copy.deepcopy(value)

    def _approved_public_claims(self, project_id, *, workflow_id=None):
        return copy.deepcopy(self.public_claims)

    def _content_candidates(self, project_id, workflow_id=None, *, section_results=None):
        return []


def _current_proposal_document():
    return {
        "document_id": "doc-current",
        "document_version_id": "docv-current-1",
        "document_role": "CURRENT_PROPOSAL",
        "title": "当前申请书",
        "document_hash": "a" * 64,
        "authority_rank": 85,
        "security_level": "INTERNAL",
        "sections": [
            {
                "section_id": "sec-current-1",
                "title": "研究内容",
                "level": 1,
                "text": "当前申请书明确列出了四项根因、研究问题和研究目标。",
                "text_hash": "b" * 64,
            }
        ],
    }


def _existing_argument_graph(source_ref):
    nodes = []
    edges = []
    for index in range(1, 5):
        nodes.extend(
            [
                {
                    "node_id": f"RC-00{index}",
                    "node_type": "ROOT_CAUSE",
                    "statement": f"根因 {index}",
                    "source_refs": [copy.deepcopy(source_ref)],
                },
                {
                    "node_id": f"RQ-{index}",
                    "node_type": "RESEARCH_QUESTION",
                    "statement": f"研究问题 {index}",
                    "source_refs": [copy.deepcopy(source_ref)],
                },
                {
                    "node_id": f"OBJ-{index}",
                    "node_type": "OBJECTIVE",
                    "statement": f"研究目标 {index}",
                    "source_refs": [copy.deepcopy(source_ref)],
                },
            ]
        )
        edges.extend(
            [
                {
                    "edge_id": f"edge-rq-rc-{index}",
                    "source_node_id": f"RQ-{index}",
                    "target_node_id": f"RC-00{index}",
                    "relation_type": "ADDRESSES",
                },
                {
                    "edge_id": f"edge-obj-rc-{index}",
                    "source_node_id": f"OBJ-{index}",
                    "target_node_id": f"RC-00{index}",
                    "relation_type": "RESPONDS_TO",
                },
            ]
        )
    return {"graph_id": "graph-current", "nodes": nodes, "edges": edges}


def test_current_proposal_source_ref_remains_authoritative_and_versioned():
    builder = ContextBuilder(NullDB(), PermissivePack())
    document = _current_proposal_document()
    section = document["sections"][0]

    source_ref = builder._source_ref(document, section)

    assert source_ref["source_type"] == "CURRENT_PROPOSAL"
    assert source_ref["document_version_id"] == "docv-current-1"
    assert source_ref["section_id"] == "sec-current-1"
    assert source_ref["source_hash"] == "b" * 64
    assert source_ref["authority_rank"] == 85


def test_existing_argument_graph_and_fact_sources_survive_context_build():
    document = _current_proposal_document()
    source_ref = ContextBuilder(NullDB(), PermissivePack())._source_ref(
        document, document["sections"][0]
    )
    graph = _existing_argument_graph(source_ref)
    internal_claim = {
        "claim_id": "claim-internal-001",
        "claim_text": "当前申请书确认了四项研究根因。",
        "knowledge_status": "CONFIRMED",
        "source_refs": [copy.deepcopy(source_ref)],
    }
    public_source = {
        "source_id": "public-src-001",
        "source_type": "PUBLIC_SOURCE",
        "document_version_id": None,
        "section_id": None,
        "span_start": None,
        "span_end": None,
        "quoted_text": "公开资料支持该一般性结论。",
        "source_hash": "c" * 64,
        "authority_rank": 50,
        "security_level": "PUBLIC",
    }
    public_claim = {
        "claim_id": "claim-public-001",
        "claim_text": "公开资料中的一般性事实。",
        "knowledge_status": "SUPPORTED",
        "source_refs": [public_source],
    }
    builder = FixtureContextBuilder(
        graph=graph,
        internal_facts=[internal_claim],
        public_claims=[public_claim],
    )
    envelope = {"payload": {"argument_graph": {}, "fact_context": []}}
    project = {
        "id": "project-current",
        "name": "项目",
        "description": "项目描述",
        "security_level": "INTERNAL",
    }

    builder._apply_common_payload(
        envelope,
        "P-CONTEXT-REGRESSION",
        project,
        {},
        [document],
        "d" * 64,
        {},
        None,
    )

    actual_graph = envelope["payload"]["argument_graph"]
    node_ids = {item["node_id"] for item in actual_graph["nodes"]}
    edge_pairs = {
        (item["source_node_id"], item["target_node_id"])
        for item in actual_graph["edges"]
    }
    assert {f"RC-00{i}" for i in range(1, 5)} <= node_ids
    assert {f"RQ-{i:03d}" for i in range(1, 5)} <= node_ids
    assert {f"OBJ-{i:03d}" for i in range(1, 5)} <= node_ids
    assert {(f"RQ-{i:03d}", f"RC-{i:03d}") for i in range(1, 5)} <= edge_pairs
    assert {(f"OBJ-{i:03d}", f"RC-{i:03d}") for i in range(1, 5)} <= edge_pairs

    claims = {item["claim_id"]: item for item in envelope["payload"]["fact_context"]}
    assert claims["claim-internal-001"]["source_refs"][0]["source_type"] == "CURRENT_PROPOSAL"
    assert claims["claim-public-001"]["knowledge_status"] == "SUPPORTED"
    assert claims["claim-public-001"]["source_refs"][0]["source_id"] == "public-src-001"


def _objective(node_id: str) -> dict:
    return {
        "node_id": node_id,
        "node_type": "OBJECTIVE",
        "statement": f"Objective {node_id}",
        "status": "PLANNED",
        "source_refs": [],
    }


def test_current_proposal_explicit_mapping_materializes_rc_nodes_and_obj_edges():
    result = {
        "argument_architecture": {
            "graph_id": "AG-001",
            "nodes": [_objective(f"OBJ-{index:03d}") for index in range(1, 5)],
            "edges": [],
        },
        "research_design_matrix": [
            {
                "research_question_id": f"RQ-{index:03d}",
                "objective_ids": ["OBJ-001"],
                "work_package_ids": ["RC-001"],
            }
            for index in range(1, 5)
        ],
    }
    sections = [{
        "document_id": "doc-current",
        "document_version_id": "doc-version-1",
        "section_id": "sec-closed-loop",
        "security_level": "INTERNAL",
        "text": "\n".join(
            f"| `RQ-{index}` | `OBJ-{index}` | `RC-{index}` |"
            for index in range(1, 5)
        ) + "\n" + "\n".join(
            f"**`RC-{index}` Work package {index}**: definition {index}"
            for index in range(1, 5)
        ) + "\n- `BASE-2` Existing software: `UNKNOWN`",
    }]

    canonical = ContextBuilder._canonicalize_argument_result_from_sections(
        result,
        sections,
    )

    graph = canonical["argument_architecture"]
    nodes = {node["node_id"]: node for node in graph["nodes"]}
    assert {"RC-001", "RC-002", "RC-003", "RC-004", "BASE-002"} <= set(nodes)
    assert [
        row["work_package_ids"]
        for row in canonical["research_design_matrix"]
    ] == [["RC-001"], ["RC-002"], ["RC-003"], ["RC-004"]]
    assert [
        row["objective_ids"]
        for row in canonical["research_design_matrix"]
    ] == [["OBJ-001"], ["OBJ-002"], ["OBJ-003"], ["OBJ-004"]]
    assert {
        (edge["source_id"], edge["relation"], edge["target_id"])
        for edge in graph["edges"]
    } >= {
        ("OBJ-001", "DECOMPOSES_TO", "RC-001"),
        ("OBJ-002", "DECOMPOSES_TO", "RC-002"),
        ("OBJ-003", "DECOMPOSES_TO", "RC-003"),
        ("OBJ-004", "DECOMPOSES_TO", "RC-004"),
    }
    for node_id in ("RC-001", "RC-002", "RC-003", "RC-004"):
        assert nodes[node_id]["source_refs"][0]["source_type"] == "CURRENT_PROPOSAL"
        assert nodes[node_id]["source_refs"][0]["source_id"] == "doc-current"
        assert nodes[node_id]["source_refs"][0]["section_id"] == "sec-closed-loop"


def test_canonicalizer_does_not_infer_mapping_from_matching_suffixes():
    result = {
        "argument_architecture": {
            "graph_id": "AG-001",
            "nodes": [_objective("OBJ-001")],
            "edges": [],
        },
        "research_design_matrix": [{
            "research_question_id": "RQ-001",
            "objective_ids": ["OBJ-001"],
            "work_package_ids": ["RC-009"],
        }],
    }
    sections = [{
        "section_id": "sec-no-map",
        "text": "RQ-1、OBJ-1 和 RC-1 分别在不同语境中出现，但没有给出对应表。",
    }]

    canonical = ContextBuilder._canonicalize_argument_result_from_sections(
        result,
        sections,
    )

    assert canonical["research_design_matrix"][0]["work_package_ids"] == ["RC-001"]
    # The sentence contains all three identifiers and therefore is an explicit
    # mapping.  A section containing only separate definitions is tested below.
    definitions_only = [{
        "section_id": "sec-definitions",
        "text": "RQ-1 research question\nOBJ-1 objective\nRC-1 work package",
    }]
    untouched = ContextBuilder._canonicalize_argument_result_from_sections(
        result,
        definitions_only,
    )
    assert untouched["research_design_matrix"][0]["work_package_ids"] == ["RC-009"]
    assert untouched["argument_architecture"]["edges"] == []


def test_approved_public_claims_bind_by_exact_identifier_only():
    argument_result = {
        "argument_architecture": {
            "nodes": [
                {
                    "node_id": "claim-001",
                    "node_type": "CLOSEST_PRIOR_WORK",
                    "statement": "Prior work",
                    "status": "UNKNOWN",
                    "source_refs": [],
                },
                {
                    "node_id": "claim-001-extra",
                    "node_type": "CLOSEST_PRIOR_WORK",
                    "statement": "Different claim",
                    "status": "UNKNOWN",
                    "source_refs": [],
                },
            ],
        },
    }
    source_ref = {
        "source_id": "public-source-001",
        "source_type": "PUBLIC_SOURCE",
        "authority_rank": 85,
        "security_level": "PUBLIC",
    }
    bound = ContextBuilder._bind_argument_result_evidence(
        argument_result,
        [{
            "claim_id": "claim-001",
            "knowledge_status": "DOCUMENT_EXTRACTED",
            "source_refs": [source_ref],
        }],
    )

    nodes = {
        node["node_id"]: node
        for node in bound["argument_architecture"]["nodes"]
    }
    assert nodes["claim-001"]["status"] == "SUPPORTED"
    assert nodes["claim-001"]["source_refs"] == [source_ref]
    assert nodes["claim-001-extra"]["status"] == "UNKNOWN"
    assert nodes["claim-001-extra"]["source_refs"] == []


def test_repair_application_identity_is_scoped_per_section():
    assert repair_override_key(
        "P-WRITE-BLUEPRINT",
        {"active_section_id": "new-abstract"},
    ) == "section:new-abstract:P-WRITE-BLUEPRINT"
    assert repair_override_key(
        "P-WRITE-BLUEPRINT",
        {"active_section_id": "research-plan"},
    ) == "section:research-plan:P-WRITE-BLUEPRINT"


def test_technical_retry_identity_is_scoped_only_for_active_section_phase():
    state = {
        "active_section_id": "new-abstract",
        "section_progress": {
            "new-abstract": {"phase": "BLUEPRINT_CRITIC"},
        },
    }
    assert technical_retry_key(
        "5",
        state,
        is_section_step=True,
    ) == "5:new-abstract:BLUEPRINT_CRITIC"
    assert technical_retry_key(
        "5",
        state,
        is_section_step=False,
    ) == "5"
