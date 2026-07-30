from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .diagram_enrichment import DiagramEnrichmentService
from .dependency_preflight import RuntimeDependencyPreflight
from .post_export_acceptance import PostExportAcceptanceManager
from .research import PublicResearchService
from .runtime_context import LiveContextBuilder
from .runtime_executor import RuntimePromptExecutor
from .runtime_export import RecoverableDocxExporter
from .runtime_gateway import AuditedModelGateway
from .runtime_workflows import RecoverableWorkflowEngine
from .security import SecurityRouter
from .unified_workflows import UnifiedWorkflowEngine
from .skill_setup import build_skill_executor
from .track_b import TrackBAgentPromptValidator


@dataclass(frozen=True)
class RuntimeStack:
    router: SecurityRouter
    gateway: AuditedModelGateway
    context_builder: LiveContextBuilder
    executor: RuntimePromptExecutor
    skill_executor: Any
    research: PublicResearchService
    diagram_enrichment: DiagramEnrichmentService
    workflows: UnifiedWorkflowEngine
    exporter: RecoverableDocxExporter
    post_export_acceptance: PostExportAcceptanceManager
    dependency_preflight: RuntimeDependencyPreflight


def build_runtime_stack(settings, pack, db) -> RuntimeStack:
    """Build the production runtime explicitly, without import-time class replacement."""
    router = SecurityRouter(pack)
    gateway = AuditedModelGateway(settings, pack)
    context_builder = LiveContextBuilder(db, pack)
    executor = RuntimePromptExecutor(
        db,
        pack,
        router,
        gateway,
        quality_guard=TrackBAgentPromptValidator(pack),
        quality_guard_enabled=settings.proposal_quality_guard_enabled,
    )
    skill_executor = build_skill_executor(db, settings)
    research = PublicResearchService(settings, skill_executor)
    diagram_enrichment = DiagramEnrichmentService(db, pack, skill_executor)
    dependency_preflight = RuntimeDependencyPreflight(settings, pack, db)
    runtime_workflows = RecoverableWorkflowEngine(
        db,
        pack,
        context_builder,
        executor,
        research,
        diagram_enrichment,
        dependency_preflight=dependency_preflight,
    )
    workflows = UnifiedWorkflowEngine(
        runtime_workflows,
        db,
        settings,
        dependency_preflight=dependency_preflight,
    )
    exporter = RecoverableDocxExporter(db, settings)
    post_export_acceptance = PostExportAcceptanceManager(db, settings, exporter)
    return RuntimeStack(
        router=router,
        gateway=gateway,
        context_builder=context_builder,
        executor=executor,
        skill_executor=skill_executor,
        research=research,
        diagram_enrichment=diagram_enrichment,
        workflows=workflows,
        exporter=exporter,
        post_export_acceptance=post_export_acceptance,
        dependency_preflight=dependency_preflight,
    )
