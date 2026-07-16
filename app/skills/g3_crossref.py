from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

from .base import SkillContext, SkillResult
from .verifiable_public_research import VerifiablePublicResearchArchiveSkill
from ..util import utc_now, write_json


class G3CrossrefResearchSkill(VerifiablePublicResearchArchiveSkill):
    """Live Crossref adapter for the existing verifiable research archive skill."""

    version = "3.0.0"

    def run(self, payload: dict[str, Any], context: SkillContext) -> SkillResult:
        provider = str(payload.get("provider") or "").lower()
        if provider != "crossref":
            return super().run(payload, context)
        plan = payload.get("plan") or {}
        queries = self._queries(plan)
        if not queries:
            raise ValueError("Crossref research requires at least one query")
        responses: list[dict[str, Any]] = []
        base_url = str(payload.get("base_url") or "https://api.crossref.org").rstrip("/")
        max_results = max(1, min(int(payload.get("max_results") or 40), 100))
        with httpx.Client(timeout=self.settings.research_fetch_timeout_seconds, follow_redirects=True) as client:
            for query in queries:
                response = client.get(
                    f"{base_url}/works",
                    params={
                        "query": query,
                        "rows": min(10, max_results),
                        "select": "DOI,title,abstract,author,published,container-title,publisher,URL,type",
                    },
                )
                response.raise_for_status()
                results = [self._candidate(item, query) for item in response.json().get("message", {}).get("items", [])]
                responses.append({"query": query, "retrieved_at": utc_now(), "results": results})
        connector_path = Path(context.data_dir) / "g3_crossref_connector.json"
        write_json(
            connector_path,
            {
                "schema_version": "1.0",
                "connector": "crossref-rest-live",
                "run_id": f"crossref-{utc_now()}",
                "created_at": utc_now(),
                "responses": responses,
            },
        )
        effective = dict(payload)
        effective.update({"provider": "connector", "connector_file": str(connector_path)})
        result = super().run(effective, context)
        result.output["mode"] = "LIVE_CROSSREF"
        manifest_path = Path(str(result.output["archive_manifest"]))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["provider"] = "crossref"
        manifest["retrieval_mode"] = "LIVE_CROSSREF"
        manifest["live_endpoint"] = base_url
        write_json(manifest_path, manifest)
        return result

    @staticmethod
    def _candidate(item: dict[str, Any], query: str) -> dict[str, Any]:
        title = " ".join(item.get("title") or []).strip()
        abstract = re.sub(r"<[^>]+>", " ", str(item.get("abstract") or ""))
        abstract = re.sub(r"\s+", " ", abstract).strip()
        authors = [
            " ".join(part for part in (author.get("given"), author.get("family")) if part)
            for author in item.get("author") or []
        ]
        date_parts = (item.get("published") or {}).get("date-parts") or []
        published_at = "-".join(str(value) for value in date_parts[0]) if date_parts else None
        doi = str(item.get("DOI") or "").strip()
        return {
            "source_id": f"crossref-{doi or abs(hash(title))}",
            "title": title or doi,
            "url": str(item.get("URL") or (f"https://doi.org/{doi}" if doi else "")),
            "excerpt": abstract or title,
            "content_text": "\n".join(filter(None, [title, abstract, "Authors: " + "; ".join(authors)])),
            "published_at": published_at,
            "authors": authors,
            "publisher": item.get("publisher"),
            "doi": doi or None,
            "matched_query": query,
            "retrieved_at": utc_now(),
            "raw_connector_result": item,
            "verification": {"status": "CROSSREF_RETURNED", "query": query},
        }
