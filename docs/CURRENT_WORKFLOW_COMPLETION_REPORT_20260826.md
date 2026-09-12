# 当前工作流完成情况说明

## 1. 报告范围与证据口径

本文说明项目 `project-8595abee7b9c4047` 截至 2026-09-01 的实际工作流状态、各 Prompt 的输入与输出，以及 WF-3、WF-4 近期在知识图谱、确定性校验和证据追踪方面的强化。

本文首次出现的 Workflow 均同时标注中文名称：

- WF-1 `PROJECT_INTAKE`（项目摄取与知识底座）：`COMPLETED`；
- WF-2 `TEMPLATE_EXTRACTION`（参考结构抽取）：`COMPLETED`；
- WF-3 `HYBRID_ONLINE_ASSIST`（混合联网辅助与公开研究）：`COMPLETED`，研究充分性为 `DEGRADED`，保留 2 个确定性研究缺口；
- WF-4 `PROPOSAL_AUTHORING`（申请书编写）：最新实例为 `BLOCKED_CONTRACT`。阶段 0 的论证架构 Producer 已通过，但独立 Argument Critic 未能产生符合语义契约的结果，因此不能把 WF-4 或阶段 0 整体表述为已完成。

“实际输入”“实际输出”和状态以 SQLite 中持久化的 Workflow、Prompt Run、Gate、Artifact 与检索归档为准；Replay 示例、被拒绝候选和旧实例只用于解释历史，不作为当前结果。

| 对象 | 当前证据 |
|---|---|
| 项目 | `project-8595abee7b9c4047` |
| 主输入材料 | `doc-46545e6862d5497b`：`PROJECT_BRIEF_人机协同决策优势冲刺_系统适配精简版.md` |
| WF-1 | `wf-e5775eba63c04057`，`COMPLETED` |
| WF-2 | `wf-f1f90c80a90b4bb9`，`COMPLETED` |
| WF-3 | `wf-5bc29f8daa544df8`，`COMPLETED`，步骤 8 |
| WF-3 检索归档 | `data/research_archive/project-8595abee7b9c4047/research-1b742177dd894afb/` |
| WF-4 | `wf-e30039f893bf4f72`，`BLOCKED_CONTRACT`，步骤 1 |
| WF-4 前置版本 | 明确冻结到上述 WF-1、WF-2 和最新 WF-3，不使用“当前最新值”隐式漂移 |

近期机制说明和未完成边界还交叉核对了 `docs/WF3_RESEARCH_QUALITY_KNOWLEDGE_GRAPH.md`、`docs/WF3_DETERMINISTIC_OWNERSHIP_AUDIT_20260828.md`、`docs/WF3_STAGE0_CROSSCHECK_CHECKLIST_20260826.md`、`docs/TODO_WF3_KNOWLEDGE_GRAPH_DRIVEN_PROMPT_CONTRACT.md` 与 `docs/TODO_STAGE0_CONTRACT_BOUNDARIES_20260821.md`。

## 2. Prompt 的共同组成与中文名称

各 Prompt 原文位于 `prompt_pack/prompts/`。共同组成包括：

1. 元数据：Prompt ID、版本、执行角色、运行环境、模型配置、后续 Gate 与严格 JSON 输出要求。
2. 角色与权限：模型只能完成本节点的语义任务，不能修改数据库、批准 Gate、扩大外发范围或决定工作流控制状态。
3. 输入契约：列出可读取的 payload、上游对象和版本约束；缺少必需输入时必须显式报告。
4. 执行算法：定义抽取、审查、研究规划、综合或论证构建步骤。
5. 语义判定：规定 Finding、未决项、用户问题、来源引用和警告的含义。
6. 来源与知识状态：区分来源事实、公开研究结论、模型归纳、项目计划和已完成成果。
7. 强制自检：检查来源支持、限定词、`UNKNOWN`、越权、敏感信息和 JSON 契约。
8. 输出 Envelope：通常包含 `result`、`findings`、`unresolved_items`、`user_questions`、`source_refs` 和 `warnings`。

近期强化后，模型输出不再被直接视为控制事实：工作流状态、阻断路由、覆盖充分性、确定性关系和持久化版本由运行时代码计算。模型负责语义内容和语义判断，不能用自己输出的 `PASS`、ID 或 Hash 覆盖运行时结论。

本文涉及的 Prompt 首次出现及中文名称如下：

