from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from .util import new_id, sha256_text


class PublicResearchError(RuntimeError):
    pass


class PublicResearchService:
    def __init__(self, settings):
        self.settings = settings

    def simulated_search(self, plan: dict[str, Any]) -> dict[str, Any]:
        queries = []
        for q in plan.get("queries", []):
            if isinstance(q, dict):
                q = q.get("query") or q.get("query_text") or q.get("text") or ""
            if isinstance(q, str) and q.strip():
                queries.append(q.strip())
        if not queries:
            queries = ["logistics agent system", "multi-agent workflow orchestration"]
        catalog = [
            ("ReAct: Synergizing Reasoning and Acting in Language Models", "https://openreview.net/forum?id=WE_vluYUL-X", "将推理轨迹与动作执行结合，适合复杂任务规划闭环。"),
            ("Toolformer: Language Models Can Teach Themselves to Use Tools", "https://openreview.net/forum?id=Yacmpz84TH", "说明语言模型可自发学习调用工具。"),
            ("AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation", "https://www.microsoft.com/en-us/research/publication/autogen-enabling-next-gen-llm-applications-via-multi-agent-conversation-framework/", "提出基于会话的多智能体协作框架。"),
            ("Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks", "https://papers.nips.cc/paper/2020/hash/6b493230205f780e1bc26945df7481e5-Abstract.html", "RAG可增强知识密集型任务表现。"),
            ("Digital Twins for Smart Logistics and Supply Chain Management", "https://ieeexplore.ieee.org/", "数字孪生适合实时监控与模拟验证。"),
            ("A Review of Dynamic Vehicle Routing Problems", "https://www.sciencedirect.com/", "动态路径规划强调事件驱动的实时重规划。"),
            ("Human-AI Collaboration in Decision Making", "https://dl.acm.org/", "人机协同有助于构建可控决策支持流程。"),
            ("Observability for LLM Applications", "https://arize.com/", "强调保留Prompt、Trace和评估日志的重要性。"),
        ]
        sources: list[dict[str, Any]] = []
        passages: list[dict[str, Any]] = []
        for idx, (title, url, snippet) in enumerate(catalog, 1):
            domain = urlparse(url).netloc
            source = {
                "source_id": f"public-src-{idx:03d}",
                "source_type": "PUBLIC_SOURCE",
                "document_version_id": None,
                "section_id": None,
                "span_start": None,
                "span_end": None,
                "quoted_text": f"{title} | {url}",
                "source_hash": sha256_text(title + url + snippet),
                "authority_rank": self._authority_rank(domain),
                "security_level": "PUBLIC",
            }
            sources.append(source)
            passages.append({
                "passage_id": new_id("passage"),
                "source_ref": source,
                "text": snippet,
                "relevance": queries[min((idx - 1) % len(queries), len(queries) - 1)],
            })
        return {"sources": sources, "passages": passages, "queries": queries, "mode": "SIMULATED"}

    async def search(self, plan: dict[str, Any]) -> dict[str, Any]:
        provider = self.settings.public_search_provider
        if provider == "disabled":
            raise PublicResearchError("PUBLIC_SEARCH_PROVIDER is disabled")
        if provider != "searxng":
            raise PublicResearchError(f"Unsupported public search provider: {provider}")
        if not self.settings.public_search_base_url:
            raise PublicResearchError("PUBLIC_SEARCH_BASE_URL is empty")

        queries = plan.get("queries", [])
        normalized: list[str] = []
        for q in queries:
            if isinstance(q, str):
                normalized.append(q)
            elif isinstance(q, dict):
                normalized.append(str(q.get("query") or q.get("query_text") or q.get("text") or ""))
        normalized = [q.strip() for q in normalized if q.strip()][:8]
        sources: list[dict[str, Any]] = []
        passages: list[dict[str, Any]] = []
        seen: set[str] = set()
        timeout = httpx.Timeout(30.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for query in normalized:
                response = await client.get(f"{self.settings.public_search_base_url}/search", params={"q": query, "format": "json", "language": "zh-CN", "safesearch": 1})
                response.raise_for_status()
                payload = response.json()
                for item in payload.get("results", [])[:5]:
                    url = str(item.get("url") or "")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    title = str(item.get("title") or "")
                    snippet = str(item.get("content") or item.get("snippet") or "")
                    domain = urlparse(url).netloc
                    source_hash = sha256_text(url + "\n" + title + "\n" + snippet)
                    source = {
                        "source_id": new_id("public-src"),
                        "source_type": "PUBLIC_SOURCE",
                        "document_version_id": None,
                        "section_id": None,
                        "span_start": None,
                        "span_end": None,
                        "quoted_text": f"{title} | {url}",
                        "source_hash": source_hash,
                        "authority_rank": self._authority_rank(domain),
                        "security_level": "PUBLIC",
                    }
                    sources.append(source)
                    passages.append({"passage_id": new_id("passage"), "source_ref": source, "text": snippet or title, "relevance": f"检索词：{query}"})
        return {"sources": sources, "passages": passages, "queries": normalized}

    @staticmethod
    def _authority_rank(domain: str) -> int:
        lowered = domain.lower()
        if lowered.endswith(".gov.cn") or lowered.endswith(".gov"):
            return 90
        if lowered.endswith(".edu.cn") or lowered.endswith(".edu") or "ac.cn" in lowered or "openreview.net" in lowered:
            return 80
        return 50
