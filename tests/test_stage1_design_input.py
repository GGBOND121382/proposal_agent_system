from __future__ import annotations
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("stage1_design_input", ROOT / "stage1_tools" / "stage1_design_input.py")
mod = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(mod)


def minimal_candidate():
    # The full positive candidate is produced in the stage trace. These tests target deterministic failure paths.
    return {"schema_version": "bad"}


def test_schema_failure_is_blocking():
    result = mod.deterministic_validate(minimal_candidate())
    assert result["verdict"] == "FAIL"
    assert any(x["code"] == "SCHEMA_ERROR" and x["severity"] == "BLOCKING" for x in result["findings"])


def test_generator_request_keeps_stage_boundary():
    request = mod.generator_request("人机协同决策优势冲刺关键技术研究")
    assert request["input_envelope"]["user_requirements"]["stage_boundary"] == "DESIGN_INPUT_ONLY"
    assert request["output_schema"]["properties"]["document_contract"]["properties"]["body_page_limit"]["maximum"] == 20
    assert "不得补写申报单位" in request["system_prompt"]


def test_package_trace_contains_final_manifest(tmp_path):
    run = tmp_path / "run"
    (run / "quality").mkdir(parents=True)
    (run / "quality" / "final_revalidation.json").write_text('{"verdict":"PASS"}\n', encoding="utf-8")
    archive = mod.package_trace(run)
    import json, zipfile
    manifest = json.loads((run / "TRACE_MANIFEST.json").read_text(encoding="utf-8"))
    assert any(x["path"] == "quality/final_revalidation.json" for x in manifest["files"])
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        prefix = run.name + "/"
        assert prefix + "TRACE_MANIFEST.json" in names
        assert prefix + "quality/final_revalidation.json" in names
        assert prefix + "TRACE_ARCHIVE.json" not in names
