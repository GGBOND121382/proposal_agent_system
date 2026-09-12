# WF-3 Research Quality Knowledge Graph

Status: implemented for the current Batch-A quality gate. This document is descriptive; automatic generation of prompt schemas from the graph remains deferred to `docs/TODO_WF3_KNOWLEDGE_GRAPH_DRIVEN_PROMPT_CONTRACT.md`.

## 1. Purpose

WF-3 must distinguish **search discovery**, **semantic relevance**, **source quality**, **evidence coverage**, and **retrieval health**. A source returned by a query is not automatically evidence for that query, and a source with a DOI is not automatically peer reviewed.

## 2. Objects and ownership

```text
ResearchQuestion              MODEL semantic plan
    |
    +--> QueryBinding         MODEL semantic plan + runtime validation
             |
             +--> ProviderQuery              RUNTIME execution
                     |
                     +--> ProviderExecutionReceipt   RUNTIME trace
                     |        |
                     |        +--> RetrievalHealth  RUNTIME quality object
                     |
                     +--> ResearchCandidate         external observation
                              |
QueryRelevanceProfile --------+--> CandidateSemanticRelevance  RUNTIME deterministic gate
                                      |
ExactTimeScope ----------------------+--> CandidateSelection     RUNTIME deterministic gate
                                             |
                                             +--> ArchivedSource
                                                    |
PublicationMetadata --------------------------------> PublicationStatus/SourceAuthority
                                                    |
ResearchQuestion + CandidateSemanticRelevance ------> EvidenceQuestionBinding
                                                    |
                                                    +--> CoverageMatrix
RetrievalHealth ------------------------------------> CoverageMatrix
                                                    |
                                                    +--> ResearchGap
                                                           |
                                                           +--> ResearchSufficiency
                                                                  |
                                                                  +--> Synthesis
                                                                  +--> ResearchCritic
                                                                  +--> WF3ResearchResult
```

Current Batch-A does not yet create full-text `EvidenceCard` objects; that remains Batch-B work.

## 3. Edge types

- `SEMANTIC_CAUSE`: semantic information required to define another semantic object.
- `CONSTRAINT`: narrows valid values (for example exact time scope).
- `EVIDENCE`: supports a semantic judgment.
- `DETERMINISTIC`: computed by runtime code.
- `TRACE`: ids, provider receipts, hashes and manifests.
- `CONTROL`: PASS/INSUFFICIENT, blocking and routing.

Only semantic cause/constraint/evidence are candidates for model input. Deterministic/trace/control relations are runtime owned.

## 4. Invariants enforced in Batch-A

### I1. Discovery binding is not evidence coverage

```text
Candidate discovered by query Q
    !=
Candidate semantically supports query Q
```

A source contributes to `CoverageMatrix.by_query[Q]` only if its `CandidateSemanticRelevance` for Q is `DIRECT` or `SUPPORTING` and `qualifies_for_coverage=true`.

### I2. Relevance is query-specific

A source may qualify for one bound query and fail another. Deduplication must preserve `semantic_relevance_by_query`, not collapse relevance to one global flag.

### I3. Domain anchors need broader context

A generic repeated anchor such as `decision` cannot by itself establish relevance. Anchored queries require additional query-concept overlap. Document-type boilerplate such as `Decision letter for ...` is removed before relevance scoring.

### I4. Time scope is exact

`YYYY-MM-DD/YYYY-MM-DD` is preserved at day precision through planning, provider filtering where supported, and deterministic post-retrieval screening. Year-only provider APIs may retrieve a superset, but out-of-window records must be removed before coverage.

### I5. DOI does not imply peer review

Publication status/source type is a separate object. Preprints, posted content, working papers, book chapters, editorials/decision letters, reports, theses and unverified scholarly records are not promoted to peer-reviewed papers merely because a DOI exists.

### I6. Query depth and query authority are separate

For strict `proposal_related_work` coverage:

```text
source_count(Q) >= configured minimum
AND
authoritative_source_count(Q) >= 1
```

Global source volume or global authority cannot hide a weak individual research query.

### I7. Provider health is independent of source volume

Many results from a surviving provider cannot hide failed configured channels. `RetrievalHealth` records requested providers, per-query execution success, rate limiting/failures and hybrid web-channel availability.

For `hybrid`, an unavailable SearXNG web channel makes retrieval health `DEGRADED` even if academic discovery returned many sources.

### I8. Research sufficiency is a runtime decision

`CoverageMatrix` and `RetrievalHealth` are runtime-owned. An LLM must not override them by declaring the literature review sufficient.

`ResearchSufficiency` has three meanings:

- `SUFFICIENT`: current quality requirements are met.
- `DEGRADED`: usable evidence exists but deterministic `ResearchGap` objects remain; the protocol may continue only while preserving those limitations.
- `BLOCKING_FAILURE`: retrieval/execution integrity failed or no qualifying public evidence exists; the protocol must not synthesize.

### I9. A known ResearchGap is not a request to invent evidence

`ResearchGap` is derived deterministically from coverage. Synthesis must preserve it as a limitation. A Research Critic observation that merely restates an already-known uncovered research question is non-blocking when the synthesis has preserved the gap; unsupported or over-generalized claims remain blocking.

### I10. Workflow completion does not erase research sufficiency

A completed WF-3 persists `WF3_RESEARCH_RESULT` containing `ResearchSufficiency`, `ResearchGap`, retrieval health, claim dispositions and the public source catalog. `COMPLETED` means the protocol completed; it does not imply that every research question was sufficiently covered.

## 5. Current deterministic relevance boundary

The current implementation is intentionally conservative and does not claim full semantic entailment. Its role is to prevent obvious keyword collisions from being counted as coverage without adding another model call.

It may leave isolated tangential/noisy results in the candidate set. This is acceptable only if they cannot make an insufficient query pass the coverage gate. Full evidence-level semantic assessment belongs to Batch-B (`EvidenceCard` / evidence-question binding).

## 6. Prompt-design consequence

For future Prompt audits, start from the target object and include only its nearest `SEMANTIC_CAUSE`, `CONSTRAINT`, and `EVIDENCE` parents. Do not pass provider receipts, source hashes, coverage status, runtime routing, TTLs, ids, or other control/trace objects merely because they are ancestors in the workflow.
