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
    public_search_engines: str
    public_search_engines_zh: str
    public_search_engines_en: str
    proposal_quality_guard_enabled: bool
    public_research_record_file: str
    public_research_connector_file: str
    public_search_max_results: int
    public_search_required_channels: str
    public_search_required_providers: str
    public_search_require_web_discovery: bool
    public_research_min_fulltext_sources_per_query: int
    public_search_allow_snippet_only: bool
    research_fetch_timeout_seconds: int
    research_max_source_bytes: int
    browser_search_enabled: bool
    browser_search_url_template: str
    browser_executable: str
    browser_headless: bool
    browser_navigation_timeout_seconds: int
    browser_rate_limit_seconds: float
    browser_cache_ttl_seconds: int
    browser_fetch_fallback_enabled: bool
    browser_humanize_enabled: bool
    browser_fallback_min_hits_per_query: int
    mermaid_js_path: Path
    mermaid_browser_executable: str
    skill_timeout_seconds: int
    safe_package_critic_enabled: bool

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
            max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "50")),
            public_search_provider=os.getenv("PUBLIC_SEARCH_PROVIDER", "disabled").lower(),
            public_search_base_url=os.getenv("PUBLIC_SEARCH_BASE_URL", "").rstrip("/"),
            public_search_engines=os.getenv("PUBLIC_SEARCH_ENGINES", "").strip(),
            public_search_engines_zh=os.getenv("PUBLIC_SEARCH_ENGINES_ZH", "").strip(),
            public_search_engines_en=os.getenv("PUBLIC_SEARCH_ENGINES_EN", "").strip(),
            proposal_quality_guard_enabled=os.getenv("PROPOSAL_QUALITY_GUARD_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"},
            public_research_record_file=os.getenv("PUBLIC_RESEARCH_RECORD_FILE", ""),
            public_research_connector_file=os.getenv("PUBLIC_RESEARCH_CONNECTOR_FILE", ""),
            public_search_max_results=int(os.getenv("PUBLIC_SEARCH_MAX_RESULTS", "40")),
            public_search_required_channels=os.getenv("PUBLIC_SEARCH_REQUIRED_CHANNELS", "").strip(),
            public_search_required_providers=os.getenv("PUBLIC_SEARCH_REQUIRED_PROVIDERS", "").strip(),
            public_search_require_web_discovery=os.getenv("PUBLIC_SEARCH_REQUIRE_WEB_DISCOVERY", "false").strip().lower() not in {"0", "false", "no", "off"},
            public_research_min_fulltext_sources_per_query=int(os.getenv("PUBLIC_RESEARCH_MIN_FULLTEXT_SOURCES_PER_QUERY", "1")),
            public_search_allow_snippet_only=os.getenv("PUBLIC_SEARCH_ALLOW_SNIPPET_ONLY", "true").strip().lower() not in {"0", "false", "no", "off"},
            research_fetch_timeout_seconds=int(os.getenv("RESEARCH_FETCH_TIMEOUT_SECONDS", "45")),
            research_max_source_bytes=int(os.getenv("RESEARCH_MAX_SOURCE_BYTES", str(10 * 1024 * 1024))),
            browser_search_enabled=os.getenv("BROWSER_SEARCH_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"},
            browser_search_url_template=os.getenv(
                "BROWSER_SEARCH_URL_TEMPLATE",
                "https://search.brave.com/search?q={query}&source=web",
            ).strip(),
            browser_executable=os.getenv(
                "BROWSER_EXECUTABLE",
                os.getenv("MERMAID_BROWSER_EXECUTABLE", ""),
            ).strip(),
            browser_headless=os.getenv("BROWSER_HEADLESS", "true").strip().lower() not in {"0", "false", "no", "off"},
            browser_navigation_timeout_seconds=int(os.getenv("BROWSER_NAVIGATION_TIMEOUT_SECONDS", "45")),
            browser_rate_limit_seconds=float(os.getenv("BROWSER_RATE_LIMIT_SECONDS", "1.5")),
            browser_cache_ttl_seconds=int(os.getenv("BROWSER_CACHE_TTL_SECONDS", "86400")),
            browser_fetch_fallback_enabled=os.getenv("BROWSER_FETCH_FALLBACK_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"},
            browser_humanize_enabled=os.getenv("BROWSER_HUMANIZE_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"},
            browser_fallback_min_hits_per_query=int(os.getenv("PUBLIC_SEARCH_BROWSER_FALLBACK_MIN_HITS_PER_QUERY", "1")),
            mermaid_js_path=Path(os.getenv("MERMAID_JS_PATH", str(root / "third_party" / "mermaid" / "mermaid.min.js"))).resolve(),
            mermaid_browser_executable=os.getenv("MERMAID_BROWSER_EXECUTABLE", ""),
            skill_timeout_seconds=int(os.getenv("SKILL_TIMEOUT_SECONDS", "60")),
            safe_package_critic_enabled=os.getenv("SAFE_PACKAGE_CRITIC_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"},
        )