| Prompt（首次出现含中文名称） | 主要职责 | 当前版本 |
|---|---|---:|
| `P-SECURITY-CLASSIFY`（安全分类） | 判断材料安全等级和外发边界 | 2.0.0 |
| `P-SECURITY-CLASSIFY-CRITIC`（安全分类独立审查） | 独立复核安全分类 | 2.0.0 |
| `P-SCHEME-EXTRACT`（申报规则抽取） | 抽取申报规则画像 | 2.0.0 |
| `P-SCHEME-CRITIC`（申报规则独立审查） | 复核规则来源、数值和遗漏 | 2.0.0 |
| `P-PROJECT-DEFINITION-EXTRACT`（项目定义抽取） | 构造项目定义、合同和论证种子 | 3.0.0 |
| `P-PROJECT-DEFINITION-CRITIC`（项目定义独立审查） | 审查项目对象、关系和论证种子 | 3.0.0 |
| `P-FACT-EXTRACT`（事实抽取） | 抽取最小可判真事实 | 2.0.0 |
| `P-FACT-CRITIC`（事实独立审查） | 回查事实来源和限定条件 | 2.0.0 |
| `P-PROJECT-READINESS-CRITIC`（项目准备度审查） | 判断能否进入下一编写阶段 | 3.0.0 |
| `P-TEMPLATE-EXTRACT`（参考结构抽取） | 从参考材料提取可复用结构 | 3.0.1 |
| `P-TEMPLATE-CRITIC`（参考结构独立审查） | 检查结构模板污染和遗漏 | 3.0.0 |
| `P-SAFE-ONLINE-PACKAGE`（安全在线任务包生成） | 将内部需求最小化为可外发任务 | 2.0.0 |
| `P-SAFE-ONLINE-PACKAGE-CRITIC`（安全在线任务包独立审查） | 复核外发范围和重识别风险 | 2.0.0 |
| `P-PUBLIC-RESEARCH-PLAN`（公开研究计划） | 生成研究问题、查询和证据要求 | 2.1.0 |
| `P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC`（公开研究计划范围审查） | 审查查询是否越界及是否忠实覆盖获批主题 | 1.0.0 |
| `P-PUBLIC-RESEARCH-SYNTHESIS`（公开研究综合） | 将归档来源综合为来源绑定的 Claim | 2.0.0 |
| `P-PUBLIC-RESEARCH-CRITIC`（公开研究独立审查） | 审查 Claim 支持度、反证和适用边界 | 2.0.0 |
| `P-ONLINE-RESULT-IMPORT-CRITIC`（在线结果导入审查） | 在离线环境审查外部结果能否导入 | 2.0.0 |
| `P-ARGUMENT-ARCHITECTURE`（论证架构生成） | 生成阶段 0 的权威论证内容 | 9.0.0 |
| `P-ARGUMENT-ARCHITECTURE-CRITIC`（论证架构独立审查） | 独立审查论证闭环、证据和研究设计 | 9.1.0 |

## 3. WF-1：项目摄取与知识底座

### 3.1 总体结果

WF-1 已完成。部分模型节点返回 `REVISE` 或 `NEED_USER_INPUT`，随后经人工 Gate 明确确认；因此 `COMPLETED` 表示流程上完成，并不表示每个模型节点都曾直接返回 `PASS`。

| 步骤 | Prompt | 实际状态 | 主要产物 |
|---:|---|---|---|
| 0 | `P-SECURITY-CLASSIFY` | `PASS` | 主材料安全等级、敏感字段、组合风险、允许环境 |
| 1 | `P-SECURITY-CLASSIFY-CRITIC` | `PASS` | 安全分类独立复核 |
| 2 | `P-SCHEME-EXTRACT` | `REVISE`，Gate 接受 | 申报规则画像、覆盖情况和未决规则 |
| 3 | `P-SCHEME-CRITIC` | `PASS` | 规则来源、数值和遗漏审查 |
| 4 | `P-PROJECT-DEFINITION-EXTRACT` | `NEED_USER_INPUT`，Gate 接受 | 项目定义、Proposal Contract、论证图种子 |
| 5 | `P-PROJECT-DEFINITION-CRITIC` | `NEED_USER_INPUT`，Gate 接受 | 项目对象、关系、论证种子审查 |
| 6 | `P-FACT-EXTRACT` | `PASS` | 23 条最小事实候选 |
| 7 | `P-FACT-CRITIC` | `PASS` | 23 条事实全部接受 |
| 8 | `P-PROJECT-READINESS-CRITIC` | `NEED_USER_INPUT`，Gate 接受 | 可进入论证架构阶段，尚未达到章节规划准备度 |

