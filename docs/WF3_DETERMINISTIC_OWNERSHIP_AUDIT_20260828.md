# WF-3 Deterministic Ownership Audit — 2026-08-28

## Purpose

This audit fixes a recurring WF-3 boundary error: values or decisions that already have a deterministic runtime answer must not be reconstructed, overridden, or adjudicated by an LLM.

The governing rule is:

- **AUTHORITATIVE_VALUE**: runtime owns the canonical value and fans it out to every consumer.
- **DETERMINISTIC_INVARIANT**: runtime checks it before any model call; a failed invariant cannot be repaired by a model response.
- **SEMANTIC_ASSESSMENT**: the model may report observations that require semantic understanding.
- **POLICY_RESOLUTION**: runtime combines semantic observations, deterministic evidence, and fixed policy to decide severity, blocking, route, and rejection scope.

## Authoritative objects

| Object | Owner | Canonical source | Consumers | Invariant |
| --- | --- | --- | --- | --- |
| ApprovedBoundary.allowed_topics | workflow runtime | resolved WF-3 input (`wf3_input_resolution.allowed_topics`) | Safe Producer, Safe Critic, downstream boundary checks | every consumer receives the same ordered values |
| Evidence requirements | workflow/runtime input | approved task / Safe Package | Research Plan, Synthesis | model must not rewrite authoritative requirements |
| Prohibited inferences | workflow/runtime input | approved task / Safe Package | Research Plan, Synthesis, Import review | model must not rewrite authoritative boundary |
| Time scope | workflow/runtime input | Research Plan canonical input | provider queries, screening, coverage | exact dates are preserved; year-only provider filters are post-filtered |
| Claim→source binding existence | runtime | canonical claims/source refs | Research/Import validation | model cannot override deterministic presence/absence |
| Source publication status | runtime | provider metadata + deterministic normalization | ranking, authority coverage | DOI alone never implies peer review |

### Authoritative fan-out invariant

An authoritative object must be resolved once and projected to all consumers. A consumer must not rebuild the same object independently from a different configuration field.

For approved topics:

`WorkflowApprovedTopics == Producer.allowed_topics == SafeCritic.allowed_topics`

A missing approved boundary is an internal contract error and must be rejected before provider invocation.

## Pre-model deterministic guards

| Node | Guard | Failure handling | Model called? |
| --- | --- | --- | --- |
| P-SAFE-ONLINE-PACKAGE | approved topics non-empty | `WF3_APPROVED_BOUNDARY_MISSING` / contract block | No |
| P-SAFE-ONLINE-PACKAGE-CRITIC | approved topics non-empty | `WF3_APPROVED_BOUNDARY_MISSING` / contract block | No |
| P-SAFE-ONLINE-PACKAGE-CRITIC | deterministic scan passed | `WF3_DETERMINISTIC_SCAN_FAILED` / content block | No |

The deterministic scan receipt may remain in the current semantic input for compatibility, but it is no longer decision-bearing: when the model is invoked, runtime has already proven `passed == true`.

## Runtime policy ownership

### Safe Package Critic

The model may report semantic risk types such as identifiable-project leakage, combination re-identification, scope excess, or missing prohibition. It does **not** own `P0`, `blocking`, workflow route, or final status. Runtime maps supported observations to fixed repair actions and severity.

A model-proposed `required_action=BLOCK` is an observation only; it cannot directly create a P0 block.

### Import Critic

The model may report semantic scope/sensitive-inference/prompt-injection observations. Runtime owns final rejection and blocking policy.

`UNSOURCED_CLAIM` is deterministic with respect to canonical claim/source binding existence. If a binding exists, a model-reported `UNSOURCED_CLAIM` is retained only as a nonblocking suspicion. If the binding is actually absent, runtime rejects the affected claim.

## Research execution ownership

`ResearchPlan.source_priorities` is now consumed by deterministic candidate ranking/reporting. It is a preference signal, not a security gate. Peer-reviewed/official priority matching is based on normalized publication/source status; DOI is only a traceability signal.

## Remaining P1 semantic boundary

WF-3 still has two query layers:

1. Safe Package seed queries / task framing.
2. Research Plan executable queries.

The executable queries must ultimately be checked against the approved semantic boundary before external dispatch. This is **not** a purely deterministic lexical invariant because queries may be paraphrased or cross-language. A future Plan Scope Guard should combine deterministic structural checks with a minimal semantic boundary assessment.

Do not solve this by reintroducing generic Critic responsibilities or by letting the model choose workflow status.

## Required regression classes

1. **Authoritative fan-out tests**: known workflow values must appear unchanged in every consumer projection.
2. **Pre-model guard tests**: failed deterministic invariants imply provider call count zero.
3. **Policy ownership tests**: semantic model fields cannot directly set P0/block/route/reject-all.
4. **Value-semantic tests**: validate actual values, not only JSON shape/schema validity.
5. **Dead-edge tests**: knowledge objects produced by a model (for example source priorities) must have a real runtime consumer or be removed.

## Deferred work

Knowledge-graph-driven automatic generation/static validation of Prompt input schemas remains deferred in `docs/TODO_WF3_KNOWLEDGE_GRAPH_DRIVEN_PROMPT_CONTRACT.md`. The current goal is to keep the existing WF-3 working and contract-stable before introducing that automation.
