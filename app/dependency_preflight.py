from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .runtime_policy import CapabilityModeError, CapabilityPolicy
from .util import utc_now


@dataclass(frozen=True)
class DependencyIssue:
    code: str
    dependency: str
    message: str
    required_settings: tuple[str, ...] = ()
    severity: str = "ERROR"
    retryable: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["required_settings"] = list(self.required_settings)
        return value


@dataclass
class DependencyReport:
    scope: str
    issues: list[DependencyIssue] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    generated_at: str = field(default_factory=utc_now)

    @property
    def blocking_issues(self) -> list[DependencyIssue]:
        return [item for item in self.issues if item.severity == "ERROR"]

    @property
    def status(self) -> str:
        if self.blocking_issues:
            return "WAITING_CONFIGURATION"
        if self.issues:
            return "WARNING"
        return "PASS"

    def extend(self, other: "DependencyReport") -> None:
        issue_keys = {
            (item.code, item.dependency, item.message, item.required_settings)
            for item in self.issues
        }
        for item in other.issues:
            key = (item.code, item.dependency, item.message, item.required_settings)
            if key not in issue_keys:
                self.issues.append(item)
                issue_keys.add(key)
        check_keys = {
            (str(item.get("name") or ""), str(item.get("path") or ""), str(item.get("endpoint_id") or ""))
            for item in self.checks
        }
        for item in other.checks:
            key = (
                str(item.get("name") or ""),
                str(item.get("path") or ""),
                str(item.get("endpoint_id") or ""),
            )
            if key not in check_keys:
                self.checks.append(item)
                check_keys.add(key)

    def summary(self) -> str:
        if not self.blocking_issues:
            return "运行依赖检查通过"
        messages = [f"{item.code}: {item.message}" for item in self.blocking_issues]
        return "运行依赖未满足：" + "；".join(messages[:8])

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "scope": self.scope,
            "generated_at": self.generated_at,
            "issues": [item.as_dict() for item in self.issues],
            "checks": self.checks,
        }