### 3.2 各 Prompt 的输入、处理与输出

#### `P-SECURITY-CLASSIFY` 2.0.0

- Prompt 要点：识别敏感实体、参数、场景、身份与组合推断风险；只能建议安全等级，不能自行批准降级。
- 实际输入：1 个对象上下文、17 个内容分段、安全策略、既有标签和预期用途。
- 实际输出：建议等级 `INTERNAL`；3 类敏感实体、3 类敏感字段、2 项组合风险、2 个允许环境、8 条理由，置信度 `HIGH`。
- 实际结果：`PASS`，无 Finding 和未决项；保留 3 条非阻断警告。

#### `P-SECURITY-CLASSIFY-CRITIC` 2.0.0

- Prompt 要点：从原始对象重新识别敏感内容，检查漏标、错误降级、组合推断和允许环境。
- 实际输入：安全分类候选、原始对象、安全策略和确定性扫描结果。
- 实际输出：`verdict=ACCEPT`，复核 8 个维度并认可 `INTERNAL`。
- 实际结果：`PASS`。

#### `P-SCHEME-EXTRACT` 2.0.0

- Prompt 要点：区分强制条款、建议和示例；抽取方向、周期、预算、指标、章节、篇幅、排除范围和合规要求；每项规则绑定来源。
- 实际输入：指南对象、文档结构、抽取范围，无既有规则画像。
- 实际输出：15 字段 `scheme_profile` 和覆盖记录。
- 实际结果：`REVISE`；经 `PROJECT_GAP_RESOLUTION` Gate 明确接受。

#### `P-SCHEME-CRITIC` 2.0.0

- Prompt 要点：回查规则来源，检查强制/建议混淆、数值、周期、范围、附件和排除条款。
- 实际输入：规则候选、指南材料和确定性 Finding。
- 实际输出：`verdict=ACCEPT`，检查 13 条规则和 2 项数值条件。
- 实际结果：`PASS`；随后通过 `SCHEME_CONFIRMATION` Gate。

#### `P-PROJECT-DEFINITION-EXTRACT` 3.0.0

- Prompt 要点：区分科研申请书与工程项目；构造项目事实图、Proposal Contract 和 Argument Graph Seed；不得把系统建设直接当作科学命题。
- 实际输入：源文档、规则画像、抽取范围和安全约束。
- 实际输出：11 字段项目定义、8 项覆盖、2 个未映射 Span、11 字段 Proposal Contract 和 6 字段论证图种子。
- 实际结果：`NEED_USER_INPUT`；经 `PROJECT_GAP_RESOLUTION` Gate 接受。

#### `P-PROJECT-DEFINITION-CRITIC` 3.0.0

- Prompt 要点：核对对象类型、关系方向、科学问题/技术瓶颈/工程任务区分、参考材料污染及知识状态。
- 实际输入：项目定义候选、源文档、规则画像、关系矩阵、合同和论证种子。
- 实际输出：检查 24 个对象、17 条关系和 8 项论证条件，发现 1 条无效关系。
- 实际结果：`NEED_USER_INPUT`；经 `PROJECT_DEFINITION_CONFIRMATION` Gate 接受。

#### `P-FACT-EXTRACT` 2.0.0

- Prompt 要点：将来源 Span 拆成单一可判真命题，保留主体、时间、数字、单位、否定和限定词，并区分事实、计划和预期结果。
- 实际输入：16 个来源 Span、权威规则和安全约束。
- 实际输出：23 条事实候选、0 个冲突、10 项覆盖记录。
- 实际结果：`PASS`。

#### `P-FACT-CRITIC` 2.0.0

- Prompt 要点：逐条回查来源，检查计划冒充完成、主体错配、数字/单位/条件丢失、否定与限定词丢失。
- 实际输入：23 条事实候选、16 个来源 Span、既有事实和权威规则。
- 实际输出：23 条全部接受，0 条拒绝、0 个冲突、0 个锁定事实违规。
- 实际结果：`PASS`；随后通过 `FACT_CONFIRMATION` Gate。

#### `P-PROJECT-READINESS-CRITIC` 3.0.0

- Prompt 要点：按完整度、确认度、证据度和冲突判断是否可进入论证架构或章节规划，不得用模型推断填补缺口。
- 实际输入：项目定义、事实包、规则画像、章节 Profile、任务指令、Proposal Contract 和论证图。
- 实际输出：12 个领域评分、11 个章节准备度、4 个可写章节、7 个阻断章节、6 个缺失输入和 11 项关键检查。
- 实际判定：`ready_for_argument_architecture=true`、`ready_for_section_planning=false`。
- 实际结果：`NEED_USER_INPUT`；Gate 接受后 WF-1 完成。

