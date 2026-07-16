# G3 正式能力验收

## 1. 验收边界

G3 是完整申请书的正式能力验收，不是模拟编排回归。只有同一次运行同时满足以下条件，`G3_ACCEPTANCE.json` 才能为 `PASS`：

- `CAPABILITY_ACCEPTANCE_MODE=true` 且 `MODEL_RUNTIME_MODE=LIVE`；
- 所有语义 Prompt 均由真实 OpenAI-compatible 模型端点返回；
- 原始响应只进行 JSON 解析、Schema 校验、持久化和哈希记录，不由脚本重写正文；
- 不使用 Replay、Mock、Simulated、自动响应器、固定模型答案或样例章节回退；
- Public Research Agent 产生结构化查询计划，Research Skill 实时检索并归档原始结果；
- 使用完整公开材料执行五条主工作流、十四章五组并发编制、分章审查与修复、每三章审查、全文 Integration Critic、最终复核和 DOCX/PDF 导出后验收；
- 所有 P0/P1 通过责任修复和独立复审关闭；
- 事实、公开 Claim、引用、图表、结论和候选来源可追溯；
- SQLite、请求响应、Prompt Trace、Research Archive、导出物和恢复包可复核。

## 2. 正式材料

G3 复用仓库完整申请书验收材料构造器，生成公开、非敏感且事实状态明确的科研项目材料。材料包括指南、事实清单、研究缺口、已有能力、目标指标、参考文献和十四章申请书结构，并明确：

- 已确认项目事实；
- 当前已有原型能力；
- 尚未解决的研究缺口；
- 未来目标和量化验收指标；
- 禁止外推的成果、论文、专利和示范应用。

主文固定十四章，并要求至少三幅可编辑 Mermaid 图和三张结构化表格。材料不包含真实个人、机构、地址、联系方式或业务数据。

## 3. 真实模型硬门

运行前必须配置：

```text
CAPABILITY_ACCEPTANCE_MODE=true
MODEL_RUNTIME_MODE=LIVE
OFFLINE_LLM_BASE_URL
OFFLINE_GENERAL_MODEL
OFFLINE_CRITIC_MODEL
ONLINE_LLM_BASE_URL
ONLINE_PUBLIC_MODEL
PUBLIC_SEARCH_PROVIDER=crossref|searxng
G3_OPERATOR_ATTESTATION=USER_REQUESTED|ATTESTED
G3_OPERATOR_ID=<operator>
G3_MODEL_PROVENANCE_ATTESTATION=REAL_MODEL_ENDPOINT
```

缺失、占位或非 LIVE 配置会生成 `BLOCKED_CONFIGURATION`，退出码为 2。系统不得自动切换到模拟模式，也不得把配置阻断写成 PASS。

`OFFLINE_LLM_BASE_URL` 对应冻结配置中的 `OFFLINE_LOCAL` 端点。正式运行者必须保证该端点确实属于获准的本地或受控离线环境，不能仅把公开云模型 URL 填入该字段伪造离线证据。

## 4. 真实公开研究

G3 增加独立的 Crossref 实时检索适配器，不修改共享 Research 基线：

- 每条模型生成查询实时调用 Crossref REST `/works`；
- 保存原始 item JSON、题名、作者、出版时间、DOI、摘要、查询和访问时间；
- 生成 `LIVE_CROSSREF` 归档模式；
- 继续使用现有计划校验、去重、来源排序、哈希复核和 Claim 绑定；
- 少于 8 个可核验来源时不通过 G3。

SearXNG 仍可作为正式提供方。`recorded`、测试 connector 文件、Replay 和静态来源集不能作为 G3 证据。

## 5. 每三章审查

完整十四章在候选确认节点被分为 `3+3+3+3+2` 五个连续窗口。每个窗口使用新的 `P-INTEGRATION-CRITIC` Run：

- 候选和章节映射来自同一冻结 Section Contract；
- 只读取该窗口的最终 Expression Critic 候选；
- 发现问题时调用现有责任路由，将问题返回最早能够修复的 Agent；
- 重写后重新生成候选并重新执行五个窗口；
- 最终五个窗口必须全部 PASS，Run ID 必须互不相同；
- 随后才确认候选并执行全文 Integration Critic。

## 6. 正式执行

```bash
python scripts/run_g3_formal_acceptance.py \
  --output-dir recovery_evidence/g3/<source-commit>
```

成功后复核：

```bash
python scripts/audit_prompt_traces.py \
  --database recovery_evidence/g3/<source-commit>/workflow_checkpoint.sqlite \
  --output-dir recovery_evidence/g3/<source-commit>/prompt_trace_audit

python scripts/validate_g3_evidence.py \
  recovery_evidence/g3/<source-commit>/G3_ACCEPTANCE.json \
  --evidence-root recovery_evidence/g3/<source-commit>
```

## 7. CI 行为

`.github/workflows/g3-formal-capability.yml` 分为两部分：

- PR 上只执行合同、Schema、单元和受影响回归，不消耗真实模型；
- 功能分支 push 或人工触发时执行 LIVE G3，读取仓库 Secrets，真实运行模型、检索、十四章编制、文档转换和恢复包复核。

工作流权限为 `contents: read`，不会提交代码、移动分支或修改仓库内容。

## 8. 通过报告

正式 `PASS` 报告至少包含：

- 真实模型和端点标识；
- 所有 Prompt Run、原始响应、解析对象和哈希；
- 真实 Research Archive、来源数量和 Claim 绑定；
- 五条主工作流 ID；
- 十四章和五个并发组；
- 五次每三章审查；
- 全文 Integration Critic 复核记录；
- 自动修复次数和 Finding 生命周期；
- DOCX、PDF、逐页截图和候选一致性；
- 无开放 P0/P1；
- SQLite 检查点、Trace 审计和恢复包。

工程代码和合同测试通过不等于 G3 PASS。只有 LIVE Artifact 中的 `G3_ACCEPTANCE.json` 与 `G3_REVERIFY.json` 均为 PASS，才可在开发计划中登记 G3 完成。
