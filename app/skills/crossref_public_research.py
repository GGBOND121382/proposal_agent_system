from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from ..util import new_id, utc_now, write_json
from .base import SkillContext, SkillResult
from .public_research import PublicResearchArchiveError
from .research_audit import verify_research_archive
from .research_plan import deduplicate_candidates, normalize_and_validate_plan
from .verifiable_public_research import VerifiablePublicResearchArchiveSkill


class CrossrefPublicResearchArchiveSkill(VerifiablePublicResearchArchiveSkill):
    """Verifiable public-research skill with a live Crossref metadata provider."""

    version = "2.1.0"

    def run(self, payload: dict[str, Any], context: SkillContext) -> SkillResult:
        provider = str(payload.get("provider") or self.settings.public_search_provider).lower()
        if provider != "crossref":
            return super().run(payload, context)
        strict = bool(payload.get("require_structured_plan", False))
        try:
            normalized_plan, validation = normalize_and_validate_plan(
                payload.get("plan") or {}, strict=strict
            )
        except ValueError as exc:
            raise PublicResearchArchiveError(str(exc)) from exc
        if validation["status"] == "BLOCK":
            codes = [str(item.get("code")) for item in validation["findings"]]
            raise PublicResearchArchiveError(
                "Research plan validation failed: " + ", ".join(codes)
            )
        max_results = max(
            1,
            min(
                int(payload.get("max_results") or self.settings.public_search_max_results),
                100,
            ),
        )
        candidates, _ = deduplicate_candidates(
            self._search_crossref(normalized_plan["queries"], max_results)
        )
        if not candidates:
            raise PublicResearchArchiveError("Crossref returned no usable records")
        input_root = Path(context.data_dir) / "research_live_inputs" / str(context.project_id)
        input_root.mkdir(parents=True, exist_ok=True)
        input_path = input_root / f"crossref-{new_id('input')}.json"
        write_json(input_path, {"sources": candidates})
        effective = {
            **payload,
            "provider": "recorded",
            "record_file": str(input_path),
            "plan": {
                **(payload.get("plan") or {}),
                "queries": normalized_plan["queries"],
            },
        }
        result = super().run(effective, context)
        manifest_path = Path(result.output["archive_manifest"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "retrieval_mode": "LIVE_CROSSREF",
                "provider": "crossref",
                "live_provider": {
                    "name": "Crossref REST API",
                    "queried_at": utc_now(),
                    "query_count": len(normalized_plan["queries"]),
                    "input_record_count": len(candidates),
                },
            }
        )
        write_json(manifest_path, manifest)
        verification = verify_research_archive(manifest_path)
        if verification.get("status") != "PASS":
            raise PublicResearchArchiveError(
                "Crossref research archive failed hash verification"
            )
        result.output.update(
            {
                "mode": "LIVE_CROSSREF",
                "provider": "crossref",
                "archive_verification": verification,
            }
        )
        result.artifacts.append(str(input_path))
        return result

    def _search_crossref(
        self, queries: list[str], max_results: int
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        rows = max(1, min(8, max_results))
        headers = {"User-Agent": "proposal-agent-system/0.6 (formal-research)"}
        with httpx.Client(
            timeout=self.settings.research_fetch_timeout_seconds,
            follow_redirects=True,
            headers=headers,
        ) as client:
            for query in queries:
                response = client.get(
                    "https://api.crossref.org/works",
                    params={
                        "query.bibliographic": query,
                        "rows": rows,
                        "select": (
                            "DOI,title,abstract,author,publisher,published,URL,"
                            "type,subject,container-title"
                        ),
                    },
                )
                response.raise_for_status()
                items = ((response.json().get("message") or {}).get("items") or [])
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    doi = str(item.get("DOI") or "").strip()
                    title_values = item.get("title") or []
                    title = str(
                        title_values[0] if title_values else doi or query
                    ).strip()
                    abstract = BeautifulSoup(
                        str(item.get("abstract") or ""), "html.parser"
                    ).get_text(" ", strip=True)
                    subjects = "; ".join(
                        str(value) for value in item.get("subject") or [] if value
                    )
                    containers = "; ".join(
                        str(value)
                        for value in item.get("container-title") or []
                        if value
                    )
                    content = "\n".join(
                        value for value in [title, abstract, subjects, containers] if value
                    )
                    published = item.get("published") or {}
                    parts = published.get("date-parts") or []
                    date_values = (
                        parts[0]
                        if parts and isinstance(parts[0], list)
                        else []
                    )
                    published_at = (
                        "-".join(str(value) for value in date_values[:3])
                        if date_values
                        else None
                    )
                    authors = []
                    for author in item.get("author") or []:
                        if not isinstance(author, dict):
                            continue
                        name = " ".join(
                            value
                            for value in [
                                str(author.get("given") or "").strip(),
                                str(author.get("family") or "").strip(),
                            ]
                            if value
                        )
                        if name:
                            authors.append(name)
                    url = str(
                        item.get("URL")
                        or (f"https://doi.org/{doi}" if doi else "")
                    ).strip()
                    if not url:
                        continue
                    candidates.append(
                        {
                            "title": title,
                            "url": url,
                            "doi": doi or None,
                            "authors": authors,
                            "publisher": item.get("publisher"),
                            "published_at": published_at,
                            "source_type": "PEER_REVIEWED_PAPER",
                            "content_text": content or title,
                            "excerpt": content or title,
                            "matched_query": query,
                            "raw_result": item,
                            "verification": {
                                "status": "CROSSREF_RETURNED",
                                "provider": "Crossref REST API",
                            },
                        }
                    )
        return candidates
