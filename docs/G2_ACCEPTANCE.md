# G2 小规模集成验收

## 1. 验收范围

G2 在同一提交上并行运行三组小规模集成：

1. **S1 单章节完整链**：Blueprint → Blueprint Critic → 定向 Repair → 独立复审 → Content → Content Critic → 定向 Repair → 独立复审 → Polish → Expression Critic → Export；
2. **S2 三章节跨章链**：背景与意义、研究内容、技术路线三个唯一角色，执行跨章 Integration Critic、责任章节定向返修和后续独立复审；
3. **S3 Research + Mermaid + DOCX/PDF 链**：公开来源归档、Claim 绑定、Mermaid 源码/SVG/PNG、DOCX/PDF、结构检查、页面截图、视觉检查和重启哈希复核。

## 2. 不可绕过条件

- 人工修改正文不属于修复证据；
- Gate 审批、直接数据库改状态或手工确认不能关闭 P0/P1 Finding；
- 问题必须路由给责任 Agent，只允许重写受影响章节或对象；
- Repair 后必须由不同的 Critic 运行重新审查；
- Expression Critic 未通过时不得导出；
- S3 中来源、Claim、图形和交付物任一缺失、越界或哈希漂移都直接失败。

## 3. 自动化 Gate

`.github/workflows/g2.yml` 在同一 SHA 上并行执行 S1、S2、S3 和完整组件回归。最终 Job 下载三份场景证据，执行 `scripts/validate_g2.py`，仅在三组均满足严格不变量且完整回归成功时生成：

- `G2_ACCEPTANCE.json`；
- `G2_ACCEPTANCE.md`；
- Prompt Trace 审计；
- 包含源码、环境、SQLite、请求/响应、Trace、测试日志和验收报告的 `g2-recovery-bundle.zip`；
- 恢复包逐文件哈希及 SQLite 完整性复核报告。

各并行 Job 在写入日志或渲染工件前先创建独立的 SHA 证据目录，防止日志管道先于验收脚本初始化目录而产生伪失败。

## 4. 验收命令

```bash
python -m pytest -q \
  tests/test_single_section_chain.py \
  tests/test_g2_three_section_chain.py \
  tests/test_s3_research_mermaid_export.py \
  tests/test_g2_gate.py
```

完整 G2 必须在具备 Chromium、LibreOffice、Poppler 和 CJK 字体的环境运行 `.github/workflows/g2.yml`。

## 5. 能力边界

G2 证明确定性编排、责任返修、独立复审、持久化恢复、真实 Mermaid 渲染和真实 DOCX/PDF 转换能够在同一集成版本中闭合。S1/S2 使用的 SIMULATED 响应和 S3 的固定连接器夹具仅用于工程与审计验收，不作为真实模型语义质量或真实在线检索能力证明；这些能力属于 G3。
