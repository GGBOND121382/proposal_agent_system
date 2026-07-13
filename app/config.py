from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    data_dir: Path
    prompt_pack_dir: Path
    db_path: Path
    uploads_dir: Path
    exports_dir: Path
    runtime_mode: str
    request_timeout_seconds: int
    max_upload_mb: int
    public_search_provider: str
    public_search_base_url: str

    @classmethod
    def load(cls) -> "Settings":
        root = Path(__file__).resolve().parents[1]
        data_dir = Path(os.getenv("APP_DATA_DIR", str(root / "data"))).resolve()
        pack_dir = Path(os.getenv("PROMPT_PACK_DIR", str(root / "prompt_pack"))).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        uploads = data_dir / "uploads"
        exports = data_dir / "exports"
        uploads.mkdir(parents=True, exist_ok=True)
        exports.mkdir(parents=True, exist_ok=True)
        mode = os.getenv("MODEL_RUNTIME_MODE", "REPLAY").upper()
        if mode not in {"REPLAY", "MOCK", "LIVE"}:
            raise ValueError("MODEL_RUNTIME_MODE must be REPLAY, MOCK, or LIVE")
        return cls(
            root_dir=root,
            data_dir=data_dir,
            prompt_pack_dir=pack_dir,
            db_path=data_dir / "proposal_agents.sqlite3",
            uploads_dir=uploads,
            exports_dir=exports,
            runtime_mode=mode,
            request_timeout_seconds=int(os.getenv("MODEL_REQUEST_TIMEOUT_SECONDS", "240")),
            max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "50")),
            public_search_provider=os.getenv("PUBLIC_SEARCH_PROVIDER", "disabled").lower(),
            public_search_base_url=os.getenv("PUBLIC_SEARCH_BASE_URL", "").rstrip("/"),
        )
