from __future__ import annotations

from .staged_workflows import STAGED_WORKFLOW_TYPE, StagedWorkflowCoordinator


class UnifiedWorkflowEngine:
    """Facade for database-native workflows and file-bridged staged workflows."""

    def __init__(self, runtime_engine, db, settings):
        self.runtime = runtime_engine
        self.staged = StagedWorkflowCoordinator(db, settings)
        self.db = db
        self.quality_manager = runtime_engine.quality_manager

    def start(self, project_id, workflow_type, options=None):
        if workflow_type == STAGED_WORKFLOW_TYPE:
            return self.staged.start(project_id, options)
        return self.runtime.start(project_id, workflow_type, options)

    async def advance(self, workflow_id):
        row = self.db.fetchone("SELECT workflow_type FROM workflows WHERE id=?", (workflow_id,))
        if row and row["workflow_type"] == STAGED_WORKFLOW_TYPE:
            return await self.staged.advance(workflow_id)
        return await self.runtime.advance(workflow_id)

    def get(self, workflow_id):
        row = self.db.fetchone("SELECT workflow_type FROM workflows WHERE id=?", (workflow_id,))
        if row and row["workflow_type"] == STAGED_WORKFLOW_TYPE:
            return self.staged.get(workflow_id)
        return self.runtime.get(workflow_id)

    def list_gates(self, *args, **kwargs):
        return self.runtime.list_gates(*args, **kwargs)

    def decide_gate(self, *args, **kwargs):
        return self.runtime.decide_gate(*args, **kwargs)

    def staged_files(self, workflow_id):
        return self.staged.files(workflow_id)
