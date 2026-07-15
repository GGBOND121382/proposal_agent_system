# G2 S3：Research + Mermaid + Export 链验收

> 对应计划：`docs/CONCURRENT_DEVELOPMENT_PLAN.md` 的 S3 / G2 小规模集成。  
> 范围：公开资料归档与 Claim 绑定 → Mermaid 可重复渲染 → 经 Expression Critic 放行的 DOCX → PDF → 结构与页面视觉验证 → 可恢复证据包。

## 1. 安全边界

本工作包不修改以下冻结区域：

- 安全分类与密级映射；
- 外发 Gate、审批状态机和 WF-1 安全流程；
- 生产环境的审批决定或数据库记录；
- 正文事实、论证结构和模型原始响应。

S3 验收使用独立临时数据库，仅构造两个已经批准的导出检查点，用于验证“批准之后”的 Research、Mermaid 和 Export 集成。该夹具不进入生产数据库，也不构成安全审批绕过。

## 2. 实现文件

- `app/s3_evidence.py`：跨组件证据绑定、哈希复核、可移植 `artifact://` 清单、交付包完整性检查和重启复核；
- `scripts/run_s3_research_mermaid_export.py`：执行 G2 S3 链；
- `scripts/verify_s3_evidence.py`：在不重新生成内容的情况下复核持久化证据；
- `tests/test_s3_research_mermaid_export.py`：正向、篡改、Claim 未通过、图形未绑定来源、交付包缺失和重启篡改测试；
- `.github/workflows/g2-s3.yml`：真实 Chromium、LibreOffice 和 Poppler 集成验收。

S3 同时集成 G1 已验收的 C、D 轨道实现：

- C：`VerifiablePublicResearchArchiveSkill`、来源快照、元数据、SHA-256、去重、覆盖检查和 `PUBLIC_CLAIM` 绑定；
- D：Mermaid `.mmd`/SVG/PNG、图形协议、DOCX、PDF、交付结构验证和页面截图验证。

## 3. 链路与硬门

```text
结构化 Research Plan
→ Connector/检索结果归档
→ Research Archive SHA-256 复核
→ PUBLIC_CLAIM 与归档来源绑定
→ Mermaid 图形绑定 claim_id 与 evidence_ids
→ 第二次渲染命中经哈希复核的缓存
→ P-EXPRESSION-POLISH 候选
→ 后置 P-EXPRESSION-CRITIC PASS
→ DOCX
→ LibreOffice PDF
→ DOCX/PDF 结构检查
→ Poppler 页面截图与视觉检查
→ S3_CHAIN_MANIFEST.json
→ s3-research-mermaid-export.zip
→ 重启复核
```

任一条件不满足即阻断：

1. Research Archive 重新计算哈希后不是 PASS；
2. Claim 绑定报告不是 PASS，或没有绑定来源；
3. Mermaid 的 `evidence_ids` 不属于已验证 Claim 的来源集合；
4. `.mmd`、SVG、PNG 的声明哈希与实际工件不一致；
5. 图形引用不是 `artifact://`，或路径越出 `APP_DATA_DIR`；
6. DOCX/PDF 验证存在阻断 Finding；
7. 导出 ZIP 缺少 DOCX、PDF、转换日志、结构/视觉报告或页面截图；
8. 重启后任一清单工件缺失或哈希漂移。

## 4. 证据输出

每次运行至少产生：

```text
workflow_checkpoint.sqlite
research_archive/
claim_bindings/public-claim-bindings.json
diagram_artifacts/
exports/*.docx
exports/*.pdf
exports/*.pdf-conversion.json
exports/*.structure-findings.json
exports/*.visual-findings.json
exports/*-pages/page-*.png
exports/*.zip
acceptance/S3_CHAIN_MANIFEST.json
acceptance/S3_ACCEPTANCE.json
acceptance/S3_RESTART_VERIFY.json
acceptance/s3-research-mermaid-export.zip
```

`S3_CHAIN_MANIFEST.json` 将 Research、Claim、Mermaid 和 Export 工件置于同一条可追溯链中；正文只包含图和引用，不包含绝对文件系统路径。

## 5. 两种运行方式

### 5.1 G2 集成夹具

```bash
python scripts/run_s3_research_mermaid_export.py \
  --output-dir recovery_evidence/s3/local \
  --fixture
```

该模式验证真实代码、真实渲染器和真实文档转换，但公开资料内容是确定性集成夹具。报告明确标记：

```text
G2_ORCHESTRATION_FIXTURE_NOT_LIVE_SEMANTIC_PROOF
```

因此它只能证明跨组件编排、工件完整性和恢复能力，不能替代 G3 的真实公开调研与真实模型语义验收。

### 5.2 已批准 Connector 结果

```bash
python scripts/run_s3_research_mermaid_export.py \
  --output-dir recovery_evidence/s3/connector-run \
  --connector-file /path/to/approved_connector_response.json
```

Connector 文件必须覆盖结构化 Research Plan 的全部查询，并保存查询、访问时间、结果元数据和原始文本/摘要。来源不足、冲突或哈希异常会保留不确定性并阻断，不会补造题名或 DOI。

## 6. 独立重启复核

```bash
python scripts/verify_s3_evidence.py \
  recovery_evidence/s3/local/acceptance/S3_ACCEPTANCE.json \
  --data-dir recovery_evidence/s3/local
```

复核过程不重新检索、不重新渲染、不重新导出，只根据清单重新计算现有工件哈希。任一文件被修改、删除或移出数据目录都会返回 FAIL。

## 7. 完成定义

S3 只有在以下结果同时成立时为 PASS：

- C、D、S3 专项测试全部通过；
- Research Archive 和 Claim 绑定通过；
- Mermaid 重复渲染源码/SVG/PNG 哈希一致且缓存命中；
- DOCX/PDF 结构与视觉验证无阻断 Finding；
- S3 证据包完整；
- 重启复核 PASS；
- 完整仓库回归通过；
- 没有修改安全冻结链路，也没有把 G2 夹具表述为 LIVE 语义能力证明。
