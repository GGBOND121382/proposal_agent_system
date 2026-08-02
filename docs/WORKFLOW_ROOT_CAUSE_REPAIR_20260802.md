# Workflow root-cause repair approval (2026-08-02)

## Scope

This change set consolidates the approved workflow repairs from patch sets 0001-0003 and the follow-up root-cause corrections required to resume the existing proposal-authoring workflow.

The approved interface changes cover strict prompt/schema ownership, explicit workflow scope, stable finding-instance identity, exact targeted-repair partitions, context-graph identity normalization, registered reference semantics, and transaction-safe workflow recovery. The approved security-scope changes cover provider error propagation, model endpoint configuration, and transaction-safe gate persistence. Security roles, allowed actions, confidentiality levels, and offline/online routing invariants remain unchanged.

## Design constraints

- Prompts and schemas own business-field semantics; runtime code validates contracts and does not invent missing business values.
- Targeted repair replaces a complete contract object and proves the exact resolved/unresolved finding-instance partition.
- Cross-field status inference and guessed identifier aliases are prohibited.
- Workflow recovery preserves workflow and run identity across transactional retries and process restarts.
- Guard observations remain diagnostic metadata and never rewrite model output.

## Verification

Approval entries record the Git blob of every changed frozen path. G0 continues to reject undeclared paths, stale blobs, state-machine drift, security-invariant drift, and prompt-registry drift. The full automated suite, prompt-pack validation, static compilation, export test, and recovery of the existing workflow are the acceptance criteria for this change set.
