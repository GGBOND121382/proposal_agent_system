from __future__ import annotations

import json
import re
from pathlib import Path

from app.executor import PromptExecutor
from app.pack import PromptPack


ROOT = Path(__file__).resolve().parents[1]
PACK_ROOT = ROOT / "prompt_pack"
_VERSION_LINE = re.compile(r"^- 版本：`([^`]+)`$", re.MULTILINE)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_registry_input_output_schema_and_prompt_markdown_versions_match() -> None:
    pack = PromptPack(PACK_ROOT)
    for prompt_id in pack.prompt_ids():
        entry = pack.entry(prompt_id)
        expected = entry["prompt_version"]
        input_version = pack.schema(prompt_id, "input")["properties"]["prompt_version"]["const"]
        output_version = pack.schema(prompt_id, "output")["properties"]["prompt_version"]["const"]
        prompt_versions = _VERSION_LINE.findall(pack.prompt_text(prompt_id))

        assert input_version == expected, prompt_id
        assert output_version == expected, prompt_id
        assert prompt_versions, prompt_id
        assert set(prompt_versions) == {expected}, prompt_id


def test_all_replays_use_the_registered_prompt_version() -> None:
    pack = PromptPack(PACK_ROOT)
    manifest = _read_json(PACK_ROOT / "replay" / "manifest.json")
    for item in manifest["cases"]:
        prompt_id = item["prompt_id"]
        expected = pack.entry(prompt_id)["prompt_version"]
        case_path = PACK_ROOT / item["fixture_path"]
        case = _read_json(case_path)

        assert case["input"]["prompt_version"] == expected, case_path
        if isinstance(case.get("expected_output"), dict):
            assert case["expected_output"]["prompt_version"] == expected, case_path


def test_shared_protocol_does_not_hard_code_a_global_prompt_version() -> None:
    shared = (PACK_ROOT / "prompts" / "shared" / "output_protocol.md").read_text(
        encoding="utf-8"
    )
    assert "prompt_version`固定为`2.0.0" not in shared
    assert "schema_version`固定为`2.0" not in shared
    assert "与本次运行时协议身份" in shared


def test_rendered_system_prompt_exposes_only_the_current_protocol_identity() -> None:
    pack = PromptPack(PACK_ROOT)
    executor = PromptExecutor.__new__(PromptExecutor)
    executor.pack = pack

    for prompt_id in pack.prompt_ids():
        entry = pack.entry(prompt_id)
        output_schema = pack.schema(prompt_id, "output")
        envelope = pack.replay_input(prompt_id)
        rendered = executor._system_prompt(prompt_id, output_schema, envelope)
        prompt_version = entry["prompt_version"]
        schema_version = output_schema["properties"]["schema_version"]["const"]

        if str(entry.get("model_contract_mode") or "").upper() == "SEMANTIC":
            assert "# 本次运行时协议身份" not in rendered
            assert "语义任务通则" in rendered
            assert f"版本：`{prompt_version}`" in rendered
        else:
            identity = rendered.split("# 本次运行时协议身份", 1)[1].split("\n\n", 1)[0]
            assert f"`prompt_id`固定为`{prompt_id}`" in identity
            assert f"`prompt_version`固定为`{prompt_version}`" in identity
            assert f"`schema_version`固定为`{schema_version}`" in identity
            assert "`prompt_version`固定为`2.0.0`" not in rendered or prompt_version == "2.0.0"