## 4. WF-2：参考结构抽取

### 4.1 `P-TEMPLATE-EXTRACT` 3.0.1

- Prompt 要点：只提取参考材料可复用的论证结构、章节功能、段落角色、图表/公式模式和格式规则；剔除参考项目名称、成果、技术和数字。
- 实际输入：1 个参考文档对象、17 节章节树、样式摘要、抽取范围和安全约束。
- 实际输出：8 字段模板对象、12 项覆盖、11 项来源事实排除记录。
- 实际结果：`PASS`。

### 4.2 `P-TEMPLATE-CRITIC` 3.0.0

- Prompt 要点：检查模板是否只保留结构，是否夹带参考项目实体/数字，是否遗漏图表公式模式和论证功能。
- 实际输入：模板候选、参考文档和确定性 Finding。
- 实际输出：`verdict=ACCEPT`；检查 11 个模板组件，0 个污染组件，0 个缺失章节功能。
- 实际结果：`PASS`；`TEMPLATE_CONFIRMATION` Gate 批准后 WF-2 完成。

## 5. WF-3：混合联网辅助与公开研究

### 5.1 当前流程与实际完成状态

WF-3 先把项目内的研究需要收敛为可公开讨论的主题，在离线环境完成最小化和安全审查；人工批准外发后，在线生成研究计划，并在检索前增加独立范围审查。检索结果经过候选筛选、去重、归档、覆盖矩阵和研究缺口判定，再进入综合、独立审查和离线导入。

```text
项目状态中的公开研究需要
  -> P-SAFE-ONLINE-PACKAGE（离线最小化）
  -> P-SAFE-ONLINE-PACKAGE-CRITIC（离线安全复核）
  -> OUTBOUND_SECURITY_APPROVAL（人工批准外发）
  -> P-PUBLIC-RESEARCH-PLAN（在线研究计划）
  -> P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC（查询范围审查）
  -> PUBLIC_SEARCH（运行时多源检索、筛选、归档和覆盖判定）
  -> P-PUBLIC-RESEARCH-SYNTHESIS（来源绑定综合）
  -> P-PUBLIC-RESEARCH-CRITIC（支持度与反证审查）
  -> P-ONLINE-RESULT-IMPORT-CRITIC（离线导入审查）
  -> ONLINE_RESULT_IMPORT_APPROVAL（人工批准导入）
```

本次 WF-3 从既有论证图研究需要中解析输入，而不是再次要求用户重复描述项目：

- 输入来源：`ARGUMENT_GRAPH_RESEARCH_QUESTIONS`；
- Need ID：`need-af869725307ccc2716f0`；
- 上游语义对象：2 项；
- 任务类型：`PUBLIC_RESEARCH`；
- 允许主题：人机协同决策、可审计决策流程、智能体、大模型。

最新实例 `wf-5bc29f8daa544df8` 已完成全部 8 个步骤。它的完成语义是 `COMPLETED_WITH_RESEARCH_GAPS`：流程和导入均已完成，但系统没有把覆盖不足伪装成充分研究。

### 5.2 安全在线任务包与外发 Gate

#### `P-SAFE-ONLINE-PACKAGE` 2.0.0

- Prompt 要点：把内部研究需要收敛为最小公开任务；删除非必要内部上下文；明确允许主题、禁止推断、禁止输出和有效期。
- 实际输入：已解析的研究需要、允许主题、任务类型和安全策略。
- 实际输出：`PUBLIC` 级安全包，含 8 条 seed query、3 项允许上下文、4 条禁止推断、4 类禁止输出；本次上游已完成范围收敛，因此占位符和删除字段均为 0。
- 有效期：至 2026-09-05。
- 实际结果：`PASS`。

#### `P-SAFE-ONLINE-PACKAGE-CRITIC` 2.0.0

- Prompt 要点：复核越界字段、重识别风险、允许主题、有效期和禁止推断，不能替代人工批准。
- 实际输入：安全包候选、安全策略和确定性预扫描结果。
- 实际输出：`ACCEPT_FOR_HUMAN_APPROVAL`，风险 `LOW`。
- 实际结果：`PASS`；对应的 `OUTBOUND_SECURITY_APPROVAL` Gate 已批准。

