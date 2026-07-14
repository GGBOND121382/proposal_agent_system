from __future__ import annotations

import asyncio
import json

import pytest

from app.documents import parse_document
from app.util import utc_now
from tests.test_runtime import add_standard_materials, create_project, finish_workflow, runtime

def test_multi_section_authoring_and_real_candidate_aggregation(runtime):
    settings, pack, db, _, builder, _, engine, exporter = runtime
    project_id = create_project(db)
    add_standard_materials(settings, db, project_id, current_sections=["立项依据", "研究内容", "研究方案"])

    async def finish(workflow_type: str):
        wf = await finish_workflow(engine, project_id, workflow_type)
        assert wf["status"] == "COMPLETED", wf["state"].get("last_error")
        return wf

    async def prepare_and_author():
        for workflow_type in ["WF-1_PROJECT_INTAKE", "WF-2_TEMPLATE_EXTRACTION"]:
            await finish(workflow_type)
        return await finish("WF-4_PROPOSAL_AUTHORING")

    authoring = asyncio.run(prepare_and_author())
    runs = db.fetchall(
        "SELECT input_json FROM prompt_runs WHERE workflow_id=? AND prompt_id='P-WRITE-CONTENT' AND status='PASS' ORDER BY created_at,id",
        (authoring["id"],),
    )
    titles = [json.loads(row["input_json"])["payload"]["source_section"]["title"] for row in runs]
    assert titles == ["立项依据", "研究内容", "研究方案"]

    integration_run = db.fetchone(
        "SELECT input_json FROM prompt_runs WHERE workflow_id=? AND prompt_id='P-INTEGRATION-CRITIC' ORDER BY created_at DESC LIMIT 1",
        (authoring["id"],),
    )
    integration_input = json.loads(integration_run["input_json"])
    assert [item["section_id"] for item in integration_input["payload"]["candidate_sections"]] == [
        json.loads(row["input_json"])["payload"]["source_section"]["section_id"] for row in runs
    ]
    assert all("candidate" in item for item in integration_input["payload"]["candidate_sections"])

    asyncio.run(finish("WF-5_SECURITY_REVIEW_AND_EXPORT"))
    review_run = db.fetchone(
        "SELECT input_json FROM prompt_runs WHERE project_id=? AND prompt_id='P-FINAL-CONFIDENTIALITY-REVIEW' ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    )
    review_input = json.loads(review_run["input_json"])
    assert len(review_input["payload"]["candidate_document"]["sections"]) == 3
    exported = exporter.export(project_id)
    assert exported.exists()


def test_safe_online_package_deterministic_redaction(runtime):
    _, pack, *_ = runtime
    from app.privacy import find_sensitive_values, sanitize_safe_online_package

    output = pack.replay_output("P-SAFE-ONLINE-PACKAGE", "normal")
    output["result"]["task_description"] = (
        "为林晓岚在浙江省杭州市西湖区青岚路88号开展的项目检索公开资料，"
        "联系电话138-0000-1234，邮箱lin.xiaolan@example.test。"
    )
    output["result"]["queries"] = ["林晓岚 杭州市 保温杯 人机工效"]
    config = {
        "external_redaction_entities": [
            {"value": "林晓岚", "entity_type": "PERSON", "placeholder": "[PERSON_1]", "field_label": "人员姓名"},
            {"value": "浙江省杭州市西湖区青岚路88号", "entity_type": "ADDRESS", "placeholder": "[ADDRESS_1]", "field_label": "详细地址"},
            {"value": "杭州市", "entity_type": "LOCATION", "placeholder": "[LOCATION_1]", "field_label": "地点名称"},
        ]
    }
    sanitized, redactions = sanitize_safe_online_package(output, config)
    serialized = json.dumps(sanitized, ensure_ascii=False)
    for forbidden in ["林晓岚", "浙江省杭州市西湖区青岚路88号", "杭州市", "138-0000-1234", "lin.xiaolan@example.test"]:
        assert forbidden not in serialized
    assert {"[PERSON_1]", "[ADDRESS_1]", "[LOCATION_1]", "[PHONE]", "[EMAIL]"}.issubset(
        {item["placeholder"] for item in sanitized["result"]["entity_placeholders"]}
    )
    assert len(redactions) >= 5
    assert find_sensitive_values(sanitized["result"], config) == []
    assert pack.validate("P-SAFE-ONLINE-PACKAGE", "output", sanitized) == []


def test_online_executor_blocks_personal_or_location_data(runtime):
    _, pack, db, _, _, executor, _, _ = runtime
    project_id = create_project(db, internet=True)
    row = db.fetchone("SELECT config_json FROM projects WHERE id=?", (project_id,))
    config = json.loads(row["config_json"])
    config["external_redaction_entities"] = [
        {"value": "林晓岚", "entity_type": "PERSON", "placeholder": "[PERSON_1]", "field_label": "人员姓名"},
        {"value": "杭州市", "entity_type": "LOCATION", "placeholder": "[LOCATION_1]", "field_label": "地点名称"},
    ]
    db.execute("UPDATE projects SET config_json=? WHERE id=?", (json.dumps(config, ensure_ascii=False), project_id))

    envelope = pack.replay_input("P-PUBLIC-RESEARCH-PLAN")
    envelope["scope"]["project_id"] = project_id
    envelope["security_context"].update(
        {
            "project_security_level": "PUBLIC",
            "input_max_security_level": "PUBLIC",
            "online_transfer_approval_status": "APPROVED",
            "allowed_model_endpoint_ids": ["online-public-primary"],
        }
    )
    envelope["payload"]["evidence_requirements"] = ["请检索林晓岚在杭州市的相关资料"]

    with pytest.raises(Exception, match="prohibited personal or project-specific data"):
        asyncio.run(executor.execute("P-PUBLIC-RESEARCH-PLAN", envelope, project_id=project_id))
