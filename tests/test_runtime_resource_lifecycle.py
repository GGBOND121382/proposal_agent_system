from __future__ import annotations

import asyncio
import gc
import threading
import time
import weakref
from types import SimpleNamespace

from app.diagram_enrichment import DiagramEnrichmentService
from app.runtime_factory import RuntimeStack
from app.skills.registry import SkillRegistry
from app.skills.mermaid import MermaidRenderSkill


class _FakeSkillExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, skill_id, payload, **kwargs):
        self.calls += 1
        return SimpleNamespace(output={"skill_id": skill_id, "payload": payload})


class _CloseableSkill:
    skill_id = "test.closeable"
    version = "1"
    description = ""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("skill")


class _StatelessSkill:
    skill_id = "test.stateless"
    version = "1"
    description = ""


def _renderer_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("mermaid-render") and thread.is_alive()
    ]


def test_diagram_executor_closes_and_can_be_recreated() -> None:
    fake = _FakeSkillExecutor()
    service = DiagramEnrichmentService(None, None, fake)

    async def invoke() -> None:
        result = await service._execute_mermaid(
            {"mermaid_source": "flowchart LR"},
            project_id="project-test",
            workflow_id="workflow-test",
            security_level="INTERNAL",
        )
        assert result.output["skill_id"] == "mermaid.render"

    asyncio.run(invoke())
    assert _renderer_threads(), "renderer executor worker was not created"

    service.close()
    deadline = time.monotonic() + 1.0
    while _renderer_threads() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _renderer_threads()

    # TestClient/application lifecycles may start the same service again in one
    # interpreter. Closing must release resources without making it unusable.
    asyncio.run(invoke())
    assert fake.calls == 2
    service.close()
    assert not _renderer_threads()


def test_skill_registry_closes_only_closeable_skills() -> None:
    events: list[str] = []
    registry = SkillRegistry()
    registry.register(_CloseableSkill(events))
    registry.register(_StatelessSkill())

    registry.close_all()

    assert events == ["skill"]


def test_runtime_stack_closes_renderer_before_skills() -> None:
    events: list[str] = []

    class _Renderer:
        def close(self) -> None:
            events.append("renderer")

    class _Skills:
        def close(self) -> None:
            events.append("skills")

    stack = RuntimeStack(
        router=None,
        gateway=None,
        context_builder=None,
        executor=None,
        skill_executor=_Skills(),
        research=None,
        diagram_enrichment=_Renderer(),
        workflows=None,
        exporter=None,
        post_export_acceptance=None,
        dependency_preflight=None,
    )

    stack.close()

    assert events == ["renderer", "skills"]


def test_mermaid_atexit_registration_does_not_retain_discarded_skill(tmp_path) -> None:
    settings = SimpleNamespace(mermaid_js_path=tmp_path / "missing-mermaid.js")
    skill = MermaidRenderSkill(settings)
    ref = weakref.ref(skill)

    skill.close()
    del skill
    gc.collect()

    assert ref() is None


def test_runtime_stack_attempts_all_cleanup_after_component_failure() -> None:
    events: list[str] = []

    class _BrokenRenderer:
        def close(self) -> None:
            events.append("renderer")
            raise RuntimeError("renderer-close-failed")

    class _Skills:
        def close(self) -> None:
            events.append("skills")

    stack = RuntimeStack(
        router=None,
        gateway=None,
        context_builder=None,
        executor=None,
        skill_executor=_Skills(),
        research=None,
        diagram_enrichment=_BrokenRenderer(),
        workflows=None,
        exporter=None,
        post_export_acceptance=None,
        dependency_preflight=None,
    )

    try:
        stack.close()
    except RuntimeError as exc:
        assert "renderer-close-failed" in str(exc)
    else:  # pragma: no cover - the test requires aggregated shutdown failure
        raise AssertionError("runtime close should report the renderer failure")

    assert events == ["renderer", "skills"]