近期的关键强化是：安全包和安全审查之前增加了运行时预检查，获批主题被解析一次并传给后续消费者；模型不得自行扩大主题集合，控制状态也不由模型声明。

### 5.3 公开研究计划与范围审查

#### `P-PUBLIC-RESEARCH-PLAN` 2.1.0

- Prompt 要点：把获批公开主题拆成研究问题和可执行查询；每个查询显式绑定研究问题；定义来源优先级、精确时间范围、证据要求、冲突处理和禁止推断。
- 实际输入：已批准安全包、研究任务类型、时间约束、已知公开来源和证据要求。
- 实际输出：7 个研究问题、8 条可执行查询、6 类来源优先级、时间范围 `2021-08-28/2026-08-28`、4 条证据要求和 4 条禁止推断。
- 实际结果：`PASS`。

实际执行的 8 条查询如下：

| 索引 | 查询 | 绑定研究问题 |
|---:|---|---|
| 0 | `human-in-the-loop decision making survey review methodology framework evaluation metrics 2022-2025` | 0 |
| 1 | `auditable decision process workflow versioning contract state management research 2021-2025` | 1、2 |
| 2 | `modular decision pipeline contract-based composition modularity decomposition formal methods 2021-2025` | 1 |
| 3 | `decision state propagation evidence model versioned facts incremental update research 2022-2025` | 2 |
| 4 | `multi-agent collaboration role allocation responsibility delegation limited attention resource management 2021-2025` | 3 |
| 5 | `LLM agent decision making tool use function calling planning reasoning framework 2022-2025` | 4 |
| 6 | `human-AI collaboration short-loop feedback rollback recovery checkpoint mechanism research 2021-2025` | 5 |
| 7 | `decision system benchmark evaluation quality latency robustness auditability dataset 2022-2025` | 6 |

#### `P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC` 1.0.0

- 目的：这是近期新增的独立边界检查。它不重新生成计划，只判断各查询是否忠实覆盖获批主题、是否泄露内部语义、是否出现跨语言改写后的隐性越界。
- 实际输入：安全包、研究计划和逐查询绑定关系。
- 实际输出：索引 0～7 全部批准，0 条拒绝。
- 实际结果：`PASS`。

该节点解决的是纯字符串包含检查不足的问题：主题可被同义改写或跨语言表达，因此需要语义范围判断；但最终是否允许检索、哪些查询进入执行仍由运行时根据审查结果确定。

### 5.4 多源检索、筛选和归档

本次检索模式为 `LIVE_HYBRID_ACADEMIC_WEB`。启用 OpenAlex、Crossref 和 SearXNG；Semantic Scholar 已禁用。三个启用 Provider 对 8 条查询均执行成功：

| Provider | 成功查询 | 原始结果数 |
|---|---:|---:|
| OpenAlex | 8/8 | 64 |
| Crossref | 8/8 | 64 |
| SearXNG | 8/8 | 48 |

执行链的数量变化为：176 个原始候选 -> 82 个语义筛选候选 -> 80 个去重候选 -> 40 个最终归档来源。最终来源上限为 40，每条查询最低目标为 3，并启用确定性语义相关性门控。

8 条查询在最终集合中的选中数分别为 `5、3、0、1、14、12、4、3`。这组不均衡分布没有被平均数掩盖：第 3 条查询（索引 2）为 0，第 4 条查询（索引 3）只有 1 条，均未满足逐查询覆盖门槛。

最终 40 个来源的类型为：

- 21 篇同行评议论文；
- 1 篇会议论文；
- 1 个书籍章节；
- 17 个学术预印本。

标准化发表状态为：22 个同行评议来源、1 个已发表非同行评议来源、4 个明确预印本、13 个状态未知。来源 Provenance 覆盖 OpenAlex、Crossref、SearXNG 和 arXiv；Provider 最大占比为 0.35，通过来源集中度检查。

全局数量指标均通过：40 个来源（最低 24）、22 个权威来源（最低 8）、40 个近期来源、21 个可比较基线来源、15 个局限来源、38 个作者团队、26 个 Publisher。但是逐问题覆盖仍不足：

- 查询索引 2：0/3 总来源，0/1 权威来源；
- 查询索引 3：1/3 总来源，0/1 权威来源。

因此运行时确定性判定为：

