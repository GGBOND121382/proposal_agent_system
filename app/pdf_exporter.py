from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .util import sha256_bytes, utc_now, write_json


class PdfConversionError(RuntimeError):
    pass


class PdfConverter:
    """Convert DOCX to PDF with an auditable, fail-closed LibreOffice invocation."""

    def __init__(self, settings):
        self.settings = settings

    def convert(self, docx_path: Path, *, timeout_seconds: int = 240) -> Path:
        docx_path = docx_path.resolve()
        if not docx_path.is_file() or docx_path.suffix.lower() != ".docx":
            raise PdfConversionError(f"DOCX input is missing or invalid: {docx_path.name}")
        executable = shutil.which("libreoffice") or shutil.which("soffice")
        if not executable:
            raise PdfConversionError(
                "LibreOffice/soffice is unavailable; PDF conversion cannot be silently skipped"
            )

        pdf_path = docx_path.with_suffix(".pdf")
        pdf_path.unlink(missing_ok=True)
        started_at = utc_now()
        with tempfile.TemporaryDirectory(prefix="proposal-agent-lo-") as profile_dir:
            command = [
                executable,
                f"-env:UserInstallation={Path(profile_dir).resolve().as_uri()}",
                "--headless",
                "--convert-to",
                "pdf:writer_pdf_Export",
                "--outdir",
                str(docx_path.parent),
                str(docx_path),
            ]
            try:
                completed = subprocess.run(
                    command,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=max(30, int(timeout_seconds)),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise PdfConversionError(
                    f"PDF conversion timed out after {timeout_seconds}s"
                ) from exc

        log: dict[str, Any] = {
            "schema_version": "1.0",
            "started_at": started_at,
            "finished_at": utc_now(),
            "converter": Path(executable).name,
            "return_code": completed.returncode,
            "stdout": completed.stdout[-8000:],
            "stderr": completed.stderr[-8000:],
            "docx_filename": docx_path.name,
            "docx_sha256": sha256_bytes(docx_path.read_bytes()),
            "pdf_filename": pdf_path.name,
        }
        if completed.returncode != 0 or not pdf_path.is_file() or pdf_path.stat().st_size < 100:
            log["status"] = "FAIL"
            write_json(docx_path.with_suffix(".pdf-conversion.json"), log)
            raise PdfConversionError(
                "LibreOffice PDF conversion failed: "
                + (completed.stderr.strip() or completed.stdout.strip() or "no output")[-1200:]
            )

        try:
            reader = PdfReader(str(pdf_path))
            page_count = len(reader.pages)
            if page_count < 1:
                raise ValueError("PDF has no pages")
        except Exception as exc:
            log["status"] = "FAIL"
            log["validation_error"] = str(exc)
            write_json(docx_path.with_suffix(".pdf-conversion.json"), log)
            raise PdfConversionError(f"Converted PDF is unreadable: {exc}") from exc

        log.update(
            {
                "status": "PASS",
                "pdf_sha256": sha256_bytes(pdf_path.read_bytes()),
                "pdf_size_bytes": pdf_path.stat().st_size,
                "page_count": page_count,
            }
        )
        write_json(docx_path.with_suffix(".pdf-conversion.json"), log)
        return pdf_path
