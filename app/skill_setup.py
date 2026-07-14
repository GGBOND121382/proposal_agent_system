from __future__ import annotations

from .skills.executor import SkillExecutor
from .skills.mermaid import MermaidRenderSkill
from .skills.public_research import PublicResearchArchiveSkill
from .skills.registry import SkillRegistry


def build_skill_executor(db, settings) -> SkillExecutor:
    registry = SkillRegistry()
    registry.register(MermaidRenderSkill(settings))
    registry.register(PublicResearchArchiveSkill(settings))
    return SkillExecutor(db, registry, settings)
