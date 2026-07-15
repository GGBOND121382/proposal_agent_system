from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def run(database: Path) -> int:
    os.environ["MODEL_RUNTIME_MODE"] = "SIMULATED"
    os.environ["APP_DATA_DIR"] = str(database.parent)
    os.environ["PROMPT_PACK_DIR"] = str(ROOT / "prompt_pack")
    from app.config import Settings
    from app.db import Database
    from app.executor import PromptExecutor
    from app.llm import ModelGateway
    from app.pack import PromptPack
    from app.security import SecurityRouter
    from app.util import utc_now

    settings = Settings.load()
    db = Database(database)
    pack = PromptPack(settings.prompt_pack_dir)
    executor = PromptExecutor(
        db, pack, SecurityRouter(pack), ModelGateway(settings, pack), quality_guard_enabled=False
    )
    now = utc_now()
    config = {
        "internet_access_allowed": True,
        "anonymized_external_processing_allowed": True,
        "allowed_public_topics": ["公开政策"],
        "prohibited_external_fields": ["真实项目名称"],
        "recipient_scope": ["内部用户"],
        "allowed_model_endpoint_ids": ["offline-primary", "online-public-primary"],
        "retention_days": 365,
    }
    db.execute(
        "INSERT INTO projects(id,name,description,security_level,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("project-f-ci", "F CI evidence", "", "INTERNAL", json.dumps(config), now, now),
    )
    count = 0
    for prompt_id in ("P-SCHEME-EXTRACT", "P-WRITE-CONTENT", "P-INTEGRATION-CRITIC"):
        await executor.execute(prompt_id, pack.replay_input(prompt_id), project_id="project-f-ci")
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Create deterministic prompt trace evidence for F CI.")
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()
    count = asyncio.run(run(database))
    print(json.dumps({"status": "PASS", "database": str(database), "prompt_calls": count}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