- `RetrievalHealth=PASS`：Provider 执行健康、8 条查询均实际执行；
- `CoverageOverall=INSUFFICIENT`：逐查询深度和权威来源不足；
- `ResearchSufficiency=DEGRADED`：生成 2 个 `ResearchGap`，允许带缺口继续；
- 工作流完成不改变研究充分性，最终 Artifact 仍保留这 2 个缺口。

归档位于：

`data/research_archive/project-8595abee7b9c4047/research-1b742177dd894afb/`

归档校验为 `PASS`，40 个来源均有 Manifest 和可追踪记录，无归档失败。

### 5.5 WF-3 的知识图谱与严谨性强化

WF-3 近期强化的重点不是简单增加字段，而是把“研究问题、检索执行、证据覆盖和充分性结论”拆成不同对象，避免用一个宽泛的命中数替代完整证据链。当前核心链路为：

```text
ResearchQuestion
  -> QueryBinding
  -> ProviderQuery
  -> ProviderExecutionReceipt / RetrievalHealth
  -> ResearchCandidate
  -> CandidateSemanticRelevance
  -> CandidateSelection
  -> ArchivedSource
  -> EvidenceQuestionBinding
  -> CoverageMatrix
  -> ResearchGap
  -> ResearchSufficiency
  -> Synthesis / Critic / WF3ResearchResult
```

同时引入以下边界：

| 边/判断 | 所有者 | 含义 |
|---|---|---|
| 主题、方法、局限等语义联系 | 模型可参与 | 属于语义候选，必须受来源和范围约束 |
| Query 到问题的绑定、候选到来源的追踪 | 运行时 | 属于确定性或 Trace 关系，不能由模型任意改写 |
| Provider 健康度、覆盖矩阵、研究充分性、工作流路由 | 运行时 | 属于控制事实，模型无权宣布通过 |
| Claim 到 Source 的引用存在性和证据层 | 运行时校验 | 引用必须落在本次来源目录中，不能编造 Source ID |

当前强制遵守的主要不变量包括：

1. 检索命中不等于证据覆盖；候选只有通过与特定查询相关的筛选后，才能计入该问题覆盖。
2. 相关性是查询特定的；同一来源对一个问题相关，不代表对所有问题均相关。
3. DOI 只证明标识存在，不等于同行评议。
4. Provider 健康度与来源数量分开判断；“接口成功”不等于“研究充分”。
5. 查询深度和权威来源深度逐查询检查，不能用全局总量抵消局部空洞。
6. `ResearchSufficiency` 由运行时计算，模型不能把已知缺口改写为 `PASS`。
7. 已知缺口只表示证据不足，不是要求模型发明证据。
8. Workflow `COMPLETED` 不会清除 `DEGRADED` 或 `ResearchGap`。

此外，来源优先级已实际进入排序与报告逻辑；计划、检索和综合均保留接受基线及运行标识，未通过的候选不能覆盖已接受结果。Prompt 面向 Provider 的字符预算也按节点单独限制，避免把无关控制元数据和重复全文传给模型。

### 5.6 综合、独立审查与离线导入

#### `P-PUBLIC-RESEARCH-SYNTHESIS` 2.0.0

- Prompt 要点：只能使用本次归档来源；逐 Claim 绑定 Source；区分来源陈述、跨来源归纳和推断；并列冲突、局限和适用边界。
- 实际输入：研究计划、40 个来源目录、来源摘录、安全在线任务包、覆盖矩阵和研究缺口。
- 调用过程：前两份可解析候选因 `source_comparisons` 中引用不存在或不匹配的 Source ID 被拒绝；第三份通过。
- 实际输出：27 个 Claim、4 个跨来源比较、3 个冲突、7 项局限。
- 实际结果：`PASS`。

#### `P-PUBLIC-RESEARCH-CRITIC` 2.0.0

- Prompt 要点：核验来源质量、时效、Claim 支持度、反证、局限和安全范围，并给出是否可进入导入审查的建议。
- 实际输入：研究计划、综合候选、40 个来源和安全包。
- 实际输出：`ACCEPT_FOR_IMPORT_REVIEW`；无不受支持 Claim ID，无缺失反证主题。
- 实际结果：`PASS`。

#### `P-ONLINE-RESULT-IMPORT-CRITIC` 2.0.0

