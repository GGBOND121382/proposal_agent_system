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


    def close_all(self) -> None:
        """Close registered skills that own external or background resources.

        Closing is deliberately duck-typed so stateless skills need no boilerplate.
        The registry remains reusable: closeable skills such as Mermaid can lazily
        recreate their worker on the next invocation.
        """
        errors: list[Exception] = []
        for skill in reversed(list(self._skills.values())):
            close = getattr(skill, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as exc:  # pragma: no cover - defensive shutdown path
                errors.append(exc)
        if errors:
            raise RuntimeError(
                "failed to close one or more skills: "
                + "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
            )
