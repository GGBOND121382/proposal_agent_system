from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from .base import SkillContext, SkillResult
from ..util import safe_filename, sha256_bytes, sha256_text, utc_now, write_json


class MermaidRenderError(RuntimeError):
    pass


class MermaidRenderSkill:
    skill_id = "mermaid.render"
    version = "1.1.0"
    description = "Validate, archive and render Mermaid source into editable .mmd, SVG and PNG artifacts."

    _ALLOWED_STARTS = (
        "flowchart ", "graph ", "sequenceDiagram", "stateDiagram", "stateDiagram-v2",
        "classDiagram", "erDiagram", "journey", "gantt", "timeline", "mindmap",
        "quadrantChart", "requirementDiagram", "C4Context", "C4Container",
    )

    def __init__(self, settings):
        self.settings = settings
        self.mermaid_js = Path(settings.mermaid_js_path)
        if not self.mermaid_js.exists():
            raise FileNotFoundError(f"Bundled Mermaid runtime not found: {self.mermaid_js}")
        self._worker: subprocess.Popen[str] | None = None
        self._worker_lock = threading.Lock()
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._worker_request_count = 0
        # Keep one browser for a complete proposal.  All calls are dispatched by
        # DiagramEnrichmentService to one dedicated thread, avoiding Playwright
        # and pipe ownership changes between asyncio worker threads.
        self._max_requests_per_worker = 10
        atexit.register(self.close)

    def run(self, payload: dict[str, Any], context: SkillContext) -> SkillResult:
        source = self._normalize_source(str(payload.get("mermaid_source") or ""))
        caption = str(payload.get("caption") or "结构图").strip()
        section_id = safe_filename(str(payload.get("section_id") or "section"))
        width_cm = float(payload.get("width_cm") or 15.0)
        if not source:
            raise MermaidRenderError("mermaid_source is empty")
        self._validate_source(source)

        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        root = Path(context.data_dir) / "diagram_artifacts" / safe_filename(context.project_id) / section_id
        root.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(f"{section_id}-{digest}")
        mmd_path = root / f"{stem}.mmd"
        svg_path = root / f"{stem}.svg"
        png_path = root / f"{stem}.png"
        meta_path = root / f"{stem}.json"
        mmd_path.write_text(source + "\n", encoding="utf-8")

        warnings: list[str] = []
        cache_hit = False
        if svg_path.exists() and png_path.exists() and meta_path.exists() and png_path.stat().st_size >= 100:
            try:
                previous = json.loads(meta_path.read_text(encoding="utf-8"))
                cache_hit = previous.get("source_sha256") == sha256_text(source)
            except (OSError, json.JSONDecodeError):
                cache_hit = False
        if cache_hit:
            svg_text = svg_path.read_text(encoding="utf-8")
            warnings.append("命中Mermaid渲染缓存，复用同一源码的SVG/PNG工件。")
        else:
            try:
                svg_text = self._render(source, svg_path, png_path)
            except Exception as first_error:
                repaired = self._repair_source(source)
                if repaired == source:
                    raise
                warnings.append(f"首次渲染失败，已应用确定性语法清理：{first_error}")
                mmd_path.write_text(repaired + "\n", encoding="utf-8")
                source = repaired
                svg_text = self._render(source, svg_path, png_path)

        metadata = {
            "schema_version": "1.0",
            "skill_id": self.skill_id,
            "skill_version": self.version,
            "project_id": context.project_id,
            "workflow_id": context.workflow_id,
            "section_id": section_id,
            "caption": caption,
            "width_cm": width_cm,
            "argument_purpose": payload.get("argument_purpose"),
            "claim_id": payload.get("claim_id"),
            "evidence_ids": list(payload.get("evidence_ids") or []),
            "section_contract_id": payload.get("section_contract_id"),
            "fallback_reason": payload.get("fallback_reason"),
            "created_at": utc_now(),
            "mermaid_version": self._mermaid_version(),
            "source_path": str(mmd_path),
            "source_sha256": sha256_text(source),
            "svg_path": str(svg_path),
            "svg_sha256": sha256_text(svg_text),
            "png_path": str(png_path),
            "png_sha256": sha256_bytes(png_path.read_bytes()),
            "browser": self._browser_executable(),
            "cache_hit": cache_hit,
            "worker_rotation_limit": self._max_requests_per_worker,
            "warnings": warnings,
        }
        write_json(meta_path, metadata)
        output = {
            **metadata,
            "metadata_path": str(meta_path),
            "figure_marker": f"[[FIGURE]]{png_path.as_posix()}|{caption}|{width_cm}|source={mmd_path.as_posix()}",
        }
        return SkillResult(status="PASS", output=output, warnings=warnings, artifacts=[str(mmd_path), str(svg_path), str(png_path), str(meta_path)])

    def _render(self, source: str, svg_path: Path, png_path: Path) -> str:
        timeout_seconds = max(10, int(self.settings.skill_timeout_seconds))
        request_id = uuid.uuid4().hex
        request = {
            "request_id": request_id,
            "source_path": str(svg_path.with_suffix(".render.mmd")),
            "svg_path": str(svg_path),
            "png_path": str(png_path),
            "timeout_ms": min(timeout_seconds * 1000, 45000),
        }
        Path(request["source_path"]).write_text(source, encoding="utf-8")
        try:
            with self._worker_lock:
                if self._worker_request_count >= self._max_requests_per_worker:
                    self._terminate_worker()
                self._ensure_worker(timeout_seconds)
                assert self._worker is not None and self._worker.stdin is not None
                self._worker.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self._worker.stdin.flush()
                try:
                    response = self._responses.get(timeout=timeout_seconds + 10)
                except queue.Empty as exc:
                    self._terminate_worker()
                    raise MermaidRenderError(f"Mermaid worker timed out after {timeout_seconds + 10}s") from exc
                if response.get("request_id") != request_id:
                    self._terminate_worker()
                    raise MermaidRenderError("Mermaid worker response order mismatch")
                if not response.get("ok"):
                    raise MermaidRenderError(f"Mermaid worker failed: {response.get('error')}\n{response.get('traceback','')}")
                self._worker_request_count += 1
        finally:
            Path(request["source_path"]).unlink(missing_ok=True)
        if not svg_path.exists() or not png_path.exists() or png_path.stat().st_size < 100:
            raise MermaidRenderError("Mermaid worker did not produce valid SVG/PNG artifacts")
        return svg_path.read_text(encoding="utf-8")

    def _ensure_worker(self, timeout_seconds: int) -> None:
        if self._worker is not None and self._worker.poll() is None:
            return
        self._terminate_worker()
        self._responses = queue.Queue()
        self._worker_request_count = 0
        self._worker = subprocess.Popen(
            [sys.executable, "-m", "app.skills.mermaid_worker", "--server"],
            cwd=str(Path(__file__).resolve().parents[2]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        assert self._worker.stdin is not None and self._worker.stdout is not None
        self._reader_thread = threading.Thread(target=self._read_worker_responses, args=(self._worker,), daemon=True)
        self._reader_thread.start()
        self._worker.stdin.write(json.dumps({"mermaid_js": str(self.mermaid_js), "browser": self._browser_executable()}) + "\n")
        self._worker.stdin.flush()
        try:
            ready = self._responses.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            self._terminate_worker()
            raise MermaidRenderError("Mermaid worker failed to start") from exc
        if not ready.get("ready"):
            self._terminate_worker()
            raise MermaidRenderError(f"Mermaid worker initialization failed: {ready}")

    def _read_worker_responses(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                self._responses.put(json.loads(line))
            except json.JSONDecodeError:
                self._responses.put({"ok": False, "error": f"Invalid worker response: {line[-500:]}"})

    def _terminate_worker(self) -> None:
        process = self._worker
        self._worker = None
        self._worker_request_count = 0
        if process is None:
            return
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass
        for stream in (process.stdin, process.stdout):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass

    def close(self) -> None:
        with self._worker_lock:
            self._terminate_worker()

    def _browser_executable(self) -> str:
        configured = str(getattr(self.settings, "mermaid_browser_executable", "") or "").strip()
        if configured:
            path = Path(configured)
            if path.exists():
                return str(path)
            found = shutil.which(configured)
            if found:
                return found
        candidates = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "msedge", "msedge.exe", "chrome", "chrome.exe"]
        if os.name == "nt":
            candidates.extend([r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe"])
        for candidate in candidates:
            if Path(candidate).exists():
                return str(Path(candidate))
            found = shutil.which(candidate)
            if found:
                return found
        raise MermaidRenderError("No Chromium/Chrome/Edge executable found; set MERMAID_BROWSER_EXECUTABLE")

    def _mermaid_version(self) -> str:
        version_file = self.mermaid_js.parent / "VERSION"
        return version_file.read_text(encoding="utf-8").strip() if version_file.exists() else "unknown"

    @classmethod
    def _normalize_source(cls, source: str) -> str:
        source = source.strip()
        source = re.sub(r"^```(?:mermaid)?\s*", "", source, flags=re.I)
        source = re.sub(r"\s*```$", "", source)
        source = source.replace("\r\n", "\n").replace("\r", "\n")
        source = source.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
        return source.strip()

    @classmethod
    def _validate_source(cls, source: str) -> None:
        first = next((line.strip() for line in source.splitlines() if line.strip()), "")
        if not any(first.startswith(prefix) for prefix in cls._ALLOWED_STARTS):
            raise MermaidRenderError(f"Unsupported Mermaid diagram type: {first[:80]}")
        if len(source) > 30000:
            raise MermaidRenderError("Mermaid source exceeds 30000 characters")
        lowered = source.lower()
        for token in ["click ", "javascript:", "<script", "%%{init", "%%{config"]:
            if token in lowered:
                raise MermaidRenderError(f"Prohibited Mermaid directive: {token.strip()}")
        node_lines = [line for line in source.splitlines() if "-->" in line or "---" in line]
        if len(node_lines) > 250:
            raise MermaidRenderError("Mermaid diagram is too complex; split it into smaller diagrams")

    @classmethod
    def _repair_source(cls, source: str) -> str:
        lines = []
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped or stripped.lower().startswith("click "):
                continue
            stripped = stripped.replace("：", ":").replace("；", ";")
            stripped = re.sub(r"\s+", " ", stripped) if "-->" in stripped else stripped
            lines.append(stripped)
        return "\n".join(lines).strip()