- Prompt 要点：回到离线环境核对 Manifest、任务范围、Prompt 注入、来源引用和是否将公开研究错误升级为项目事实。
- 实际输入：已批准安全包、研究结果包、40 个来源、Transfer Manifest 和安全策略。
- 调用过程：第一份响应根对象错误且遗漏决策，被契约拒绝；第二份通过。
- 实际输出：24 个 Claim 接受导入，3 个仅作参考，0 个拒绝；未发现 Prompt 注入或范围越界。
- 确定性绑定：27 个 Claim 全部通过 Source Catalog 校验，每个 Claim 均保留 `ORIGINAL_SNAPSHOT -> SOURCE_EXTRACT -> MODEL_SYNTHESIS` 三层证据链。
- 实际结果：`PASS`；对应的 `ONLINE_RESULT_IMPORT_APPROVAL` Gate 批准后 WF-3 完成。

### 5.7 当前边界

当前归档以元数据和来源摘录为主，并不等于对 40 篇材料逐篇完成全文方法审阅。完整的 EvidenceCard、全文级语义蕴含、量表、样本量、统计过程和精确阈值核验仍属于后续工作。当前知识图谱能够严格回答“哪项研究需要经哪条查询、由哪个 Provider、落到哪个归档来源并支撑哪个 Claim”，但不能把短摘录提升为全文证据。

## 6. WF-4：申请书编写与阶段 0 论证架构

### 6.1 当前状态

最新实例 `wf-e30039f893bf4f72` 显式绑定已完成的 WF-1、WF-2 和最新 WF-3。阶段 0 Producer 已产生并通过完整校验的论证架构，但后续独立 Critic 连续契约失败，工作流当前为 `BLOCKED_CONTRACT`、步骤 1。

这与旧报告中的“实例级跳过 Critic、继续到下一节点”不同：最新实例没有把 Critic 伪造为通过，也没有把 Producer 通过等同于阶段 0 整体完成。

### 6.2 `P-ARGUMENT-ARCHITECTURE` 9.0.0

- Prompt 目的：依据 Proposal Contract、项目子图、确认事实、论证种子、参考结构和 WF-3 公开证据，生成可比较、可证伪、能形成研究闭环的权威语义状态。
- 主要规则：每条线程应形成“局限机制 -> 研究缺口 -> 研究问题 -> 目标 -> 工作包 -> 方法 -> 理论属性/实验 -> 基线/消融 -> 成功标准 -> 创新/贡献”的闭环；公开来源只能作为文献依据，不能冒充项目成果。
- 实际输入：11 字段 Proposal Contract、4 字段项目子图、47 条确认事实、6 字段论证图种子、8 字段模板上下文、16 个章节对象、11 字段任务指令、4 条 Revision Finding，无人工补充回答。
- 调用过程：先后出现非法 JSON、输出 Token 上限和含 4 个语义缺口的 `REVISE`；随后一次完整重生成得到通过候选。
- 实际结果：Producer `PASS`。

通过候选包含：

- 4 条研究线程；
- 4 个非阻断 `EvidenceGap`；
- 4 行研究设计矩阵；
- 4 组线程假设；
- 19 个实际 Source Reference；
- `readiness.ready=true`，无阻断节点。

四条研究线程聚焦：

1. 决策功能分解与统一数据/证据底座；
2. 人员主导的多智能体动态分工与短周期反馈；
3. 任务—能力—资源候选生成与可解释排序；
4. 多维指标联合评价、失效边界与可信性保障。

4 个非阻断证据缺口主要涉及团队/既有基础证据、指标与统计方案中的 `UNKNOWN`、最近工作只有概念级比较，以及第 4 条线程的指标依据。这些缺口被保留为缺口对象，没有由模型自行补造。

### 6.3 WF-4 的知识图谱与严谨性强化

WF-4 当前不再要求模型同时维护多份互相独立的完整图结构。模型生成的 `authored_state` 是语义权威源；运行时以 `ARGUMENT_PROJECTOR_V2` 从该状态确定性投影 Argument Graph、Research Design Matrix、Evidence Binding 和机器元数据。这样可避免“正文对象正确，但模型重复填写的图、矩阵或 ID 相互矛盾”。

```text
Model Authored State（语义权威源）
  -> Deterministic Argument Projector
     -> Argument Graph
     -> Research Design Matrix
     -> Evidence Bindings
     -> Projection Meta / Hash / Runtime IDs
  -> Deterministic Closure Validation
  -> 可接受候选或精确错误路径
```

本次确定性投影结果为 121 个论证对象、108 条关系和 121 条证据绑定：

