# TODO — WF-3 Search Strategy Upgrade

Status: deferred until the current WF-3 protocol can complete reliably end to end. These items improve how a `DEGRADED` research result is repaired into a `SUFFICIENT` one; they are not prerequisites for protocol completion.

## Deferred search-strategy work

- Introduce `QueryIntent` as a first-class object between `ResearchQuestion` and provider queries.
- Allow one research question to produce multiple complementary queries rather than one keyword-dense query.
- Represent domain anchors, must-have concepts, optional concepts and excluded ambiguities explicitly.
- Generate provider-specific query forms for OpenAlex, Crossref and SearXNG.
- Make venue/source priorities drive provider/venue filters and ranking where APIs support them.
- Add additive-only `PlanDelta` for supplemental search; never replace or mutate the approved base plan.
- Build a gap-driven supplemental-search loop from deterministic `ResearchGap` objects.
- Rank supplemental queries and retain query-effectiveness feedback across repair rounds.
- Evaluate PaperQA2 as a supplemental-search/evidence-retrieval adapter rather than rebuilding agentic paper search from scratch.
- Use STORM-style multi-perspective query decomposition as a design reference, not as the workflow owner.
- Add citation expansion, full-text acquisition, EvidenceCard extraction and evidence reranking in the later evidence-depth phase.

## Security hardening completed in flow-closure

The final executable Research Plan queries now receive a dedicated semantic scope recheck against the workflow-owned `ApprovedBoundary` immediately before public search. The scope critic reviews the actual outbound queries, creates no human Gate, and routes semantic scope defects back to `P-PUBLIC-RESEARCH-PLAN`; Runtime owns status, severity, retry and routing. Search-strategy upgrades below must preserve this boundary.

## Non-goals for the current flow-closure patch

- Do not weaken semantic relevance or authority thresholds merely to obtain PASS.
- Do not fabricate coverage.
- Do not automatically expand the approved research scope.
- Do not add new LLM calls solely to make an insufficient search look sufficient.
