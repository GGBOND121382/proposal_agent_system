from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _script(path: str) -> tuple[str, ast.Module]:
    source = (ROOT / path).read_text(encoding="utf-8")
    return source, ast.parse(source)


def test_demo_and_simulated_e2e_use_the_canonical_pause_boundary() -> None:
    for relative in (
        "scripts/demo.py",
        "scripts/run_outdoor_thermos_simulated_e2e.py",
    ):
        source, _ = _script(relative)
        assert "from app.workflow_status import should_pause_automatic_advancement" in source
        assert "should_pause_automatic_advancement(workflow[\"status\"])" in source
        assert "exceeded the" in source
        assert "paused at" in source


def test_simulated_e2e_uses_context_aware_simulated_provider() -> None:
    source, _ = _script("scripts/run_outdoor_thermos_simulated_e2e.py")
    assert 'os.environ["MODEL_RUNTIME_MODE"] = "SIMULATED"' in source
    assert 'os.environ["MODEL_RUNTIME_MODE"] = "REPLAY"' not in source


def test_driver_loops_do_not_unconditionally_break_after_one_running_step() -> None:
    for relative in (
        "scripts/demo.py",
        "scripts/run_outdoor_thermos_simulated_e2e.py",
    ):
        _, tree = _script(relative)
        loops = [node for node in ast.walk(tree) if isinstance(node, ast.For)]
        advance_loops = [
            loop
            for loop in loops
            if any(
                isinstance(statement, ast.Assign)
                and isinstance(statement.value, ast.Await)
                and isinstance(statement.value.value, ast.Call)
                and isinstance(statement.value.value.func, ast.Attribute)
                and statement.value.value.func.attr == "advance"
                for statement in loop.body
            )
        ]
        assert advance_loops, relative
        for loop in advance_loops:
            assert not any(isinstance(statement, ast.Break) for statement in loop.body), relative
            assert loop.orelse, relative
