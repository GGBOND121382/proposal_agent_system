from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrate_contract_artifact.py"


def load_module():
    spec = importlib.util.spec_from_file_location("migrate_contract_artifact", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_historical_envelope_is_migrated_and_raw_payload_is_preserved():
    module = load_module()
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["knowledge_status", "claim_type", "temporal_status"],
        "additionalProperties": False,
        "properties": {
            "knowledge_status": {"enum": ["DOCUMENT_EXTRACTED", "USER_ASSERTED", "ESTIMATED"]},
            "claim_type": {"enum": ["FACT", "PLAN", "EXPECTED_RESULT", "MODEL_INFERENCE"]},
            "temporal_status": {"enum": ["CURRENT", "PLANNED", "EXPECTED", "UNKNOWN"]},
        },
    }
    document = {
        "request_id": "req-1",
        "output": {
            "knowledge_status": "DOCUMENT_EXTRACTED",
            "claim_type": "PROJECT_DESIGN",
            "temporal_status": "PROJECT_DESIGN",
        },
    }

    output, report = module.migrate(document, schema, contract_id="test:migration")

    assert output["request_id"] == "req-1"
    assert output["output"] == {
        "knowledge_status": "DOCUMENT_EXTRACTED",
        "claim_type": "PLAN",
        "temporal_status": "PLANNED",
    }
    assert report["raw_payload"]["claim_type"] == "PROJECT_DESIGN"
    assert report["normalized_payload"]["claim_type"] == "PLAN"
    assert report["schema_validation"] == {"valid": True, "error_count": 0, "errors": []}
    assert report["registry_normalization"]["normalized_count"] == 2


def test_unknown_alias_remains_invalid_and_auditable():
    module = load_module()
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["claim_type"],
        "properties": {"claim_type": {"enum": ["FACT", "PLAN"]}},
    }
    output, report = module.migrate(
        {"claim_type": "MODEL_APPROVED_PLAN"}, schema, contract_id="test:strict"
    )

    assert output["claim_type"] == "MODEL_APPROVED_PLAN"
    assert report["registry_normalization"]["unresolved_count"] == 1
    assert report["schema_validation"]["valid"] is False
    assert report["schema_validation"]["error_count"] == 1
