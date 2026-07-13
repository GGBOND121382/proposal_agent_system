from __future__ import annotations

import json
import time
from typing import Any

from .llm import LLMError, ModelGateway
from .security import RoutingDenied, SecurityRouter
from .util import new_id, sha256_json, utc_now


class PromptExecutionError(RuntimeError):
    def __init__(self, message: str, *, validation_errors: list[str] | None = None):
        super().__init__(message)
        self.validation_errors = validation_errors or []


class PromptExecutor:
    def __init__(self, db, pack, router: SecurityRouter, gateway: ModelGateway):
        self.db = db
        self.pack = pack
        self.router = router
        self.gateway = gateway

    async def execute(
        self,
        prompt_id: str,
        envelope: dict[str, Any],
        *,
        project_id: str,
        workflow_id: str | None = None,
        original_environment: str | None = None,
    ) -> dict[str, Any]:
        run_id = new_id("run")
        started = time.perf_counter()
        input_hash = sha256_json(envelope)
        route = None
        output: dict[str, Any] | None = None
        error: str | None = None
        status = "ERROR"
        try:
            input_errors = self.pack.validate(prompt_id, "input", envelope)
            if input_errors:
                raise PromptExecutionError("Input schema validation failed", validation_errors=input_errors)
            route = self.router.route(prompt_id, envelope, original_environment=original_environment)
            output_schema = self.pack.inlined_schema(prompt_id, "output")
            system_prompt = self._system_prompt(prompt_id, output_schema)
            result = await self.gateway.invoke(route, prompt_id, system_prompt, envelope, output_schema)
            output = result.output
            output_errors = self.pack.validate(prompt_id, "output", output)
            if output_errors:
                raise PromptExecutionError("Output schema validation failed", validation_errors=output_errors)
            status = output.get("status", "ERROR")
            duration_ms = int((time.perf_counter() - started) * 1000)
            self._save_run(run_id, project_id, workflow_id, prompt_id, status, result.model_id, result.endpoint_id, input_hash, envelope, output, None, duration_ms)
            self._save_artifact(project_id, workflow_id, prompt_id, output, envelope)
            return {
                "run_id": run_id,
                "prompt_id": prompt_id,
                "status": status,
                "route": {"environment": route.environment, "model_id": result.model_id, "endpoint_id": result.endpoint_id},
                "output": output,
            }
        except (PromptExecutionError, RoutingDenied, LLMError, KeyError, ValueError) as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            details = getattr(exc, "validation_errors", [])
            error = str(exc) + ((" | " + "; ".join(details[:20])) if details else "")
            self._save_run(run_id, project_id, workflow_id, prompt_id, "ERROR", route.model_id if route else None, route.endpoint_id if route else None, input_hash, envelope, output, error, duration_ms)
            raise PromptExecutionError(error, validation_errors=details) from exc

    def _system_prompt(self, prompt_id: str, output_schema: dict[str, Any]) -> str:
        return (
            self.pack.shared_prompt
            + "\n\n"
            + self.pack.prompt_text(prompt_id)
            + "\n\n# 运行时强制输出Schema\n"
            + json.dumps(output_schema, ensure_ascii=False)
        )

    def _save_run(self, run_id: str, project_id: str, workflow_id: str | None, prompt_id: str, status: str, model_id: str | None, endpoint_id: str | None, input_hash: str, envelope: dict[str, Any], output: dict[str, Any] | None, error: str | None, duration_ms: int) -> None:
        self.db.execute(
            """INSERT INTO prompt_runs(id,project_id,workflow_id,prompt_id,status,model_id,endpoint_id,input_hash,output_hash,input_json,output_json,error,duration_ms,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, project_id, workflow_id, prompt_id, status, model_id, endpoint_id,
                input_hash, sha256_json(output) if output is not None else None,
                json.dumps(envelope, ensure_ascii=False), json.dumps(output, ensure_ascii=False) if output is not None else None,
                error, duration_ms, utc_now(),
            ),
        )
        self.db.audit("PROMPT_EXECUTED", project_id=project_id, object_id=run_id, metadata={"prompt_id": prompt_id, "status": status, "input_hash": input_hash, "duration_ms": duration_ms})

    def _save_artifact(self, project_id: str, workflow_id: str | None, prompt_id: str, output: dict[str, Any], envelope: dict[str, Any]) -> None:
        row = self.db.fetchone("SELECT COALESCE(MAX(version),0) AS v FROM artifacts WHERE project_id=? AND prompt_id=?", (project_id, prompt_id))
        version = int(row["v"]) + 1 if row else 1
        security_level = envelope.get("security_context", {}).get("input_max_security_level", "INTERNAL")
        self.db.execute(
            """INSERT INTO artifacts(id,project_id,workflow_id,artifact_type,prompt_id,version,status,security_level,context_hash,content_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("artifact"), project_id, workflow_id, "PROMPT_OUTPUT", prompt_id, version, output.get("status", "UNKNOWN"), security_level, sha256_json(envelope), json.dumps(output, ensure_ascii=False), utc_now()),
        )