| 对象类型 | 数量 | 对象类型 | 数量 |
|---|---:|---|---:|
| Central Proposition | 1 | Research Question | 4 |
| Research Gap | 4 | Limitation Mechanism | 4 |
| Objective | 4 | Work Package | 4 |
| Analytical Method | 4 | Assumption | 24 |
| Theoretical Property | 8 | Experiment Design | 8 |
| Baseline | 8 | Ablation | 8 |
| Success Criterion | 24 | Novel Mechanism | 4 |
| Contribution | 4 | Closest Prior Work | 8 |

108 条关系覆盖 `MOTIVATES`、`ADDRESSED_BY`、`DECOMPOSES_TO`、`USES`、`ASSUMES`、`HAS_PROPERTY`、`VALIDATED_BY`、`COMPARES_WITH`、`INCLUDES_ABLATION`、`MEASURED_BY`、`YIELDS`、`CONTRASTS_WITH` 和 `EVIDENCES` 等闭环语义。

确定性检查确认：

- 121 个对象 ID 唯一；
- 108 条边的起点和终点均存在；
- 研究线程内关系和跨对象引用合法；
- 121 个对象均有证据绑定；
- 19 个 Source Reference 均来自允许的来源目录，0 个无效来源引用；
- 研究设计矩阵引用的对象与论证图一致；
- `blocking=false` 的缺口保留但不错误阻断，阻断缺口则不能被 `readiness=true` 覆盖。

候选只有经过全量结构、关系和语义闭包校验后才能成为新的权威状态；非法或退化候选不会仅凭模型自报 `PASS` 覆盖已接受状态。机器 ID、Hash、索引、投影状态和路由尽量由运行时生成或校验，而不是要求模型反复恢复。

### 6.4 `P-ARGUMENT-ARCHITECTURE-CRITIC` 9.1.0

- Prompt 目的：独立复核 Producer 的论证闭环、证据支持、假设、方法—评价映射、基线、消融和成功标准。
- 实际输入：已通过确定性校验的权威状态、论证图投影、研究设计矩阵、证据绑定和审查契约。
- 实际响应：6 份响应均为可解析 JSON，但均未满足语义契约。
- 主要错误：使用错误的 Evidence ID 命名空间；`component` 与 `review_unit` 不一致；遗漏 24 个假设的审查覆盖；输出额外字段或错误的空值结构；Finding Code 与目标组件不兼容。
- 当前重试缺陷：多次重试基本重复同一语义请求，没有把上一轮的精确契约错误转成足够小且明确的修复任务。
- 实际结果：`PROVIDER_RETRIES_EXHAUSTED`，WF-4 为 `BLOCKED_CONTRACT`。

因此，当前能够确认的是“Producer 权威状态和确定性知识图谱投影通过”；尚不能确认“阶段 0 已通过独立语义 Critic”。这一边界必须保留。

## 7. 当前可用资产、结论与后续边界

目前已经具备：

- 经安全分类和人工确认的主输入材料；
- 申报规则画像、项目定义、事实包、Proposal Contract 和论证图种子；
- 经污染检查的可复用参考结构；
- WF-3 的 40 个归档来源、27 个来源绑定 Claim、24 个可导入 Claim、3 个参考 Claim 和 2 个显式研究缺口；
- WF-3 从 Research Question 到 Provider、归档来源、Claim 和 Research Gap 的可追踪知识链；
- WF-4 阶段 0 的 4 条研究线程、121 个论证对象、108 条关系、研究设计矩阵和证据绑定；
- WF-4 的“权威语义状态 -> 确定性投影 -> 闭包校验”机制。

当前不需要为继续 WF-4 重复一次全量公开检索。WF-4 消费 WF-3 已批准导入的公开证据；若论证或后续写作出现明确、可公开检索且会影响内容的具体缺口，应回到 WF-3 做有边界的补充检索，而不是由 WF-4 隐式联网。

仍需保留以下限制：

- WF-1 的 `COMPLETED` 含人工接受的非 PASS 产物。
- WF-3 是“完成但研究充分性降级”，不能将 40 个来源总量解释为 7 个研究问题均已充分覆盖。
- WF-3 当前主要完成来源级和摘录级证据追踪，尚未完成全部来源的全文 EvidenceCard 和全文级蕴含检查。
- 自动从统一知识图谱生成所有 Prompt Schema、彻底消除模型输出中的机器控制字段，仍是后续方向，不能表述为已经全部实现。
- WF-4 最新实例仍阻断在 Argument Critic；Producer 通过不等于阶段 0 或 WF-4 完成。
- Critic 的精确失败反馈、轻量审查单元和失败恢复仍需继续收口；在此之前不得用跳过或伪造 `PASS` 掩盖独立审查缺失。
