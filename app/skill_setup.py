from __future__ import annotations

from .skills.executor import SkillExecutor
from .skills.mermaid import MermaidRenderSkill
from .skills.registry import SkillRegistry
from .skills.verifiable_public_research import VerifiablePublicResearchArchiveSkill


def build_skill_executor(db, settings) -> SkillExecutor:
    registry = SkillRegistry()
    registry.register(MermaidRenderSkill(settings))
    registry.register(VerifiablePublicResearchArchiveSkill(settings))
    return SkillExecutor(db, registry, settings)
