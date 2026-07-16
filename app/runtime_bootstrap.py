from __future__ import annotations


def install_runtime_extensions() -> None:
    """Install Track-A runtime implementations behind the frozen public interfaces."""
    from . import context as context_module
    from . import executor as executor_module
    from . import exporter as exporter_module
    from . import llm as llm_module
    from . import workflows as workflows_module
    from .runtime_context import LiveContextBuilder
    from .runtime_executor import RuntimePromptExecutor
    from .runtime_export import RecoverableDocxExporter
    from .full_integration_quality import FullProposalQualityGuard
    from .runtime_gateway import AuditedModelGateway
    from .runtime_workflows import RecoverableWorkflowEngine

    context_module.ContextBuilder = LiveContextBuilder
    executor_module.ProposalQualityGuard = FullProposalQualityGuard
    executor_module.PromptExecutor = RuntimePromptExecutor
    llm_module.ModelGateway = AuditedModelGateway
    workflows_module.WorkflowEngine = RecoverableWorkflowEngine
    exporter_module.DocxExporter = RecoverableDocxExporter
