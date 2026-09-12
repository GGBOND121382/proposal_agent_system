# Stage 4A Evidence Completion

This stage completes evidence needed for reversible content planning without freezing the final submission contract.

## Boundary

- Maps public prior work to RQ-1 through RQ-3.
- Separates public evidence, user assertions, and internal traces.
- Freezes metric measurement protocols while keeping numeric thresholds provisional.
- Reclassifies missing official guide/template as final-compliance blockers.
- Does not generate proposal sections.

## CLI

```bash
python stage4a_tools/stage4a_evidence_completion.py init \
  --run-dir <run_dir> \
  --argument-architecture <stage4_argument_architecture.json> \
  --evidence-inputs stage4a_tools/examples/stage4a_evidence_inputs.json
```

Use `ingest-generator`, `ingest-critic`, and `finalize` for the remaining file-bridge steps. Every model response must declare the actual `model_id` and `endpoint_id`.
