# S3：Research + Mermaid + Export 链

> 对应 `docs/CONCURRENT_DEVELOPMENT_PLAN.md` 的 G2 第 3 组小规模集成。  
> 本链只负责把已验收的 C 轨道公开研究能力与 D 轨道 Mermaid、DOCX/PDF 和交付后验证能力连接起来，不改写模型正文，也不修改安全分类或审批规则。

## 1. 实现边界

入口为 `app/research_mermaid_export.py` 的 `ResearchMermaidExportPipeline`，按三个可恢复阶段运行：

1. `research`：执行结构化公开研究计划，保存来源原始快照、提取文本、元数据和连接器响应，并复核 SHA-256；
2. `prepare`：校验 `PUBLIC_CLAIM` 与来源哈希绑定，要求每个 Mermaid 图同时声明 `claim_ids` 和 `source_ids`，渲染 `.mmd`、SVG、PNG 和元数据，写入可移植检查点；
3. `finalize`：重载并复核检查点，要求每个图指令在后续通过 `P-EXPRESSION-CRITIC` 的正文中恰好出现一次，再调用生产 `DocxExporter` 生成 DOCX、PDF、页面截图、结构/视觉 Findings 和交付压缩包。

所有跨阶段文件引用使用 `artifact://`，绝对路径不进入检查点或正文。任何来源、图形、检查点、DOCX、PDF、验证报告或压缩包缺失、哈希漂移、图指令遗漏/重复，链路均失败闭锁。

## 2. 关键证据

一次成功运行至少产生：

```text
research_archive/<project>/<session>/
  manifest.json
  raw/
  text/
  metadata/
  connector/connector_response.json

diagram_artifacts/<project>/<section>/
  *.mmd
  *.svg
  *.png
  *.json

exports/
  *.docx
  *.pdf
  *.pdf-conversion.json
  *.structure-findings.json
  *.visual-findings.json
  *.delivery-validation.json
  *.zip

recovery_evidence/s3/<run>/
  research-manifest.portable.json
  S3_PREPARED_CHECKPOINT.json
  S3_PREPARED_VERIFICATION.json
  S3_FINAL_ACCEPTANCE.json
  S3_FINAL_VERIFICATION.json
  S3_RESULT.json
  s3-research-mermaid-export-evidence.zip
```

`S3_RESULT.json` 同时记录来源数量、Claim 绑定、图形工件、Expression Polish/Critic 运行 ID、DOCX/PDF/交付包哈希和最终验证状态。

## 3. 验收命令

```bash
python -m pytest -q \
  tests/test_research_skill_track_c.py \
  tests/test_d_track.py \
  tests/test_s3_research_mermaid_export.py

python scripts/run_s3_research_mermaid_export.py \
  --output-dir recovery_evidence/s3/local
```

真实交付验收要求环境中存在 Chromium、LibreOffice Writer 和 Poppler `pdftoppm`。CI 工作流为 `.github/workflows/s3-research-mermaid-export.yml`。

## 4. 判定口径

确定性测试覆盖：

- 研究计划、来源和 Claim 绑定失败时阻断；
- 图引用未绑定到 Claim 来源时阻断；
- 图指令在已批准正文中缺失或重复时阻断；
- 研究、连接器或 Mermaid 工件被篡改后，重启验证失败；
- DOCX/PDF、结构检查、页面截图和视觉检查全部通过后才生成 S3 PASS 与证据包。

`scripts/run_s3_research_mermaid_export.py` 使用批准的 recorded connector fixture，只证明工程集成、真实 Mermaid 渲染、真实 DOCX/PDF 转换、可恢复性和哈希门；它明确不构成 LIVE 检索或真实模型语义能力证明。正式能力验收仍必须在 LIVE 模式下使用真实检索端点、真实模型原始响应和固定输入材料。
