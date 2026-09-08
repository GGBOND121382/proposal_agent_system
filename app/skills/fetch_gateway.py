from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx

from ..util import sha256_bytes


class FetchGatewayError(RuntimeError):
    category = "RETRIEVAL"
    error_code = "FETCH_GATEWAY_ERROR"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details or {})


class FetchGatewaySecurityError(FetchGatewayError):
    category = "SECURITY"
    error_code = "FETCH_GATEWAY_SECURITY_ERROR"


class FetchGatewayRetrievalError(FetchGatewayError):
    category = "RETRIEVAL"
    error_code = "FETCH_GATEWAY_RETRIEVAL_ERROR"


@dataclass(frozen=True)
class FetchedDocument:
    requested_url: str
    final_url: str
    content_type: str
    http_status: int
    raw_bytes: bytes
    fetch_mode: str = "HTTP"
    raw_path: str | None = None
    fallback_reason: str | None = None
    browser_status: str | None = None
    blockage_type: str | None = None
    cache_hit: bool = False

    @property
    def byte_size(self) -> int:
        return len(self.raw_bytes)

    @property
    def raw_sha256(self) -> str:
        return sha256_bytes(self.raw_bytes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "content_type": self.content_type,
            "http_status": self.http_status,
            "fetch_mode": self.fetch_mode,
            "raw_path": self.raw_path,
            "fallback_reason": self.fallback_reason,
            "browser_status": self.browser_status,
            "blockage_type": self.blockage_type,
            "cache_hit": self.cache_hit,
            "byte_size": self.byte_size,
            "raw_sha256": self.raw_sha256,
        }


def validate_public_url(url: str, *, resolve_dns: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FetchGatewaySecurityError("Only public HTTP(S) URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise FetchGatewaySecurityError("Credentials in public URLs are prohibited")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise FetchGatewaySecurityError("Local addresses are prohibited")
    try:
        ip = ipaddress.ip_address(host)
        addresses = [ip]
    except ValueError:
        addresses = []
        if resolve_dns:
            try:
                addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None)]
            except socket.gaierror as exc:
                raise FetchGatewayRetrievalError(f"DNS resolution failed for {host}") from exc
    for address in addresses:
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        ):
            raise FetchGatewaySecurityError(f"Private/reserved address is prohibited: {address}")


