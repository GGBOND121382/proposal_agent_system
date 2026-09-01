from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_workflow_panel_exposes_one_click_rebuild_and_resume_buttons() -> None:
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    assert 'id="rebuildWorkflow"' in html
    assert 'id="resumeRebuild"' in html
    assert "一键重建所选工作流" in html
    assert "继续重建" in html


def test_frontend_uses_standard_rebuild_apis_and_persisted_operation_listing() -> None:
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    assert "/api/workflow-rebuilds?project_id=" in js
    assert "/rebuild`" in js
    assert "/resume`" in js
    assert "python scripts/rebuild.py" not in js
    assert "rebuild_wf3.py" not in js
    assert "rebuild_wf4.py" not in js


def test_backend_exposes_project_rebuild_listing_endpoint() -> None:
    main_py = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert '@app.get("/api/workflow-rebuilds")' in main_py
    assert "lifecycle.list_operations(project_id, limit=limit)" in main_py
