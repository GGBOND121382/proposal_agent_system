from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .fetch_gateway import FetchGatewaySecurityError, validate_public_url
from ..util import sha256_text, utc_now


@dataclass(frozen=True)
class BrowserPageResult:
    requested_url: str
    final_url: str
    status: str
    http_status: int | None
    content_type: str
    html: str
    text: str
    title: str
    blockage_type: str | None = None
    error: str | None = None
    cache_hit: bool = False
    rendered_at: str = ""

    @property
    def html_sha256(self) -> str:
        return sha256_text(self.html)

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        value = asdict(self)
        value["html_sha256"] = self.html_sha256
        value["html_length"] = len(self.html)
        value["text_length"] = len(self.text)
        if not include_content:
            value.pop("html", None)
            value.pop("text", None)
        return value


class BrowserWorker:
    """One reusable, policy-constrained Playwright context per research batch.

    Playwright is imported only when the first uncached navigation is required.
    This keeps replay/offline imports working while dependency preflight can report
    a missing browser runtime before a live search step starts.
    """

    BLOCK_MARKERS = {
        "CAPTCHA": (
            "verify you are human",
            "unusual traffic",
            "solve the challenge to continue",
            "one last step",
            "人机验证",
            "验证码",
            "请解决以下难题以继续",
        ),
        "LOGIN_WALL": (
            "sign in to continue",
            "log in to continue",
            "login required",
            "登录后继续",
            "请先登录",
        ),
        "ROBOTS_DENIED": (
            "access denied",
            "request blocked",
            "automated queries",
        ),
    }

    def __init__(
        self,
        settings,
        *,
        cache_dir: str | Path | None = None,
        renderer: Callable[[str, int], BrowserPageResult] | None = None,
        url_validator: Callable[..., None] = validate_public_url,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.cache_dir = Path(
            cache_dir
            or Path(getattr(settings, "data_dir", Path("data"))) / "browser_cache"
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.renderer = renderer
        self.url_validator = url_validator
        self.sleep = sleep
        self.monotonic = monotonic
        self._lock = threading.RLock()
        self._last_request_by_origin: dict[str, float] = {}
        self._validated_urls: set[str] = set()
        self._playwright = None
        self._browser = None
        self._context = None

    def __enter__(self) -> "BrowserWorker":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            for resource in (self._context, self._browser, self._playwright):
                if resource is None:
                    continue
                try:
                    resource.close() if hasattr(resource, "close") else resource.stop()
                except Exception:
                    pass
            self._context = None
            self._browser = None
            self._playwright = None

    def fetch(self, url: str, *, use_cache: bool = True) -> BrowserPageResult:
        self._validate(url)
        with self._lock:
            if use_cache:
                cached = self._read_cache(url)
                if cached is not None:
                    return replace(cached, cache_hit=True)
            self._rate_limit(url)
            timeout_ms = max(
                1000,
                int(getattr(self.settings, "browser_navigation_timeout_seconds", 45))
                * 1000,
            )
            try:
                result = (
                    self.renderer(url, timeout_ms)
                    if self.renderer is not None
                    else self._render_with_playwright(url, timeout_ms)
                )
            except FetchGatewaySecurityError:
                raise
            except Exception as exc:
                result = BrowserPageResult(
                    requested_url=url,
                    final_url=url,
                    status="ERROR",
                    http_status=None,
                    content_type="text/html",
                    html="",
                    text="",
                    title="",
                    blockage_type=self._exception_blockage(exc),
                    error=f"{type(exc).__name__}: {exc}",
                    rendered_at=utc_now(),
                )
            self._validate(result.final_url or url)
            classified = self._classify(result)
            self._write_cache(classified)
            return classified

    def _validate(self, url: str) -> None:
        # DNS resolution is cached per exact URL for the lifetime of a batch;
        # every distinct navigation/subrequest still crosses this boundary.
        if url in self._validated_urls:
            return
        self.url_validator(url, resolve_dns=True)
        self._validated_urls.add(url)

    def _rate_limit(self, url: str) -> None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc.lower()}"
        interval = max(0.0, float(getattr(self.settings, "browser_rate_limit_seconds", 1.5)))
        now = self.monotonic()
        last = self._last_request_by_origin.get(origin)
        if last is not None and now - last < interval:
            self.sleep(interval - (now - last))
            now = self.monotonic()
        self._last_request_by_origin[origin] = now

    def _ensure_context(self):
        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed; install the locked project dependency"
            ) from exc
        self._playwright = sync_playwright().start()
        launch_options: dict[str, Any] = {
            "headless": bool(getattr(self.settings, "browser_headless", True)),
        }
        executable = self._find_executable(
            str(getattr(self.settings, "browser_executable", "") or "").strip()
        )
        if executable:
            launch_options["executable_path"] = executable
        self._browser = self._playwright.chromium.launch(**launch_options)
        self._context = self._browser.new_context(
            ignore_https_errors=False,
            java_script_enabled=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0 Safari/537.36 ProposalAgentResearch/1.0"
            ),
        )
        return self._context

    @staticmethod
    def _find_executable(configured: str) -> str | None:
        if configured:
            path = Path(configured).expanduser()
            if path.is_file():
                return str(path.resolve())
            resolved = shutil.which(configured)
            if resolved:
                return resolved
        for name in ("chromium", "chromium-browser", "google-chrome", "chrome", "msedge"):
            resolved = shutil.which(name)
            if resolved:
                return resolved
        candidates = (
            Path(os.getenv("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.getenv("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.getenv("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.getenv("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate.resolve())
        return None

    def _render_with_playwright(self, url: str, timeout_ms: int) -> BrowserPageResult:
        context = self._ensure_context()
        page = context.new_page()
        blocked_subrequests: list[dict[str, str]] = []

        def handle_route(route) -> None:
            request_url = str(route.request.url)
            try:
                self._validate(request_url)
            except Exception as exc:
                blocked_subrequests.append(
                    {"url": request_url, "reason": f"{type(exc).__name__}: {exc}"}
                )
                route.abort("blockedbyclient")
                return
            if route.request.resource_type in {"image", "media", "font"}:
                route.abort("blockedbyclient")
                return
            route.continue_()

        page.route("**/*", handle_route)
        response = None
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(min(1500, max(250, timeout_ms // 20)))
            final_url = str(page.url or url)
            self._validate(final_url)
            html = page.content()
            try:
                text = page.locator("body").inner_text(timeout=min(timeout_ms, 5000))
            except Exception:
                text = ""
            title = page.title()
            status = int(response.status) if response is not None else None
            content_type = "text/html"
            if response is not None:
                content_type = str(response.headers.get("content-type") or "text/html").split(";", 1)[0]
            error = None
            if blocked_subrequests:
                error = f"blocked_subrequests={len(blocked_subrequests)}"
            return BrowserPageResult(
                requested_url=url,
                final_url=final_url,
                status="PASS",
                http_status=status,
                content_type=content_type,
                html=html,
                text=text,
                title=title,
                error=error,
                rendered_at=utc_now(),
            )
        finally:
            page.close()

    @classmethod
    def _classify(cls, result: BrowserPageResult) -> BrowserPageResult:
        if result.status == "ERROR":
            return result
        if result.http_status == 403:
            return replace(result, status="PROVIDER_BLOCKED", blockage_type="HTTP_403")
        if result.http_status == 429:
            return replace(result, status="PROVIDER_BLOCKED", blockage_type="HTTP_429")
        combined = f"{result.final_url}\n{result.title}\n{result.text}".lower()
        parsed = urlparse(result.final_url)
        path = parsed.path.lower()
        if any(token in path for token in ("/captcha", "/challenge")):
            return replace(result, status="PROVIDER_BLOCKED", blockage_type="CAPTCHA")
        if any(token in path for token in ("/login", "/signin", "/auth/")) and len(result.text) < 5000:
            return replace(result, status="PROVIDER_BLOCKED", blockage_type="LOGIN_WALL")
        for blockage_type, markers in cls.BLOCK_MARKERS.items():
            if any(marker in combined for marker in markers):
                return replace(
                    result,
                    status="PROVIDER_BLOCKED",
                    blockage_type=blockage_type,
                )
        if not result.html.strip() or not result.text.strip():
            return replace(
                result,
                status="ERROR",
                blockage_type="DYNAMIC_EXTRACTION_FAILED",
                error=result.error or "Rendered page contains no readable DOM text",
            )
        return result

    @staticmethod
    def _exception_blockage(exc: Exception) -> str:
        text = f"{type(exc).__name__}: {exc}".lower()
        if "timeout" in text:
            return "TIMEOUT"
        return "NAVIGATION_ERROR"

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = sha256_text(url)
        return self.cache_dir / f"{key}.json", self.cache_dir / f"{key}.html"

    def _read_cache(self, url: str) -> BrowserPageResult | None:
        metadata_path, html_path = self._cache_paths(url)
        if not metadata_path.is_file() or not html_path.is_file():
            return None
        ttl = int(getattr(self.settings, "browser_cache_ttl_seconds", 86400))
        if ttl <= 0 or time.time() - metadata_path.stat().st_mtime > ttl:
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["html"] = html_path.read_text(encoding="utf-8")
            metadata.setdefault("text", "")
            allowed = set(BrowserPageResult.__dataclass_fields__)
            return BrowserPageResult(**{key: value for key, value in metadata.items() if key in allowed})
        except (OSError, ValueError, TypeError):
            return None

    def _write_cache(self, result: BrowserPageResult) -> None:
        metadata_path, html_path = self._cache_paths(result.requested_url)
        html_path.write_bytes(result.html.encode("utf-8"))
        metadata = asdict(replace(result, html="", cache_hit=False))
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
