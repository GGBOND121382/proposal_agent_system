# G2：三章节跨章链

## 1. 验收范围

该链路对应 `CONCURRENT_DEVELOPMENT_PLAN.md` 的 G2 第 2 组，只处理并联合审查三个正式章节：

1. `BACKGROUND_AND_SIGNIFICANCE`：背景/意义/立项依据；
2. `RESEARCH_CONTENT`：研究内容；
3. `TECHNICAL_ROUTE`：技术路线。

启动 `WF-4_PROPOSAL_AUTHORING` 时设置：

```json
{
  "three_section_cross_chapter": true,
  "integration_scope": "THREE_SECTION_CROSS_CHAPTER"
}
```

系统会按 Section Profile 解析章节，而不是按列表位置猜测。缺少任一角色、同一角色出现多个章节、跨章审查候选与冻结合同不一致时，工作流直接阻断。

## 2. 执行链

每个章节仍执行完整生产链：

`Blueprint → Blueprint Critic → Content → Content Critic → Expression Polish → Expression Critic`

三个章节全部产生通过独立 Critic 的候选后，系统保存 `three_section_contract`，构建仅含这三章的 `P-INTEGRATION-CRITIC` 输入并执行跨章审查。

跨章 Finding 按以下原则返修：

- 论证图缺陷返回 `ARGUMENT_ARCHITECTURE_AGENT`；
- 章节合同、命题归属或映射缺陷返回 `PLANNING_AGENT`；
- 术语、数字、重复或正文表达缺陷返回 `WRITING_AGENT`，并从审查结果、Finding 定位路径和证据引用中提取责任章节；
- Critic 已指定写作责任但未给出章节定位时，三章全部重写，禁止人工直接改正文或静默放行。

写作类问题只移除受影响章节的已完成记录，未受影响章节不会重跑。返修后自动再次执行 `P-INTEGRATION-CRITIC`。

## 3. 质量生命周期

P0/P1 Finding 以追加式 `QUALITY_FINDING` Artifact 持久化。一个问题只有同时具备以下证据才能关闭：

1. 后续责任 Agent 的新生产运行，形成 repair evidence；
2. 与修复运行不同的、时间更晚的 Integration Critic 运行，且对应 Finding Code 已消失。

人工 Gate、直接修改数据库状态或导出操作均不能关闭未复审的 P0/P1。导出器在存在开放阻断项时拒绝 DOCX/PDF 交付。

## 4. 重启恢复

跨章审查的运行记录、冻结合同、责任章节、返修轮次、审查历史和质量生命周期均写入 SQLite/Artifact。进程在“已路由、尚未重写”处中断后，新建 `WorkflowEngine` 可从持久化 `current_step` 和 state 继续；未受影响章节不会重复生成。

## 5. 自动验收

```bash
python scripts/run_g2_three_section_chain.py \
  --output-dir recovery_evidence/g2_three_sections/local
```

自动测试覆盖：

- 缺失技术路线的负向阻断；
- 三章唯一合同；
- 首轮跨章 P1 Finding；
- 仅技术路线定向重写；
- 中断后恢复；
- 第二次独立复审；
- Finding 从 `OPEN`/`REPAIR_RECORDED` 到 `VERIFIED`；
- 工作流、Gate、Run、质量和导出 API 未被并发分支合并误删。

该验收使用 `SIMULATED` 模式验证编排和证据链，不代表真实模型语义质量。真实模型和真实 Skill 的语义能力属于 G3。
