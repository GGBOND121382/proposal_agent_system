# Stage 5 Provisional Section Planning

This stage creates a reversible 14-section content plan, page budget, generation contracts, visual plan, and four drafting batches. It does not generate proposal prose.

The final submission contract remains blocked until official guide/template and evidence materials are available.

## CLI

```bash
python stage5_tools/stage5_section_planning.py init \
  --run-dir <run_dir> \
  --design-input <stage1.json> \
  --project-definition <stage3.json> \
  --argument-architecture <stage4.json> \
  --evidence-completion <stage4a.json>
```

Then use `ingest-generator`, optional `ingest-repair`, `ingest-critic`, and `finalize`.