class RuntimeDependencyPreflight:
    """Central dependency checks for application, workflow and step execution.

    Configuration failures are represented as explicit dependency issues. Workflow
    engines use these reports to pause in ``WAITING_CONFIGURATION`` rather than
    consuming retry budgets or recording an opaque technical ``BLOCKED`` state.
    """

    MODEL_CONFIGURATION_MARKERS = (
        "no eligible model route",
        "endpoint disabled",
        "has no base_url",
        "provider_model_name is empty",
        "llm transport failed",
        "llm stream exceeded",
        "llm endpoint returned 401",
        "llm endpoint returned 403",
        "llm endpoint returned 404",
        "llm endpoint returned 408",
        "llm endpoint returned 429",
        "llm endpoint returned 500",
        "llm endpoint returned 502",
        "llm endpoint returned 503",
        "llm endpoint returned 504",
        "connection refused",
        "connecterror",
        "name or service not known",
        "temporary failure in name resolution",
    )
    SEARCH_CONFIGURATION_MARKERS = (
        "public_search_provider is disabled",
        "unsupported public_search_provider",
        "public_search_base_url is empty",
        "connector research file not found",
        "recorded research file not found",
        "connector research file must",
        "recorded research file must",
        "connector responses do not cover planned queries",
        "public research skill executor is not configured",
        "searxng",
        "connecterror",
        "connection refused",
        "name or service not known",
        "temporary failure in name resolution",
        "timed out",
        "timeout",
        "403 forbidden",
        "404 not found",
        "502 bad gateway",
        "503 service unavailable",
    )
    STORAGE_CONFIGURATION_MARKERS = (
        "permission denied",
        "read-only file system",
        "no space left on device",
        "evidenceintegrityerror",
        "request evidence mismatch",
        "response evidence mismatch",
        "partial request evidence",
    )
    EXPORT_CONFIGURATION_MARKERS = (
        "libreoffice",
        "soffice",
        "no chromium/chrome/edge executable",
        "mermaid_js_path",
        "font",
    )

    def __init__(self, settings, pack, db=None):
        self.settings = settings
        self.pack = pack
        self.db = db

    @staticmethod
    def _bool_env(name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _atomic_write_check(path: Path) -> tuple[bool, str | None]:
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path, prefix=".preflight-", delete=False
            ) as handle:
                temp = Path(handle.name)
                handle.write(b"proposal-agent-preflight")
                handle.flush()
                os.fsync(handle.fileno())
            target = temp.with_suffix(".committed")
            os.replace(temp, target)
            target.read_bytes()
            target.unlink()
            return True, None
        except Exception as exc:  # pragma: no cover - platform-specific messages
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _load_json_file(path: Path) -> tuple[Any | None, str | None]:
        try:
            return json.loads(path.read_text(encoding="utf-8")), None
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _find_browser(configured: str) -> str | None:
        if configured:
            path = Path(configured).expanduser()
            if path.exists():
                return str(path.resolve())
            resolved = shutil.which(configured)
            if resolved:
                return resolved
        for name in ("chromium", "chromium-browser", "google-chrome", "chrome", "msedge", "microsoft-edge"):
            resolved = shutil.which(name)
            if resolved:
                return resolved
        windows_candidates = (
            Path(os.getenv("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.getenv("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.getenv("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.getenv("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        )
        for path in windows_candidates:
            if str(path) and path.exists():
                return str(path.resolve())
        return None

    @staticmethod
    def _find_libreoffice() -> str | None:
        configured = os.getenv("LIBREOFFICE_EXECUTABLE", "").strip()
        if configured:
            path = Path(configured).expanduser()
            if path.exists():
                return str(path.resolve())
            resolved = shutil.which(configured)
            if resolved:
                return resolved
        for name in ("libreoffice", "soffice"):
            resolved = shutil.which(name)
            if resolved:
                return resolved
        windows_candidates = (
            Path(os.getenv("PROGRAMFILES", "")) / "LibreOffice/program/soffice.exe",
            Path(os.getenv("PROGRAMFILES(X86)", "")) / "LibreOffice/program/soffice.exe",
        )
        for path in windows_candidates:
            if str(path) and path.exists():
                return str(path.resolve())
        return None

    @staticmethod
    def _font_candidates(kind: str) -> list[Path]:
        env_name = "STAGE8_FONT_SANS_PATH" if kind == "sans" else "STAGE8_FONT_SERIF_PATH"
        configured = os.getenv(env_name, "").strip()
        result: list[Path] = [Path(configured).expanduser()] if configured else []
        if kind == "sans":
            result.extend(
                [
                    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                    Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
                    Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc",
                    Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts/simhei.ttf",
                ]
            )
        else:
            result.extend(
                [
                    Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
                    Path("/usr/share/fonts/opentype/noto/NotoSerifCJKsc-Regular.otf"),
                    Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts/simsun.ttc",
                    Path(os.getenv("WINDIR", "C:/Windows")) / "Fonts/simfang.ttf",
                ]
            )
        return result

    @classmethod
    def _find_font(cls, kind: str) -> str | None:
        for path in cls._font_candidates(kind):
            if path.exists() and path.is_file():
                return str(path.resolve())
        return None

    def _storage_report(self) -> DependencyReport:
        report = DependencyReport("APPLICATION_STORAGE")
        evidence_dir = Path(
            os.getenv("MODEL_CALL_EVIDENCE_DIR", str(Path(self.settings.data_dir) / "model_calls"))
        ).resolve()
        runtime_export_dir = Path(
            os.getenv(
                "RUNTIME_EXPORT_EVIDENCE_DIR",
                str(Path(self.settings.data_dir) / "runtime_exports"),
            )
        ).resolve()
        checks = (
            (Path(self.settings.data_dir), "APP_DATA_DIR", "APP_DATA_DIR_NOT_WRITABLE"),
            (Path(self.settings.uploads_dir), "APP_DATA_DIR", "UPLOAD_DIR_NOT_WRITABLE"),
            (Path(self.settings.exports_dir), "APP_DATA_DIR", "EXPORT_DIR_NOT_WRITABLE"),
            (evidence_dir, "MODEL_CALL_EVIDENCE_DIR", "MODEL_EVIDENCE_DIR_NOT_WRITABLE"),
            (runtime_export_dir, "RUNTIME_EXPORT_EVIDENCE_DIR", "RUNTIME_EXPORT_EVIDENCE_DIR_NOT_WRITABLE"),
        )
        for path, setting, code in checks:
            ok, reason = self._atomic_write_check(path)
            report.checks.append({"name": code, "status": "PASS" if ok else "FAIL", "path": str(path), "reason": reason})
            if not ok:
                report.issues.append(
                    DependencyIssue(
                        code=code,
                        dependency="STORAGE",
                        message=f"目录无法完成创建、fsync、原子重命名和回读：{path}（{reason}）",
                        required_settings=(setting,),
                    )
                )
        return report

    def _runtime_policy_report(self) -> DependencyReport:
        report = DependencyReport("RUNTIME_POLICY")
        try:
            CapabilityPolicy.from_environment().assert_environment(self.settings.runtime_mode)
            report.checks.append({"name": "CAPABILITY_POLICY", "status": "PASS"})
        except CapabilityModeError as exc:
            report.issues.append(
                DependencyIssue(
                    code="CAPABILITY_MODE_CONFIGURATION_CONFLICT",
                    dependency="RUNTIME_POLICY",
                    message=str(exc),
                    required_settings=("CAPABILITY_ACCEPTANCE_MODE", "MODEL_RUNTIME_MODE", "PUBLIC_SEARCH_PROVIDER"),
                )
            )
        fault_points = os.getenv("RUNTIME_FAULT_POINT", "").strip()
        if (
            fault_points
            and str(self.settings.runtime_mode).upper() == "LIVE"
            and not self._bool_env("ALLOW_RUNTIME_FAULT_INJECTION", False)
        ):
            report.issues.append(
                DependencyIssue(
                    code="RUNTIME_FAULT_INJECTION_ENABLED",
                    dependency="RUNTIME_POLICY",
                    message="LIVE 运行环境仍配置了 RUNTIME_FAULT_POINT；这会故意中断工作流。",
                    required_settings=("RUNTIME_FAULT_POINT", "ALLOW_RUNTIME_FAULT_INJECTION"),
                    details={"fault_points": fault_points},
                )
            )
        return report

    def application_report(self, *, require_export: bool = False) -> DependencyReport:
        report = DependencyReport("APPLICATION")
        report.extend(self._storage_report())
        report.extend(self._runtime_policy_report())
        if int(self.settings.request_timeout_seconds) <= 0:
            report.issues.append(
                DependencyIssue(
                    code="MODEL_REQUEST_TIMEOUT_INVALID",
                    dependency="MODEL_ENDPOINT",
                    message="MODEL_REQUEST_TIMEOUT_SECONDS 必须大于 0。",
                    required_settings=("MODEL_REQUEST_TIMEOUT_SECONDS",),
                )
            )
        if int(self.settings.skill_timeout_seconds) <= 0:
            report.issues.append(
                DependencyIssue(
                    code="SKILL_TIMEOUT_INVALID",
                    dependency="SKILL_RUNTIME",
                    message="SKILL_TIMEOUT_SECONDS 必须大于 0。",
                    required_settings=("SKILL_TIMEOUT_SECONDS",),
                )
            )
        if not Path(self.settings.prompt_pack_dir).exists():
            report.issues.append(
                DependencyIssue(
                    code="PROMPT_PACK_NOT_FOUND",
                    dependency="PROMPT_PACK",
                    message=f"Prompt Pack 目录不存在：{self.settings.prompt_pack_dir}",
                    required_settings=("PROMPT_PACK_DIR",),
                    retryable=False,
                )
            )
        if not Path(self.settings.mermaid_js_path).exists():
            report.issues.append(
                DependencyIssue(
                    code="MERMAID_RUNTIME_NOT_FOUND",
                    dependency="MERMAID",
                    message=f"Mermaid 运行时不存在：{self.settings.mermaid_js_path}",
                    required_settings=("MERMAID_JS_PATH",),
                    severity="WARNING" if not require_export else "ERROR",
                )
            )
        browser = self._find_browser(self.settings.mermaid_browser_executable)
        report.checks.append({"name": "MERMAID_BROWSER", "status": "PASS" if browser else "FAIL", "path": browser})
        if not browser:
            report.issues.append(
                DependencyIssue(
                    code="MERMAID_BROWSER_NOT_FOUND",
                    dependency="MERMAID",
                    message="未找到 Chromium、Chrome 或 Edge。包含 Mermaid 图示的章节将无法渲染。",
                    required_settings=("MERMAID_BROWSER_EXECUTABLE",),
                    severity="WARNING" if not require_export else "ERROR",
                )
            )
        if require_export:
            libreoffice = self._find_libreoffice()
            report.checks.append({"name": "LIBREOFFICE", "status": "PASS" if libreoffice else "FAIL", "path": libreoffice})
            if not libreoffice:
                report.issues.append(
                    DependencyIssue(
                        code="LIBREOFFICE_NOT_FOUND",
                        dependency="PDF_EXPORT",
                        message="未找到 LibreOffice/soffice，无法完成 PDF 转换和页数验收。",
                        required_settings=("LIBREOFFICE_EXECUTABLE",),
                    )
                )
            for kind, env_name in (("sans", "STAGE8_FONT_SANS_PATH"), ("serif", "STAGE8_FONT_SERIF_PATH")):
                font = self._find_font(kind)
                report.checks.append({"name": f"STAGE8_FONT_{kind.upper()}", "status": "PASS" if font else "FAIL", "path": font})
                if not font:
                    report.issues.append(
                        DependencyIssue(
                            code=f"STAGE8_FONT_{kind.upper()}_NOT_FOUND",
                            dependency="PDF_EXPORT",
                            message=f"未找到 Stage 8 所需的中文{'无衬线' if kind == 'sans' else '衬线'}字体。",
                            required_settings=(env_name,),
                        )
                    )
        return report

    def _model_environment_report(self, environment: str) -> DependencyReport:
        report = DependencyReport(f"MODEL:{environment}")
        if str(self.settings.runtime_mode).upper() != "LIVE":
            report.checks.append({"name": f"MODEL_{environment}", "status": "SKIP", "reason": f"runtime mode {self.settings.runtime_mode}"})
            return report
        endpoints = [item for item in self.pack.endpoints.get("endpoints", []) if item.get("environment") == environment]
        enabled = [item for item in endpoints if bool(item.get("enabled", False))]
        if not enabled:
            env_name = "ONLINE_LLM_ENABLED" if environment == "ONLINE_PUBLIC" else "OFFLINE_LLM_ENABLED"
            report.issues.append(
                DependencyIssue(
                    code=f"{environment}_ENDPOINT_DISABLED",
                    dependency="MODEL_ENDPOINT",
                    message=f"没有启用的 {environment} 模型端点。",
                    required_settings=(env_name,),
                )
            )
            return report
        usable_endpoint_ids: set[str] = set()
        for endpoint in enabled:
            endpoint_id = str(endpoint.get("endpoint_id") or "")
            base_url = str(endpoint.get("base_url") or "").strip()
            if not base_url:
                report.issues.append(
                    DependencyIssue(
                        code=f"{environment}_BASE_URL_MISSING",
                        dependency="MODEL_ENDPOINT",
                        message=f"模型端点 {endpoint_id} 未配置 base_url。",
                        required_settings=(("ONLINE_LLM_BASE_URL",) if environment == "ONLINE_PUBLIC" else ("OFFLINE_LLM_BASE_URL",)),
                        details={"endpoint_id": endpoint_id},
                    )
                )
                continue
            usable_endpoint_ids.add(endpoint_id)
            secret_name = str(endpoint.get("api_key_secret") or "").strip()
            if secret_name and not os.getenv(secret_name, "").strip():
                report.issues.append(
                    DependencyIssue(
                        code=f"{environment}_API_KEY_EMPTY",
                        dependency="MODEL_ENDPOINT",
                        message=f"端点 {endpoint_id} 声明了 {secret_name}，但当前为空。若服务允许匿名访问可忽略，否则调用会被拒绝。",
                        required_settings=(secret_name,),
                        severity="WARNING",
                        details={"endpoint_id": endpoint_id},
                    )
                )
        models = [
            item for item in self.pack.models.get("models", [])
            if bool(item.get("enabled", False)) and str(item.get("endpoint_id") or "") in usable_endpoint_ids
        ]
        if not models:
            report.issues.append(
                DependencyIssue(
                    code=f"{environment}_MODEL_NOT_CONFIGURED",
                    dependency="MODEL_ENDPOINT",
                    message=f"{environment} 没有绑定到可用端点的启用模型。",
                    required_settings=("ONLINE_PUBLIC_MODEL",) if environment == "ONLINE_PUBLIC" else ("OFFLINE_GENERAL_MODEL", "OFFLINE_CRITIC_MODEL"),
                )
            )
        for model in models:
            if not str(model.get("provider_model_name") or "").strip():
                report.issues.append(
                    DependencyIssue(
                        code="PROVIDER_MODEL_NAME_EMPTY",
                        dependency="MODEL_ENDPOINT",
                        message=f"模型 {model.get('model_id')} 的 provider_model_name 为空。",
                        required_settings=("ONLINE_PUBLIC_MODEL",) if environment == "ONLINE_PUBLIC" else ("OFFLINE_GENERAL_MODEL", "OFFLINE_CRITIC_MODEL"),
                        details={"model_id": model.get("model_id")},
                    )
                )
        return report

    def _project_online_report(self, project_id: str | None) -> DependencyReport:
        report = DependencyReport("PROJECT_ONLINE_POLICY")
        if not project_id or self.db is None:
            return report
        row = self.db.fetchone("SELECT config_json FROM projects WHERE id=?", (project_id,))
        if not row:
            return report
        config = json.loads(row.get("config_json") or "{}")
        if not bool(config.get("internet_access_allowed")):
            report.issues.append(
                DependencyIssue(
                    code="PROJECT_INTERNET_ACCESS_NOT_ALLOWED",
                    dependency="PROJECT_POLICY",
                    message="项目未允许访问互联网。",
                    required_settings=("project.internet_access_allowed",),
                )
            )
        if not bool(config.get("anonymized_external_processing_allowed")):
            report.issues.append(
                DependencyIssue(
                    code="PROJECT_EXTERNAL_PROCESSING_NOT_ALLOWED",
                    dependency="PROJECT_POLICY",
                    message="项目未允许匿名化后的外部模型处理。",
                    required_settings=("project.anonymized_external_processing_allowed",),
                )
            )
        allowed = {str(item) for item in config.get("allowed_model_endpoint_ids") or []}
        online_endpoint_ids = {
            str(item.get("endpoint_id") or "")
            for item in self.pack.endpoints.get("endpoints", [])
            if item.get("environment") == "ONLINE_PUBLIC" and bool(item.get("enabled", False))
        }
        if online_endpoint_ids and not (allowed & online_endpoint_ids):
            report.issues.append(
                DependencyIssue(
                    code="PROJECT_ONLINE_ENDPOINT_NOT_ALLOWED",
                    dependency="PROJECT_POLICY",
                    message=(
                        "项目允许端点列表未包含任何已启用的 ONLINE_PUBLIC 端点："
                        + "、".join(sorted(online_endpoint_ids))
                    ),
                    required_settings=("project.allowed_model_endpoint_ids",),
                    details={"enabled_online_endpoint_ids": sorted(online_endpoint_ids)},
                )
            )
        return report

    def _prompt_route_report(self, project_id: str, prompt_id: str) -> DependencyReport:
        report = DependencyReport(f"PROMPT_ROUTE:{prompt_id}")
        if str(self.settings.runtime_mode).upper() != "LIVE":
            return report
        entry = self.pack.entry(prompt_id)
        required = str(entry.get("required_environment") or "")
        if required == "SAME_AS_ORIGINAL":
            return report
        project = self.db.fetchone(
            "SELECT security_level,config_json FROM projects WHERE id=?",
            (project_id,),
        ) if self.db is not None else None
        config = json.loads((project or {}).get("config_json") or "{}")
        allowed_endpoints = {
            str(item) for item in config.get("allowed_model_endpoint_ids") or []
        }
        project_security_level = str((project or {}).get("security_level") or "INTERNAL")
        # ONLINE_PUBLIC prompts receive a sanitized PUBLIC envelope.  This must
        # match ContextBuilder and SecurityRouter; checking the project's
        # original classification here produces a false-negative preflight
        # after the safe-online package has already been approved.
        execution_security_level = (
            "PUBLIC" if required == "ONLINE_PUBLIC" else project_security_level
        )
        endpoint_by_id = {
            str(item.get("endpoint_id")): item
            for item in self.pack.endpoints.get("endpoints", [])
        }
        model_by_id = {
            str(item.get("model_id")): item
            for item in self.pack.models.get("models", [])
        }
        profile = self.pack.model_profile(prompt_id)
        candidate_ids = list(profile.get("preferred_models") or []) + list(
            profile.get("fallback_models") or []
        )
        reasons: list[str] = []
        for model_id in candidate_ids:
            model = model_by_id.get(str(model_id))
            if not model or not bool(model.get("enabled", False)):
                reasons.append(f"{model_id}: model disabled or missing")
                continue
            if not str(model.get("provider_model_name") or "").strip():
                reasons.append(f"{model_id}: provider_model_name empty")
                continue
            endpoint = endpoint_by_id.get(str(model.get("endpoint_id") or ""))
            if not endpoint or not bool(endpoint.get("enabled", False)):
                reasons.append(f"{model_id}: endpoint disabled or missing")
                continue
            if str(endpoint.get("environment") or "") != required:
                reasons.append(f"{model_id}: environment mismatch")
                continue
            endpoint_id = str(endpoint.get("endpoint_id") or "")
            if allowed_endpoints and endpoint_id not in allowed_endpoints:
                reasons.append(f"{model_id}: endpoint not allowed by project")
                continue
            if execution_security_level not in set(endpoint.get("allowed_security_levels") or []):
                reasons.append(f"{model_id}: execution security level denied")
                continue
            report.checks.append(
                {
                    "name": f"PROMPT_ROUTE:{prompt_id}",
                    "status": "PASS",
                    "model_id": model_id,
                    "endpoint_id": endpoint_id,
                }
            )
            return report
        report.issues.append(
            DependencyIssue(
                code="PROMPT_MODEL_ROUTE_UNAVAILABLE",
                dependency="MODEL_ENDPOINT",
                message=f"{prompt_id} 没有可用模型路由：" + "；".join(reasons),
                required_settings=(
                    "ONLINE_LLM_ENABLED",
                    "ONLINE_LLM_BASE_URL",
                    "ONLINE_PUBLIC_MODEL",
                    "project.allowed_model_endpoint_ids",
                ) if required == "ONLINE_PUBLIC" else (
                    "OFFLINE_LLM_ENABLED",
                    "OFFLINE_LLM_BASE_URL",
                    "OFFLINE_GENERAL_MODEL",
                    "OFFLINE_CRITIC_MODEL",
                    "project.allowed_model_endpoint_ids",
                ),
                details={"prompt_id": prompt_id, "candidate_reasons": reasons},
            )
        )
        return report

    def _search_report(self, plan: dict[str, Any] | None = None) -> DependencyReport:
        report = DependencyReport("PUBLIC_SEARCH")
        provider = str(self.settings.public_search_provider or "disabled").lower()
        mode = str(self.settings.runtime_mode or "").upper()
        if mode in {"REPLAY", "MOCK"} or (mode == "SIMULATED" and provider == "disabled"):
            report.checks.append({"name": "PUBLIC_SEARCH", "status": "SKIP", "reason": f"runtime mode {mode}"})
            return report
        allowed = {"searxng", "connector", "recorded"}
        if provider == "disabled":
            report.issues.append(
                DependencyIssue(
                    code="PUBLIC_SEARCH_DISABLED",
                    dependency="PUBLIC_SEARCH",
                    message="PUBLIC_SEARCH_PROVIDER=disabled，真实公开资料检索未启用。",
                    required_settings=("PUBLIC_SEARCH_PROVIDER",),
                )
            )
            return report
        if provider not in allowed:
            report.issues.append(
                DependencyIssue(
                    code="PUBLIC_SEARCH_PROVIDER_UNSUPPORTED",
                    dependency="PUBLIC_SEARCH",
                    message=f"不支持的 PUBLIC_SEARCH_PROVIDER：{provider}",
                    required_settings=("PUBLIC_SEARCH_PROVIDER",),
                )
            )
            return report
        if provider == "searxng":
            if not str(self.settings.public_search_base_url or "").strip():
                report.issues.append(
                    DependencyIssue(
                        code="SEARXNG_BASE_URL_MISSING",
                        dependency="PUBLIC_SEARCH",
                        message="SearXNG 模式未配置 PUBLIC_SEARCH_BASE_URL。",
                        required_settings=("PUBLIC_SEARCH_BASE_URL",),
                    )
                )
        elif provider == "connector":
            path = Path(str(self.settings.public_research_connector_file or "")).expanduser()
            self._validate_research_file(report, path, provider="connector", plan=plan)
        elif provider == "recorded":
            path = Path(str(self.settings.public_research_record_file or "")).expanduser()
            self._validate_research_file(report, path, provider="recorded", plan=plan)
        if int(self.settings.public_search_max_results) <= 0:
            report.issues.append(
                DependencyIssue(
                    code="PUBLIC_SEARCH_MAX_RESULTS_INVALID",
                    dependency="PUBLIC_SEARCH",
                    message="PUBLIC_SEARCH_MAX_RESULTS 必须大于 0。",
                    required_settings=("PUBLIC_SEARCH_MAX_RESULTS",),
                )
            )
        if int(self.settings.research_fetch_timeout_seconds) <= 0:
            report.issues.append(
                DependencyIssue(
                    code="RESEARCH_FETCH_TIMEOUT_INVALID",
                    dependency="PUBLIC_SEARCH",
                    message="RESEARCH_FETCH_TIMEOUT_SECONDS 必须大于 0。",
                    required_settings=("RESEARCH_FETCH_TIMEOUT_SECONDS",),
                )
            )
        if int(self.settings.research_max_source_bytes) <= 0:
            report.issues.append(
                DependencyIssue(
                    code="RESEARCH_MAX_SOURCE_BYTES_INVALID",
                    dependency="PUBLIC_SEARCH",
                    message="RESEARCH_MAX_SOURCE_BYTES 必须大于 0。",
                    required_settings=("RESEARCH_MAX_SOURCE_BYTES",),
                )
            )
        return report

    def _validate_research_file(
        self,
        report: DependencyReport,
        path: Path,
        *,
        provider: str,
        plan: dict[str, Any] | None,
    ) -> None:
        setting = "PUBLIC_RESEARCH_CONNECTOR_FILE" if provider == "connector" else "PUBLIC_RESEARCH_RECORD_FILE"
        if not str(path) or str(path) == "." or not path.exists() or not path.is_file():
            report.issues.append(
                DependencyIssue(
                    code=f"PUBLIC_RESEARCH_{provider.upper()}_FILE_NOT_FOUND",
                    dependency="PUBLIC_SEARCH",
                    message=f"{setting} 指向的文件不存在：{path}",
                    required_settings=(setting,),
                )
            )
            return
        payload, error = self._load_json_file(path)
        if error:
            report.issues.append(
                DependencyIssue(
                    code=f"PUBLIC_RESEARCH_{provider.upper()}_FILE_INVALID_JSON",
                    dependency="PUBLIC_SEARCH",
                    message=f"{setting} 不是合法 JSON：{error}",
                    required_settings=(setting,),
                )
            )
            return
        if not isinstance(payload, dict):
            report.issues.append(
                DependencyIssue(
                    code=f"PUBLIC_RESEARCH_{provider.upper()}_FILE_INVALID_ROOT",
                    dependency="PUBLIC_SEARCH",
                    message=f"{setting} 的 JSON 根必须是对象。",
                    required_settings=(setting,),
                )
            )
            return
        field_name = "responses" if provider == "connector" else "sources"
        values = payload.get(field_name)
        if not isinstance(values, list) or not values:
            report.issues.append(
                DependencyIssue(
                    code=f"PUBLIC_RESEARCH_{provider.upper()}_FILE_EMPTY",
                    dependency="PUBLIC_SEARCH",
                    message=f"{setting} 必须包含非空 {field_name} 数组。",
                    required_settings=(setting,),
                )
            )
            return
        if provider == "connector" and plan:
            planned = set(self._queries(plan))
            actual = {
                str(item.get("query") or "").strip()
                for item in values
                if isinstance(item, dict) and str(item.get("query") or "").strip()
            }
            missing = sorted(planned - actual)
            if missing:
                report.issues.append(
                    DependencyIssue(
                        code="CONNECTOR_QUERY_COVERAGE_INCOMPLETE",
                        dependency="PUBLIC_SEARCH",
                        message=f"Connector 文件未覆盖研究计划中的查询：{missing}",
                        required_settings=(setting,),
                        details={"missing_queries": missing},
                    )
                )

    @staticmethod
    def _queries(plan: dict[str, Any]) -> list[str]:
        result: list[str] = []
        for item in (plan or {}).get("queries") or []:
            if isinstance(item, str):
                value = item
            elif isinstance(item, dict):
                value = item.get("query") or item.get("query_text") or item.get("text") or ""
            else:
                value = ""
            value = str(value).strip()
            if value:
                result.append(value)
        return result

    def workflow_report(
        self,
        project_id: str,
        workflow_type: str,
        options: dict[str, Any] | None = None,
    ) -> DependencyReport:
        options = options or {}
        report = DependencyReport(f"WORKFLOW:{workflow_type}")
        # Keep application-level prerequisites in the same report so invalid
        # timeouts, missing Prompt Pack assets and write-path failures are
        # discovered before a workflow consumes model calls or human approvals.
        report.extend(self.application_report(require_export=False))
        if workflow_type in {
            "WF-1_PROJECT_INTAKE",
            "WF-2_TEMPLATE_EXTRACTION",
            "WF-3_HYBRID_ONLINE_ASSIST",
            "WF-4_PROPOSAL_AUTHORING",
            "WF-5_SECURITY_REVIEW_AND_EXPORT",
        }:
            report.extend(self._model_environment_report("OFFLINE_LOCAL"))
        if workflow_type == "WF-3_HYBRID_ONLINE_ASSIST":
            report.extend(self._model_environment_report("ONLINE_PUBLIC"))
            report.extend(self._project_online_report(project_id))
            report.extend(self._search_report())
        if workflow_type == "WF-STAGED_PROPOSAL":
            run_root = Path(str(options.get("run_root") or Path(self.settings.data_dir) / "staged_workflows" / "preflight")).expanduser().resolve()
            parent = run_root.parent
            ok, reason = self._atomic_write_check(parent)
            if not ok:
                report.issues.append(
                    DependencyIssue(
                        code="STAGED_RUN_ROOT_NOT_WRITABLE",
                        dependency="STAGED_WORKFLOW",
                        message=f"Stage 运行目录父路径不可写：{parent}（{reason}）",
                        required_settings=("options.run_root",),
                    )
                )
            ignored_names = {".proposal_agent_workflow_owner.json"}
            if run_root.exists() and any(
                item.name not in ignored_names for item in run_root.iterdir()
            ):
                report.issues.append(
                    DependencyIssue(
                        code="STAGED_RUN_ROOT_NOT_EMPTY",
                        dependency="STAGED_WORKFLOW",
                        message=f"Stage 运行目录必须为空：{run_root}",
                        required_settings=("options.run_root",),
                        retryable=False,
                    )
                )
        return report

    def prompt_report(self, project_id: str, prompt_id: str) -> DependencyReport:
        report = DependencyReport(f"PROMPT:{prompt_id}")
        entry = self.pack.entry(prompt_id)
        environment = str(entry.get("required_environment") or "")
        if environment in {"OFFLINE_LOCAL", "ONLINE_PUBLIC"}:
            report.extend(self._model_environment_report(environment))
        if environment == "ONLINE_PUBLIC":
            report.extend(self._project_online_report(project_id))
        report.extend(self._prompt_route_report(project_id, prompt_id))
        return report

    def step_report(
        self,
        project_id: str,
        workflow_type: str,
        step: dict[str, Any],
        state: dict[str, Any],
        *,
        public_research_plan: dict[str, Any] | None = None,
    ) -> DependencyReport:
        """Check only dependencies that can affect the next executable step."""
        step_type = str(step.get("type") or "PROMPT")
        report = DependencyReport(f"STEP:{workflow_type}:{step_type}")
        # Recheck mutable runtime dependencies at every executable step.  The
        # checks are local and deterministic; network availability is probed
        # only through the explicit /api/config/probe endpoint.
        report.extend(self._storage_report())
        report.extend(self._runtime_policy_report())
        if step_type == "PUBLIC_SEARCH":
            report.extend(self.public_search_report(public_research_plan))
            return report
        prompt_id = str(step.get("prompt_id") or "")
        if prompt_id:
            report.extend(self.prompt_report(project_id, prompt_id))
        return report

    def public_search_report(self, plan: dict[str, Any] | None = None) -> DependencyReport:
        return self._search_report(plan)

    def staged_transition_report(self, next_stage: str, state: dict[str, Any]) -> DependencyReport:
        report = DependencyReport(f"STAGED_TRANSITION:{next_stage}")
        options = state.get("options") or {}
        if next_stage == "stage4a":
            path = str(options.get("evidence_inputs") or "").strip()
            if not path or not Path(path).expanduser().resolve().is_file():
                report.issues.append(
                    DependencyIssue(
                        code="STAGE4A_EVIDENCE_INPUTS_MISSING",
                        dependency="STAGED_WORKFLOW",
                        message="Stage 4A 需要存在的 options.evidence_inputs 文件。",
                        required_settings=("options.evidence_inputs",),
                    )
                )
        if next_stage == "stage5":
            generated = Path(state["run_root"]) / "stage4a" / "outputs" / "stage4a_evidence_completion.json"
            configured = str(options.get("evidence_completion") or "").strip()
            if not generated.is_file() and (not configured or not Path(configured).expanduser().resolve().is_file()):
                report.issues.append(
                    DependencyIssue(
                        code="STAGE5_EVIDENCE_COMPLETION_MISSING",
                        dependency="STAGED_WORKFLOW",
                        message="Stage 5 需要已完成的 Stage 4A 输出或 options.evidence_completion。",
                        required_settings=("options.evidence_completion",),
                    )
                )
        if next_stage == "stage8":
            report.extend(self.application_report(require_export=True))
        return report

    def classify_runtime_error(
        self,
        exc: Exception | str,
        *,
        dependency_hint: str | None = None,
    ) -> DependencyIssue | None:
        message = str(exc)
        text = message.lower()
        hint = str(dependency_hint or "").upper()
        category = str(getattr(exc, "category", "") or "").upper()
        error_code = str(getattr(exc, "error_code", "") or "")
        details = dict(getattr(exc, "details", {}) or {})

        # Typed public-research failures take precedence over broad string hints.
        # A plan-contract or archive-integrity failure happens *inside* the
        # PUBLIC_SEARCH step, but it is not repaired by editing .env.  Only the
        # CONFIGURATION category is allowed to produce WAITING_CONFIGURATION.
        if category == "CONFIGURATION":
            return DependencyIssue(
                code=error_code or "PUBLIC_SEARCH_RUNTIME_UNAVAILABLE",
                dependency="PUBLIC_SEARCH",
                message=message,
                required_settings=("PUBLIC_SEARCH_PROVIDER", "PUBLIC_SEARCH_BASE_URL", "PUBLIC_RESEARCH_CONNECTOR_FILE", "PUBLIC_RESEARCH_RECORD_FILE"),
                details=details,
            )
        if category in {"PLAN_CONTRACT", "RETRIEVAL", "SECURITY", "INTEGRITY"}:
            return None

        # Model-specific transport/stream markers must win over generic words
        # such as "timeout", which also occur in search failures.  The prompt
        # execution path supplies the required model environment as the hint;
        # typed public-search errors were already handled above.
        if any(marker in text for marker in self.MODEL_CONFIGURATION_MARKERS):
            settings = (
                ("ONLINE_LLM_ENABLED", "ONLINE_LLM_BASE_URL", "ONLINE_LLM_API_KEY", "ONLINE_PUBLIC_MODEL")
                if hint == "ONLINE_PUBLIC"
                else ("OFFLINE_LLM_ENABLED", "OFFLINE_LLM_BASE_URL", "OFFLINE_LLM_API_KEY", "OFFLINE_GENERAL_MODEL", "OFFLINE_CRITIC_MODEL")
            )
            return DependencyIssue(
                code="MODEL_ENDPOINT_RUNTIME_UNAVAILABLE",
                dependency="MODEL_ENDPOINT",
                message=message,
                required_settings=settings,
            )
        if any(marker in text for marker in self.SEARCH_CONFIGURATION_MARKERS):
            return DependencyIssue(
                code="PUBLIC_SEARCH_RUNTIME_UNAVAILABLE",
                dependency="PUBLIC_SEARCH",
                message=message,
                required_settings=("PUBLIC_SEARCH_PROVIDER", "PUBLIC_SEARCH_BASE_URL", "PUBLIC_RESEARCH_CONNECTOR_FILE", "PUBLIC_RESEARCH_RECORD_FILE"),
            )
        if any(marker in text for marker in self.STORAGE_CONFIGURATION_MARKERS):
            return DependencyIssue(
                code="RUNTIME_STORAGE_UNAVAILABLE",
                dependency="STORAGE",
                message=message,
                required_settings=("APP_DATA_DIR", "MODEL_CALL_EVIDENCE_DIR", "RUNTIME_EXPORT_EVIDENCE_DIR"),
            )
        if any(marker in text for marker in self.EXPORT_CONFIGURATION_MARKERS):
            return DependencyIssue(
                code="RENDER_OR_EXPORT_DEPENDENCY_UNAVAILABLE",
                dependency="EXPORT",
                message=message,
                required_settings=("MERMAID_JS_PATH", "MERMAID_BROWSER_EXECUTABLE", "LIBREOFFICE_EXECUTABLE", "STAGE8_FONT_SANS_PATH", "STAGE8_FONT_SERIF_PATH"),
            )
        return None

    def report_from_runtime_error(
        self,
        exc: Exception | str,
        *,
        dependency_hint: str | None = None,
        scope: str = "RUNTIME_ERROR",
    ) -> DependencyReport | None:
        issue = self.classify_runtime_error(exc, dependency_hint=dependency_hint)
        if issue is None:
            return None
        return DependencyReport(scope=scope, issues=[issue])

    def probe(self, *, timeout_seconds: int | None = None) -> DependencyReport:
        report = self.application_report(require_export=False)
        timeout = max(1, int(timeout_seconds or os.getenv("DEPENDENCY_PROBE_TIMEOUT_SECONDS", "10")))
        if str(self.settings.runtime_mode).upper() == "LIVE":
            for environment in ("OFFLINE_LOCAL", "ONLINE_PUBLIC"):
                env_report = self._model_environment_report(environment)
                report.extend(env_report)
                for endpoint in self.pack.endpoints.get("endpoints", []):
                    if endpoint.get("environment") != environment or not bool(endpoint.get("enabled", False)):
                        continue
                    base_url = str(endpoint.get("base_url") or "").rstrip("/")
                    if not base_url:
                        continue
                    secret_name = str(endpoint.get("api_key_secret") or "")
                    api_key = os.getenv(secret_name, "") if secret_name else ""
                    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                    try:
                        response = httpx.get(f"{base_url}/models", headers=headers, timeout=timeout)
                        response.raise_for_status()
                        payload = response.json()
                        models = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict)]
                        report.checks.append({"name": f"MODEL_PROBE:{endpoint.get('endpoint_id')}", "status": "PASS", "models": models[:20]})
                    except Exception as exc:
                        report.issues.append(
                            DependencyIssue(
                                code="MODEL_ENDPOINT_PROBE_FAILED",
                                dependency="MODEL_ENDPOINT",
                                message=f"{endpoint.get('endpoint_id')} 探测失败：{type(exc).__name__}: {exc}",
                                required_settings=(secret_name,) if secret_name else (),
                                details={"endpoint_id": endpoint.get("endpoint_id"), "base_url": base_url},
                            )
                        )
        search = self._search_report()
        report.extend(search)
        if self.settings.public_search_provider == "searxng" and not search.blocking_issues:
            try:
                params = {
                    "q": "proposal agent preflight",
                    "format": "json",
                    "language": "all",
                    "safesearch": 1,
                }
                engines = str(getattr(self.settings, "public_search_engines", "") or "").strip()
                if engines:
                    params["engines"] = engines
                with httpx.Client(timeout=timeout, trust_env=False) as client:
                    response = client.get(
                        f"{self.settings.public_search_base_url}/search",
                        params=params,
                    )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload.get("results"), list):
                    raise ValueError("response does not contain results[]")
                report.checks.append({"name": "SEARXNG_JSON_API", "status": "PASS", "result_count": len(payload.get("results") or [])})
            except Exception as exc:
                report.issues.append(
                    DependencyIssue(
                        code="SEARXNG_JSON_API_UNAVAILABLE",
                        dependency="PUBLIC_SEARCH",
                        message=f"SearXNG JSON API 探测失败：{type(exc).__name__}: {exc}",
                        required_settings=("PUBLIC_SEARCH_BASE_URL",),
                    )
                )
        return report
