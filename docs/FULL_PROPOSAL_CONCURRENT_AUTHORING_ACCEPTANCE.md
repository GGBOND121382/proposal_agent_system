# 完整申请书并发编制实现与验收

## 1. 阶段定位

本阶段对应 `docs/CONCURRENT_DEVELOPMENT_PLAN.md` 依赖图中的：

```text
G2 小规模集成验收
→ 完整申请书并发编制
→ 全文 Integration Critic
→ DOCX/PDF 导出后验收
→ G3 正式能力验收
```

因此，本阶段的完成定义是：在论证架构和 Section Contract 冻结后，以五个相互隔离、可恢复的并发写作组完成全部章节，并由父工作流汇总为唯一有序候选集合，交给全文 Integration Critic。DOCX/PDF 后验收和真实模型/真实 Skill 能力证明属于后续阶段，不在此阶段冒充完成。

## 2. 并发架构

`WF-4_PROPOSAL_AUTHORING` 在 `full_proposal_concurrent=true` 或 `integration_scope=FULL_PROPOSAL_CONCURRENT` 时成为父协调工作流。父工作流冻结以下五组合同：

1. `GROUP_1_BACKGROUND_AND_PROBLEM`：背景与问题；
2. `GROUP_2_OBJECTIVES_AND_TASKS`：目标与任务；
3. `GROUP_3_METHOD_AND_VALIDATION`：方法与验证；
4. `GROUP_4_IMPLEMENTATION_AND_ASSURANCE`：实施与保障；
5. `GROUP_5_FIGURES_AND_REFERENCES`：图表与引用。

每个有独立章节的组创建一个持久化子工作流。五个子工作流通过 `asyncio.gather` 并发运行，但同一子工作流内部仍逐章串行；每章严格执行：

```text
Blueprint
→ Blueprint Critic
→ Targeted Repair（需要时最多一次）
→ 独立 Blueprint Critic 复审
→ Content
→ Content Critic
→ Targeted Repair（需要时最多一次）
→ 独立 Content Critic 复审
→ Expression Polish
→ Expression Critic
```

图表、公式、参考文献和交叉引用作为第五组及跨章节责任通道登记。不存在独立参考文献或附录章节时，该组以虚拟责任通道存在，不伪造正文章节。

## 3. Section Contract 冻结

父工作流按最终叙事架构中的章节顺序生成合同，并记录：

- 唯一 `section_id`；
- 章节标题和 Profile；
- 唯一责任组；
- 文档顺序；
- 五组清单及跨章节职责；
- 整体 `contract_hash`。

进入写作后，如果章节集合、顺序、Profile 或分组发生漂移，工作流直接阻断，要求返回 `P-REVISION-PLAN` 重新生成和复审，不允许在写作阶段静默改组。

## 4. 状态隔离与恢复

每个并发组拥有独立的：

- `workflow_id`；
- `state_json`；
- `section_progress`；
- `repair_attempts`；
- `repair_overrides`；
- Prompt Run 和 Trace；
- 开始、完成及审计事件。

父工作流只保存子工作流引用和不可变合同，不共享可变章节草稿。进程中断后：

- 已完成子工作流及其 Prompt 数量保持不变；
- 未完成组从自身章节检查点继续；
- 已完成组不会重新生成；
- 父工作流恢复后按合同顺序重新聚合结果。

子工作流虽然复用 `WF-4` 状态机，但带有 `parent_workflow_id`。`WF-5_SECURITY_REVIEW_AND_EXPORT` 的前置检查只接受完成的父 `WF-4`，任何单个写作组完成均不能提前解锁导出。

## 5. 并发上下文隔离

串行模式可以读取项目范围内最近一次 Producer 输出；并发模式不可以。`ContextBuilder` 现按以下联合条件选择章节候选：

```text
project_id + workflow_id + prompt_id + source_section.section_id
```

全文 Integration Critic 则只读取父状态登记的五个子工作流，按冻结合同的文档顺序聚合候选。这避免了一个组的 Blueprint、Content 或 Polish 输出被另一个组的 Critic 错读。

## 6. 全文 Finding 责任路由

全文 Integration Critic 发现写作类 P0/P1 问题时：

1. 从 Finding 的目标路径、证据引用和一致性检查中解析受影响 `section_id`；
2. 查找该章节的唯一责任组；
3. 只清除责任章节的结果、进度和章节级 Repair Override；
4. 未受影响子工作流保持完成并复用；
5. 责任组重新执行完整章节链；
6. 由新的 Integration Critic Run 独立复审；
7. 只有修复证据和后续复审证据均存在时，质量 Finding 才可进入 `VERIFIED`。

人工直接修改正文、Gate 空确认或直接改数据库状态均不属于修复证据。

## 7. 自动验收

专用工作流：

```text
.github/workflows/full-proposal-concurrent.yml
```

它执行：

- G0 合同与 Prompt Pack 校验；
- 新增并发合同、五组闭合、恢复和责任返修测试；
- S1/S2 兼容性回归；
- 全仓 pytest；
- 固定 14 章节完整申请书并发运行；
- 请求、响应、Prompt Trace 和材料清单导出；
- SQLite 检查点和恢复包构建/复核；
- 最终 Artifact 上传。

核心自动检查包括：

- 四个核心写作组不可缺失；
- 14 个章节只归属一个组；
- 五个独立子工作流；
- 组间执行时间重叠；
- 组内及章内严格串行；
- 全文候选集合与合同完全一致；
- Integration Critic 为 PASS；
- 无未关闭 P0/P1；
- 子工作流不能解锁 WF-5；
- 完成组在重启后不重复生成；
- 全文问题只重写责任章节并由新 Critic 复审。

## 8. 阶段证据

验收目录包含：

```text
source_commit.txt
environment_manifest.json
input_material_manifest.json
workflow_checkpoint.sqlite
requests/
responses/
prompt_traces/
research_archive/
mermaid_artifacts/
exports/
test_logs/
FULL_PROPOSAL_CONCURRENT_ACCEPTANCE.json
acceptance_report.md
recovery_bundle.zip
```

## 9. 能力边界

本阶段 CI 使用明确标注的 `SIMULATED` 固定材料，证明的是五组并发编排、章节隔离、断点恢复、责任返修和全文 Integration Critic 的工程闭合，不将模拟文本冒充真实模型语义质量。

要宣称 G3 正式能力通过，仍必须在人工启动的固定材料任务中使用真实模型和真实 Research Skill，保留原始响应，完成真实 Mermaid、DOCX/PDF、结构和页面视觉验收，并满足无 Replay、无模拟响应和无人工改正文等条件。
