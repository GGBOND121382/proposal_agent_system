from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from app.context import (
    ContextBuilder,
    _BUILD_AUTHORING_CHILD_IDS,
    _BUILD_PROMPT_ID,
    _BUILD_SECTION_ID,
    _BUILD_WORKFLOW_ID,
)
from app.runtime_context import (
    LiveContextBuilder,
    _LIVE_ASSEMBLING,
    _LIVE_TOUCHED_PATHS,
)


def test_workflow_build_scope_is_task_local() -> None:
    builder = object.__new__(ContextBuilder)

    async def capture(name: str) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
        state = {
            "active_section_id": f"section-{name}",
            "authoring_child_workflow_ids": [f"child-{name}"],
        }
        with builder._workflow_build_scope(f"P-{name}", f"workflow-{name}", state):
            await asyncio.sleep(0)
            return (
                _BUILD_PROMPT_ID.get(),
                _BUILD_WORKFLOW_ID.get(),
                _BUILD_SECTION_ID.get(),
                _BUILD_AUTHORING_CHILD_IDS.get(),
            )

    async def run_pair():
        return await asyncio.gather(capture("A"), capture("B"))

    first, second = asyncio.run(run_pair())
    assert first == ("P-A", "workflow-A", "section-A", ("child-A",))
    assert second == ("P-B", "workflow-B", "section-B", ("child-B",))
    assert _BUILD_PROMPT_ID.get() is None
    assert _BUILD_WORKFLOW_ID.get() is None
    assert _BUILD_SECTION_ID.get() is None
    assert _BUILD_AUTHORING_CHILD_IDS.get() == ()


def test_live_context_mutation_tracking_is_task_local() -> None:
    builder = object.__new__(LiveContextBuilder)
    builder.runtime_mode = "LIVE"

    async def touch(name: str) -> set[str]:
        touched_token = _LIVE_TOUCHED_PATHS.set(set())
        assembling_token = _LIVE_ASSEMBLING.set(True)
        try:
            envelope = {"payload": {"value": None}}
            assert builder._set_path_if_valid(
                "P-TEST", envelope, "payload.value", name
            )
            await asyncio.sleep(0)
            assert envelope["payload"]["value"] == name
            return set(builder._touched_paths())
        finally:
            _LIVE_ASSEMBLING.reset(assembling_token)
            _LIVE_TOUCHED_PATHS.reset(touched_token)

    async def run_pair():
        return await asyncio.gather(touch("A"), touch("B"))

    first, second = asyncio.run(run_pair())
    assert first == {"payload.value"}
    assert second == {"payload.value"}
    assert _LIVE_ASSEMBLING.get() is False
    assert _LIVE_TOUCHED_PATHS.get() is None


def test_workflow_identity_contextvar_is_read_only_at_one_resolution_boundary() -> None:
    source_path = Path(__file__).parents[1] / "app" / "context_base.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    enclosing: list[str] = []
    readers: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            enclosing.append(node.name)
            self.generic_visit(node)
            enclosing.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "get"
                and isinstance(function.value, ast.Name)
                and function.value.id == "_CURRENT_WORKFLOW_ID"
            ):
                readers.append(enclosing[-1] if enclosing else "<module>")
            self.generic_visit(node)

    Visitor().visit(tree)
    assert readers == ["_resolve_workflow_id"]


def test_live_context_touched_scope_resets_when_build_fails() -> None:
    builder = object.__new__(LiveContextBuilder)
    builder.runtime_mode = "LIVE"

    def fail(*args, **kwargs):
        builder._touched_paths().add("payload.before_failure")
        raise RuntimeError("expected failure")

    builder._build_live = fail
    assert _LIVE_TOUCHED_PATHS.get() is None
    with pytest.raises(RuntimeError, match="expected failure"):
        builder.build("P-TEST", "project-1")
    assert _LIVE_TOUCHED_PATHS.get() is None
