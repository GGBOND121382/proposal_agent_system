# 全文 Integration Critic 实施与验收

## 1. 阶段目标

本阶段完成 `docs/CONCURRENT_DEVELOPMENT_PLAN.md` 中“完整申请书并发编制”之后、DOCX/PDF 导出之前的全文 Integration Critic。它审查的是冻结 Section Contract 下的**完整最终候选集合**，而不是抽取若干章节或相信模型自行声明“已阅读全文”。

本阶段不修改安全分类、密级映射、WF-1/WF-3/WF-5、外发审批或 Gate 决策逻辑。

## 2. 完整输入硬门

在调用 `P-INTEGRATION-CRITIC` 前，编排器必须从 SQLite、冻结合同和五个子工作流建立可审计快照，并同时满足：

1. 五个并发组与冻结合同完全一致；
2. 每个子工作流已经 `COMPLETED`，且合同哈希与父工作流一致；
3. 每个章节只有一个责任子工作流；
4. `candidate_sections` 与 `document_section_map` 的章节顺序、`section_id` 和 `candidate_id` 完全一致；
5. 每个最终候选都能回溯到责任子工作流中的 `P-EXPRESSION-POLISH`；
6. 同一候选随后由独立的 `P-EXPRESSION-CRITIC` 复审并通过；
7. 全部章节、候选、责任工作流、Producer/Critic Run 和哈希组成稳定的 `candidate_set_hash`。

任一条件不满足时直接阻断，不允许使用 Replay 样例、占位对象或人工拼接正文补齐。

## 3. 全文审查内容

模型审查与确定性质量门共同覆盖：

- 中心命题是否贯穿背景、现状、问题、目标、任务、方法、实验和贡献；
- 差距→问题、问题→目标、目标→任务、任务→方法、方法→验证、结果→贡献六条论证链；
- 文种、中心命题、论证链、证据、方法、创新、基础、指标、章节独特性、表达密度、篇幅和跨章一致性十二个维度；
- 完全重复、语义模板重复、重复信息所有权、非中心命题过度集中和句式骨架重复；
- 术语、数字、目标—任务—方法—成果—指标映射；
- 结论是否回扣中心命题与创新；
- 创新是否绑定最近工作与新机制；
- 研究基础是否绑定真实前期证据；
- 指标是否绑定实验、基线或可核验依据；
- 主文是否出现 Prompt、Trace、Gate、部署说明等文种漂移；
- 主文页数和 Section Contract 预算。

模型输出中的链路 ID、章节 ID、候选 ID 和责任路由均由程序再次与真实输入对象核对，不能以模型自报替代证据。

## 4. 责任路由与返修

全文 Finding 必须路由到最早能够实质修复的阶段：

| 问题类型 | 责任阶段 | 处理方式 |
|---|---|---|
| 中心命题、论证架构、项目事实缺陷 | Argument Architecture / Project Knowledge | 作废下游并发代，重新形成论证架构 |
| 信息所有权、章节依赖、篇幅合同缺陷 | Planning Agent | 作废下游并发代，重新冻结 Section Contract |
| 可定位的章节正文冲突、重复或文种漂移 | Writing Agent | 只重写责任章节，复用未受影响章节 |
| 表达语义保持缺陷 | Expression Editor | 返回对应章节表达阶段 |
| 候选集合、映射或审查证据不完整 | Integration Agent / BLOCK | 修复工程输入，不让写作 Agent 改正文掩盖 |
| 必须由负责人确认的事实或范围 | USER | 保持未解决状态，不自动确认 |

禁止人工直接修改正文、直接修改 Finding 状态或用空 Gate 确认代替修复。

## 5. 独立复审与生命周期

每次全文审查保存：

- `run_id`、状态和审查序号；
- 完整章节清单和 `candidate_set_hash`；
- 五个责任子工作流 ID；
- 输入/输出哈希、模型 ID、端点 ID；
- Finding、责任路由和十二维/六链检查结果；
- 与上一次全文审查是否为不同 Run；
- 上一次返回 `REVISE/BLOCK` 后候选集合是否真实变化。

全文 Critic 只有在以下条件全部成立时才能 `PASS`：

- 十二个质量维度全部通过；
- 六条论证链全部完整；
- 中心命题覆盖成立；
- 无文种漂移、重复和篇幅超限；
- 无未解决项目；
- 无开放 P0/P1；
- 本次 Run 与前次独立；
- 返修后的 `candidate_set_hash` 与前次不同。

P0/P1 Finding 只有在存在责任 Agent 修复证据，并由后续独立全文 Critic 复审后，才能进入 `VERIFIED`。

## 6. 固定材料验收场景

`scripts/run_full_integration_critic_acceptance.py` 使用明确标注为 `SIMULATED` 的固定 14 章节材料执行三轮全文审查：

1. 第一轮注入技术路线术语冲突，路由到 Writing Agent，只重写技术路线；
2. 第二轮注入跨章信息所有权冲突，路由到 Planning Agent，作废旧并发代并重建全部章节；
3. 第三轮由新的 Integration Critic 对新候选集合独立复审并通过。

验收要求：

- 状态序列必须为 `REVISE → REVISE → PASS`；
- 三个 `run_id` 和三个 `candidate_set_hash` 均不同；
- 第一次返修只增加责任章节的 Content Run；
- 第二次返修归档旧子工作流并创建新并发代；
- 三个 P1 Finding 均具有修复和复审证据，最终开放 P0/P1 为 0；
- 请求、响应、Prompt Trace、SQLite、测试日志和恢复包完整。

## 7. 自动验收

专用只读工作流：

```text
.github/workflows/full-integration-critic.yml
```

主要命令：

```bash
python -m pytest -q tests/test_full_integration_critic.py
python scripts/run_full_integration_critic_acceptance.py \
  --output-dir recovery_evidence/full_integration/<commit>
```

专用 Gate 同时运行全仓回归、Prompt Pack 校验、Trace 审计以及恢复包构建和复核。Actions 权限仅为 `contents: read`，不自动提交或推送代码。

## 8. 能力边界

本阶段证明全文候选完整性、来源溯源、跨章质量硬门、责任路由、自动返修、独立复审和恢复证据闭合。固定验收使用 `SIMULATED` 响应，不能冒充真实模型科学判断、真实公开检索或最终 DOCX/PDF 视觉验收；后两项仍由后续 X/G3 正式能力验收证明。
