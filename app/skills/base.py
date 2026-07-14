from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SkillContext:
    project_id: str
    workflow_id: str | None
    security_level: str
    data_dir: str


@dataclass
class SkillResult:
    status: str
    output: dict[str, Any]
    warnings: list[str]
    artifacts: list[str]


class Skill(Protocol):
    skill_id: str
    version: str

    def run(self, payload: dict[str, Any], context: SkillContext) -> SkillResult:
        ...
