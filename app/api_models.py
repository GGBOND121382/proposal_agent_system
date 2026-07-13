from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    security_level: Literal["PUBLIC", "INTERNAL", "SENSITIVE", "CLASSIFIED"] = "INTERNAL"
    internet_access_allowed: bool = False
    anonymized_external_processing_allowed: bool = False
    allowed_public_topics: list[str] = Field(default_factory=list)
    prohibited_external_fields: list[str] = Field(default_factory=list)
    recipient_scope: list[str] = Field(default_factory=lambda: ["内部用户"])
    task_instruction: dict[str, Any] | None = None


class PromptExecuteRequest(BaseModel):
    project_id: str
    input_data: dict[str, Any] | None = None
    workflow_id: str | None = None
    overrides: dict[str, Any] | None = None


class WorkflowStartRequest(BaseModel):
    project_id: str
    workflow_type: str
    options: dict[str, Any] = Field(default_factory=dict)
    auto_advance: bool = True


class GateDecisionRequest(BaseModel):
    action: str
    decided_by: str = "local-user"
    decided_role: str
    comment: str | None = None
    answers: list[dict[str, Any]] = Field(default_factory=list)
    context_hash: str | None = None
