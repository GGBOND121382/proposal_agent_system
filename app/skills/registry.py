from __future__ import annotations

from typing import Any


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, Any] = {}

    def register(self, skill: Any) -> None:
        if skill.skill_id in self._skills:
            raise ValueError(f"Duplicate skill_id: {skill.skill_id}")
        self._skills[skill.skill_id] = skill

    def get(self, skill_id: str) -> Any:
        if skill_id not in self._skills:
            raise KeyError(f"Unknown skill_id: {skill_id}")
        return self._skills[skill_id]

    def list(self) -> list[dict[str, str]]:
        return [
            {
                "skill_id": skill.skill_id,
                "version": skill.version,
                "description": getattr(skill, "description", ""),
            }
            for skill in self._skills.values()
        ]
