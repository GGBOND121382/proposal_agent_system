from __future__ import annotations

from .skills.executor import SkillExecutor
from .skills.mermaid import MermaidRenderSkill
from .skills.registry import SkillRegistry
from .skills.crossref_public_research import CrossrefPublicResearchArchiveSkill


def build_skill_executor(db, settings) -> SkillExecutor:
    registry = SkillRegistry()
    registry.register(MermaidRenderSkill(settings))
    registry.register(CrossrefPublicResearchArchiveSkill(settings))
    return SkillExecutor(db, registry, settings)
