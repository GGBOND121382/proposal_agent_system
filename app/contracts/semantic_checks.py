from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .semantic_contract import (
    RuleResponsibility,
    SemanticContract,
    get_semantic_contract,
)


@dataclass(frozen=True)
class SemanticViolation:
    """One deterministic violation emitted directly from the semantic contract."""

    rule_id: str
    responsibility: RuleResponsibility
    code: str
    category: str
    target_path: str
    description: str
    repair_instruction: str
    blocking: bool


def _violation(
    contract: SemanticContract,
    rule_id: str,
    *,
    code: str,
    category: str,
    target_path: str,
    description: str,
    repair_instruction: str,
) -> SemanticViolation:
    rule = contract.rule(rule_id)
    return SemanticViolation(
        rule_id=rule.rule_id,
        responsibility=rule.responsibility,
        code=code,
        category=category,
        target_path=target_path,
        description=description,
        repair_instruction=repair_instruction,
        blocking=rule.blocking,
    )


def check_blueprint_semantics(
    blueprint: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
    *,
    contract: SemanticContract | None = None,
) -> tuple[SemanticViolation, ...]:
    """Check only machine-decidable blueprint semantics.

    This function deliberately excludes rhetorical quality, section-profile choice,
    prose density, and template-reuse judgments. It reads every deterministic rule
    from ``SemanticContract`` and never mutates the blueprint or payload.
    """

    active = contract or get_semantic_contract()
    blueprint_value = blueprint if isinstance(blueprint, Mapping) else {}
    payload_value = payload if isinstance(payload, Mapping) else {}
    paragraphs = tuple(
        paragraph
        for paragraph in blueprint_value.get("paragraphs") or ()
        if isinstance(paragraph, Mapping)
    )
    section_contract = payload_value.get("section_contract")
    section_contract = section_contract if isinstance(section_contract, Mapping) else {}

    violations: list[SemanticViolation] = []
    contract_id = str(section_contract.get("section_contract_id") or "")
    contract_keys = tuple(
        str(value) for value in section_contract.get("unique_information_keys") or ()
    )
    paragraph_keys = tuple(str(paragraph.get("novel_content_key") or "") for paragraph in paragraphs)

    if contract_id and not all(paragraph_keys):
        violations.append(
            _violation(
                active,
                "SC-INFORMATION-KEY-HIERARCHY",
                code="QG_BLUEPRINT_MISSING_INFORMATION_IDENTITY",
                category="BLUEPRINT",
                target_path="paragraphs.novel_content_key",
                description="蓝图段落缺少新增信息键，后续无法判断章节是否推进了新内容。",
                repair_instruction="为每个段落指定属于本章节合同的novel_content_key。",
            )
        )

    if len(paragraph_keys) != len(set(paragraph_keys)):
        violations.append(
            _violation(
                active,
                "SC-INFORMATION-KEY-HIERARCHY",
                code="QG_BLUEPRINT_DUPLICATE_INFORMATION_KEYS",
                category="BLUEPRINT",
                target_path="paragraphs.novel_content_key",
                description="同一章节内多个段落复用了相同新增信息键。",
                repair_instruction="每个段落只推进一个独立信息单元，并使用唯一novel_content_key。",
            )
        )

    foreign_keys = sorted(
        key
        for key in paragraph_keys
        if key and contract_keys and not active.information_key_belongs(key, contract_keys)
    )
    if foreign_keys:
        violations.append(
            _violation(
                active,
                "SC-INFORMATION-KEY-HIERARCHY",
                code="QG_BLUEPRINT_INFORMATION_KEY_OUTSIDE_CONTRACT",
                category="BLUEPRINT",
                target_path="paragraphs.novel_content_key",
                description=f"有{len(foreign_keys)}个新增信息键不属于本章节合同。",
                repair_instruction="仅使用section_contract.unique_information_keys及其子键。",
            )
        )

    prior_keys = {
        str(key)
        for digest in payload_value.get("prior_section_digest") or ()
        if isinstance(digest, Mapping)
        for key in digest.get("new_information_keys") or ()
    }
    reused_prior = sorted(set(paragraph_keys) & prior_keys)
    if reused_prior:
        violations.append(
            _violation(
                active,
                "SC-INFORMATION-KEY-HIERARCHY",
                code="QG_BLUEPRINT_REUSES_PRIOR_INFORMATION",
                category="BLUEPRINT",
                target_path="paragraphs.novel_content_key",
                description=f"蓝图复用了前文章节的{len(reused_prior)}个信息键。",
                repair_instruction="更换为本章节独有信息键；共享背景只能通过allowed_shared_context_ids引用。",
            )
        )

    required_roles = section_contract.get("required_argument_roles") or ()
    actual_roles = {
        active.canonical_role(paragraph.get("argument_role")) for paragraph in paragraphs
    }
    missing_roles = active.missing_required_roles(required_roles, actual_roles)
    if missing_roles:
        violations.append(
            _violation(
                active,
                "SC-ARGUMENT-ROLE-COMPATIBILITY",
                code="QG_BLUEPRINT_REQUIRED_ROLES_MISSING",
                category="BLUEPRINT",
                target_path="paragraphs.argument_role",
                description=f"蓝图缺少章节合同要求的论证角色：{', '.join(missing_roles)}。",
                repair_instruction="补齐章节Profile要求的论证角色，不得用通用段落替代。",
            )
        )

    self_evidence = {
        str(paragraph.get("paragraph_id") or ""): sorted(active.self_evidence_ids(paragraph))
        for paragraph in paragraphs
        if active.self_evidence_ids(paragraph)
    }
    if self_evidence:
        violations.append(
            _violation(
                active,
                "SC-EVIDENCE-SELF-REFERENCE",
                code="QG_BLUEPRINT_SELF_EVIDENCE",
                category="EVIDENCE",
                target_path="paragraphs.required_evidence_ids",
                description=f"{len(self_evidence)}个段落把主命题自身列为证据。",
                repair_instruction="删除主命题自引用，并绑定独立事实、来源、实验、指标或论证节点。",
            )
        )

    missing_evidence = sorted(
        active.missing_required_evidence_ids(section_contract, paragraphs)
    )
    if missing_evidence:
        missing_evidence_list = ", ".join(missing_evidence)
        violations.append(
            _violation(
                active,
                "SC-EVIDENCE-CONTRACT-COVERAGE",
                code="QG_BLUEPRINT_REQUIRED_EVIDENCE_MISSING",
                category="EVIDENCE",
                target_path="paragraphs.required_evidence_ids",
                description=(
                    f"Blueprint is missing {len(missing_evidence)} contract-required "
                    f"evidence ID(s): {missing_evidence_list}."
                ),
                repair_instruction=(
                    "Bind every missing section-contract evidence ID to at least one "
                    "paragraph whose primary claim is different from that evidence ID."
                ),
            )
        )

    covered_claims = {
        claim_id for paragraph in paragraphs for claim_id in active.claim_coverage_ids(paragraph)
    }
    required_claims = {
        str(value) for value in section_contract.get("must_advance_claim_ids") or ()
    }
    missing_claims = sorted(required_claims - covered_claims)
    if missing_claims:
        missing_claim_list = ", ".join(missing_claims)
        violations.append(
            _violation(
                active,
                "SC-CLAIM-COVERAGE",
                code="QG_BLUEPRINT_REQUIRED_CLAIMS_MISSING",
                category="BLUEPRINT",
                target_path="paragraphs.primary_claim_id",
                description=(
                    f"Blueprint is missing {len(missing_claims)} required claim ID(s): "
                    f"{missing_claim_list}."
                ),
                repair_instruction=(
                    "Preserve every already-covered claim and add or revise paragraph plans so each "
                    f"missing ID ({missing_claim_list}) is covered by one of the semantic-contract "
                    "claim fields (primary_claim_id, project_item_slots, or technical_slots). Use "
                    "primary_claim_id only when the paragraph advances that claim as its singular thesis."
                ),
            )
        )

    return tuple(violations)