class HttpFetchGateway:
    """Fast-path public HTTP/PDF fetcher used before the Phase-2 browser fallback."""

    def __init__(
        self,
        settings,
        *,
        client_factory: Callable[..., Any] = httpx.Client,
        url_validator: Callable[..., None] = validate_public_url,
        browser_worker: Any | None = None,
    ):
        self.settings = settings
        self.client_factory = client_factory
        self.url_validator = url_validator
        self.browser_worker = browser_worker
        if self.browser_worker is None and bool(
            getattr(settings, "browser_fetch_fallback_enabled", False)
        ):
            from .browser_worker import BrowserWorker

            self.browser_worker = BrowserWorker(settings)

    def fetch(self, url: str) -> FetchedDocument:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.1",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        limit = int(self.settings.research_max_source_bytes)
        try:
            with self.client_factory(
                timeout=self.settings.research_fetch_timeout_seconds,
                follow_redirects=False,
                headers=headers,
            ) as client:
                current_url = url
                for redirect_count in range(11):
                    # Validate before every network request so a public URL
                    # cannot redirect through a private intermediate address.
                    self.url_validator(current_url, resolve_dns=True)
                    with client.stream("GET", current_url) as response:
                        status = int(response.status_code)
                        location = str(response.headers.get("location") or "").strip()
                        if status in {301, 302, 303, 307, 308} and location:
                            if redirect_count >= 10:
                                raise FetchGatewayRetrievalError(
                                    "Public source exceeded 10 redirects",
                                    details={"url": url},
                                )
                            next_url = urljoin(current_url, location)
                            self.url_validator(next_url, resolve_dns=True)
                            current_url = next_url
                            continue
                        response.raise_for_status()
                        final_url = str(response.url or current_url)
                        self.url_validator(final_url, resolve_dns=True)
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > limit:
                                raise FetchGatewayRetrievalError(
                                    f"Source exceeds {limit} bytes",
                                    details={"url": url, "limit": limit},
                                )
                            chunks.append(chunk)
                        content_type = response.headers.get(
                            "content-type", "application/octet-stream"
                        ).split(";", 1)[0].lower()
                        fetched = FetchedDocument(
                            requested_url=url,
                            final_url=final_url,
                            content_type=content_type,
                            http_status=status,
                            raw_bytes=b"".join(chunks),
                        )
                        return self._browser_fallback(fetched)
                raise FetchGatewayRetrievalError(
                    "Public source redirect processing failed",
                    details={"url": url},
                )
        except FetchGatewayError:
            raise
        except (httpx.TimeoutException, httpx.RequestError, httpx.HTTPStatusError) as exc:
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response is not None
                and int(exc.response.status_code) in {403, 429}
                and self.browser_worker is not None
            ):
                recovered = self._browser_error_fallback(url, int(exc.response.status_code))
                if recovered is not None:
                    return recovered
            raise FetchGatewayRetrievalError(
                f"Public source fetch failed: {exc}",
                details={"url": url, "exception_type": type(exc).__name__},
            ) from exc

    def _browser_error_fallback(self, url: str, http_status: int) -> FetchedDocument | None:
        """Retry a hard HTTP 403/429 with a real browser render.

        Anti-bot pages reject the plain HTTP client before any content exists,
        so the extraction-quality fallback never fires for them.  A recovered
        page is returned as PLAYWRIGHT_RENDERED; a blocked or failed render
        returns None and the caller raises the original retrieval error.
        """
        try:
            rendered = self.browser_worker.fetch(url)
        except Exception:
            return None
        if rendered.status == "PASS" and rendered.html.strip():
            return FetchedDocument(
                requested_url=url,
                final_url=rendered.final_url or url,
                content_type=rendered.content_type or "text/html",
                http_status=rendered.http_status or http_status,
                raw_bytes=rendered.html.encode("utf-8"),
                fetch_mode="PLAYWRIGHT_RENDERED",
                fallback_reason=f"HTTP_{http_status}_BROWSER_RECOVERED",
                browser_status=rendered.status,
                blockage_type=rendered.blockage_type,
                cache_hit=rendered.cache_hit,
            )
        return None

    def _browser_fallback(self, fetched: FetchedDocument) -> FetchedDocument:
        if self.browser_worker is None:
            return fetched
        # Local import avoids a module cycle: ContentExtractor consumes the
        # FetchedDocument contract while FetchGateway owns the fallback policy.
        from .content_extraction import ContentExtractor

        extractor = ContentExtractor()
        extracted = extractor.extract(fetched)
        reason = extractor.browser_fallback_reason(fetched, extracted)
        if reason is None:
            return fetched
        rendered = self.browser_worker.fetch(fetched.final_url)
        if rendered.status == "PASS" and rendered.html.strip():
            return FetchedDocument(
                requested_url=fetched.requested_url,
                final_url=rendered.final_url,
                content_type=rendered.content_type or "text/html",
                http_status=rendered.http_status or fetched.http_status,
                raw_bytes=rendered.html.encode("utf-8"),
                fetch_mode="PLAYWRIGHT_RENDERED",
                fallback_reason=reason,
                browser_status=rendered.status,
                blockage_type=rendered.blockage_type,
                cache_hit=rendered.cache_hit,
            )
        # Keep the search hit available as an explicitly snippet-only record;
        # the caller must not represent the static shell as fetched full text.
        return FetchedDocument(
            requested_url=fetched.requested_url,
            final_url=rendered.final_url or fetched.final_url,
            content_type=fetched.content_type,
            http_status=rendered.http_status or fetched.http_status,
            raw_bytes=fetched.raw_bytes,
            fetch_mode="SNIPPET_ONLY",
            fallback_reason=reason,
            browser_status=rendered.status,
            blockage_type=rendered.blockage_type,
            cache_hit=rendered.cache_hit,
        )
