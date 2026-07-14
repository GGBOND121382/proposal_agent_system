from __future__ import annotations

import json
import time
from typing import Any

from .base import SkillContext, SkillResult
from .registry import SkillRegistry
from ..util import new_id, sha256_json, utc_now


class SkillExecutionError(RuntimeError):
    pass


class SkillExecutor:
    def __init__(self, db, registry: SkillRegistry, settings):
        self.db = db
        self.registry = registry
        self.settings = settings

    def execute(
        self,
        skill_id: str,
        payload: dict[str, Any],
        *,
        project_id: str,
        workflow_id: str | None,
        security_level: str,
    ) -> SkillResult:
        run_id = new_id("skill-run")
        started = time.perf_counter()
        input_hash = sha256_json(payload)
        status = "ERROR"
        output: dict[str, Any] | None = None
        error: str | None = None
        skill = self.registry.get(skill_id)
        try:
            context = SkillContext(
                project_id=project_id,
                workflow_id=workflow_id,
                security_level=security_level,
                data_dir=str(self.settings.data_dir),
            )
            result = skill.run(payload, context)
            status = result.status
            output = result.output
            return result
        except Exception as exc:  # Skill boundary: persist exact failure before rethrow.
            error = str(exc)
            raise SkillExecutionError(f"{skill_id}: {error}") from exc
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.db.execute(
                """INSERT INTO skill_runs(
                       id,project_id,workflow_id,skill_id,skill_version,status,
                       input_hash,output_hash,input_json,output_json,error,duration_ms,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    project_id,
                    workflow_id,
                    skill_id,
                    getattr(skill, "version", "unknown"),
                    status,
                    input_hash,
                    sha256_json(output) if output is not None else None,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(output, ensure_ascii=False) if output is not None else None,
                    error,
                    duration_ms,
                    utc_now(),
                ),
            )
            self.db.audit(
                "SKILL_EXECUTED",
                project_id=project_id,
                object_id=run_id,
                metadata={
                    "skill_id": skill_id,
                    "skill_version": getattr(skill, "version", "unknown"),
                    "status": status,
                    "input_hash": input_hash,
                    "duration_ms": duration_ms,
                    "error": error,
                },
            )
            if output is not None:
                row = self.db.fetchone(
                    "SELECT COALESCE(MAX(version),0) AS v FROM artifacts WHERE project_id=? AND artifact_type='SKILL_OUTPUT' AND prompt_id=?",
                    (project_id, skill_id),
                )
                version = int(row["v"]) + 1 if row else 1
                self.db.execute(
                    """INSERT INTO artifacts(
                           id,project_id,workflow_id,artifact_type,prompt_id,version,status,
                           security_level,context_hash,content_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id("artifact"),
                        project_id,
                        workflow_id,
                        "SKILL_OUTPUT",
                        skill_id,
                        version,
                        status,
                        security_level,
                        input_hash,
                        json.dumps(output, ensure_ascii=False),
                        utc_now(),
                    ),
                )
