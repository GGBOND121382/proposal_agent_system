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
    model_max_output_tokens: int
    model_min_request_interval_seconds: float
    model_max_input_characters: int
    model_response_format: str
    model_schema_prompt_mode: str
    max_upload_mb: int
    public_search_provider: str
    public_search_base_url: str
    proposal_quality_guard_enabled: bool
    public_research_record_file: str
    public_research_connector_file: str
    public_search_max_results: int
    research_fetch_timeout_seconds: int
    research_max_source_bytes: int
    mermaid_js_path: Path
    mermaid_browser_executable: str
    skill_timeout_seconds: int
    model_gateway_mode: str
    chat_bridge_dir: Path | None
    human_gate_bridge_dir: Path | None

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
        if mode not in {"REPLAY", "MOCK", "SIMULATED", "LIVE"}:
            raise ValueError("MODEL_RUNTIME_MODE must be REPLAY, MOCK, SIMULATED, or LIVE")
        return cls(
            root_dir=root,
            data_dir=data_dir,
            prompt_pack_dir=pack_dir,
            db_path=data_dir / "proposal_agents.sqlite3",
            uploads_dir=uploads,
            exports_dir=exports,
            runtime_mode=mode,
            request_timeout_seconds=int(os.getenv("MODEL_REQUEST_TIMEOUT_SECONDS", "240")),
            model_max_output_tokens=int(os.getenv("MODEL_MAX_OUTPUT_TOKENS", "0")),
            model_min_request_interval_seconds=float(os.getenv("MODEL_MIN_REQUEST_INTERVAL_SECONDS", "0")),
            model_max_input_characters=int(os.getenv("MODEL_MAX_INPUT_CHARACTERS", "80000")),
            model_response_format=os.getenv("MODEL_RESPONSE_FORMAT", "json_schema").strip().lower(),
            model_schema_prompt_mode=os.getenv("MODEL_SCHEMA_PROMPT_MODE", "full").strip().lower(),
            max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "50")),
            public_search_provider=os.getenv("PUBLIC_SEARCH_PROVIDER", "disabled").lower(),
            public_search_base_url=os.getenv("PUBLIC_SEARCH_BASE_URL", "").rstrip("/"),
            proposal_quality_guard_enabled=os.getenv("PROPOSAL_QUALITY_GUARD_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"},
            public_research_record_file=os.getenv("PUBLIC_RESEARCH_RECORD_FILE", ""),
            public_research_connector_file=os.getenv("PUBLIC_RESEARCH_CONNECTOR_FILE", ""),
            public_search_max_results=int(os.getenv("PUBLIC_SEARCH_MAX_RESULTS", "40")),
            research_fetch_timeout_seconds=int(os.getenv("RESEARCH_FETCH_TIMEOUT_SECONDS", "45")),
            research_max_source_bytes=int(os.getenv("RESEARCH_MAX_SOURCE_BYTES", str(10 * 1024 * 1024))),
            mermaid_js_path=Path(os.getenv("MERMAID_JS_PATH", str(root / "third_party" / "mermaid" / "mermaid.min.js"))).resolve(),
            mermaid_browser_executable=os.getenv("MERMAID_BROWSER_EXECUTABLE", ""),
            skill_timeout_seconds=int(os.getenv("SKILL_TIMEOUT_SECONDS", "60")),
            model_gateway_mode=os.getenv("MODEL_GATEWAY_MODE", "OPENAI_COMPATIBLE").strip().upper(),
            chat_bridge_dir=(Path(os.environ["CHAT_BRIDGE_DIR"]).resolve() if os.getenv("CHAT_BRIDGE_DIR") else None),
            human_gate_bridge_dir=(Path(os.environ["HUMAN_GATE_BRIDGE_DIR"]).resolve() if os.getenv("HUMAN_GATE_BRIDGE_DIR") else None),
        )
