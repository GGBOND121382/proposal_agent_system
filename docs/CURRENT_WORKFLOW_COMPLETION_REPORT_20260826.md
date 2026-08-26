# 当前工作流已完成工作说明

## 1. 报告范围与证据口径

本文说明项目 `project-8595abee7b9c4047` 截至 2026-08-26 已完成的工作，覆盖：

- WF-1 `PROJECT_INTAKE`：已完成；
- WF-2 `TEMPLATE_EXTRACTION`：已完成；
- WF-3 `HYBRID_ONLINE_ASSIST`：已完成；
- WF-4 `PROPOSAL_AUTHORING`：阶段 0 的 Argument Architecture Producer 已通过，Argument Critic 因连续输出契约失败按用户决定跳过，工作流当前停在 `P-PROJECT-READINESS-CRITIC` 之前。

本文的“输入”和“输出”以 SQLite 中实际持久化的 Prompt Run、Workflow State、Gate 与检索归档为准，不把 Replay 示例当成实际运行结果。主要证据对象如下：

| 对象 | ID/位置 |
|---|---|
| 项目 | `project-8595abee7b9c4047` |
| 主输入材料 | `doc-46545e6862d5497b`：`PROJECT_BRIEF_人机协同决策优势冲刺_系统适配精简版.md` |
| WF-1 | `wf-e5775eba63c04057`，`COMPLETED` |
| WF-2 | `wf-f1f90c80a90b4bb9`，`COMPLETED` |
| WF-3 | `wf-f29b7e641f4c4987`，`COMPLETED` |
| WF-4 | `wf-dcd0bba4c2eb41f8`，`RUNNING`，当前步骤 2 |
| WF-3 检索归档 | `data/research_archive/project-8595abee7b9c4047/research-49fc253ead2b4a95/` |
| WF-4 阶段 0 Producer Run | `run-2966155732654c69`，`PASS` |

## 2. Prompt 的共同组成

各 Prompt 的完整原文位于 `prompt_pack/prompts/`。虽然不同节点职责不同，但 Prompt 基本由以下元素组成：

1. 元数据：Prompt ID、版本、执行角色、执行环境、模型配置、后续 Gate 和严格 JSON 输出要求。
2. 角色与权限：限定模型只能完成本节点职责，不能修改数据库、批准 Gate、扩大检索范围或把推断升级为确认事实。
3. 必须读取的输入：列出允许读取的 payload 字段；缺字段、Hash 过期、对象版本不一致时应停止。
4. 执行算法：定义抽取、审查、规划、综合或论证构建步骤。
5. 状态判定：`PASS`、`REVISE`、`NEED_USER_INPUT`、`BLOCK` 的使用边界。
6. Finding 代码：要求问题具有严重级别、目标路径、证据、修复指令和路由。
7. 来源与知识状态规则：区分来源事实、公开结论、模型归纳、项目计划和已完成成果。
8. 强制自检：检查来源、限定词、UNKNOWN、越权、敏感信息和 JSON 契约。
9. 输出 Schema：强制输出固定 Envelope，包括 `result`、`findings`、`unresolved_items`、`user_questions`、`source_refs` 和 `warnings`。

后文的“Prompt 要点”是在该共同结构之上，列出每个节点特有的目标和算法。

## 3. WF-1：项目摄取与知识底座

### 3.1 总体结果

WF-1 已形式化完成。它并非所有模型节点均为 `PASS`：部分抽取或准备度节点返回了 `REVISE`/`NEED_USER_INPUT`，随后经人工 Gate 明确确认并作为可接受的阶段产物继续流转。

| 步骤 | Prompt | 实际状态 | 主要产物 |
|---:|---|---|---|
| 0 | `P-SECURITY-CLASSIFY` | `PASS` | 主材料安全等级、敏感字段、组合风险、允许环境 |
| 1 | `P-SECURITY-CLASSIFY-CRITIC` | `PASS` | 安全分类独立复核通过 |
| 2 | `P-SCHEME-EXTRACT` | `REVISE`，Gate 接受 | 申报规则画像、覆盖情况和未决规则 |
| 3 | `P-SCHEME-CRITIC` | `PASS` | 规则来源、数值和遗漏审查 |
| 4 | `P-PROJECT-DEFINITION-EXTRACT` | `NEED_USER_INPUT`，Gate 接受 | 项目定义、Proposal Contract、论证图种子 |
| 5 | `P-PROJECT-DEFINITION-CRITIC` | `NEED_USER_INPUT`，Gate 接受 | 项目对象、关系、论证种子审查 |
| 6 | `P-FACT-EXTRACT` | `PASS` | 23 条最小事实候选 |
| 7 | `P-FACT-CRITIC` | `PASS` | 23 条事实全部接受 |
| 8 | `P-PROJECT-READINESS-CRITIC` | `NEED_USER_INPUT`，Gate 接受 | 可进入论证架构阶段，但尚未达到章节规划准备度 |

### 3.2 各 Prompt 的输入、处理与输出

#### `P-SECURITY-CLASSIFY` 2.0.0

- Prompt 要点：逐段识别敏感实体、参数、场景、身份与关系；评估多个字段组合后产生的推断风险；只能给出建议等级，不能自行降级审批。
- 实际输入：1 个对象上下文、17 个内容分段、安全策略、1 个既有标签和 1 项预期用途。
- 实际输出：主材料建议等级 `INTERNAL`；3 类敏感实体、3 类敏感字段、2 项组合风险、2 个允许环境、8 条判定理由，置信度 `HIGH`。
- 实际结果：`PASS`，无 Finding、无未决项；保留 3 条不阻断警告。
- Prompt 原文：`prompt_pack/prompts/security/security_classify.md`。

#### `P-SECURITY-CLASSIFY-CRITIC` 2.0.0

- Prompt 要点：从原始对象重新识别敏感内容，对比候选标签，检查漏标、错误降级、组合推断和允许环境。
- 实际输入：安全分类候选、原始对象、安全策略和空的确定性 Finding 集合。
- 实际输出：`verdict=ACCEPT`，复核 8 个维度，认可 `INTERNAL` 等级并记录候选 Hash。
- 实际结果：`PASS`，无 Finding、未决项或警告。

#### `P-SCHEME-EXTRACT` 2.0.0

- Prompt 要点：从正式指南/模板中区分强制条款、建议和示例；抽取方向、周期、预算、指标、章节、篇幅、排除范围和合规要求；每项规则必须绑定来源。
- 实际输入：1 份指南对象、3 个文档结构项、1 个抽取范围，无既有 Profile。
- 实际输出：包含 15 个字段的 `scheme_profile`、1 项覆盖记录；未形成歧义规则 ID。
- 实际结果：`REVISE`，2 个 Finding、3 个未决项、3 个用户问题和 5 条警告。
- 后续处理：`PROJECT_GAP_RESOLUTION` Gate `gate-4dc8e1a7a3924a3d` 已 `APPROVED/CONFIRM`，该非 PASS 结果被显式接受。

#### `P-SCHEME-CRITIC` 2.0.0

- Prompt 要点：逐条回查规则来源，检查强制/建议混淆、数值、周期、范围、附件和排除条款。
- 实际输入：规则候选、指南材料和空的确定性 Finding 集合。
- 实际输出：`verdict=ACCEPT`，检查 13 条规则和 2 项数值条件，无遗漏规则候选。
- 实际结果：`PASS`，仍保留 2 个未决项和 3 条警告。
- 后续处理：`SCHEME_CONFIRMATION` Gate `gate-56d740983dd949a7` 已批准。

#### `P-PROJECT-DEFINITION-EXTRACT` 3.0.0

- Prompt 要点：区分科研申请书与工程项目；同时构造项目事实图、Proposal Contract 和 Argument Graph Seed；明确差距、问题、目标、任务、方法、实验、创新、成果、指标、基础、团队和资源；不得把“建设系统/形成原型”直接当作科研命题。
- 实际输入：1 份源文档、15 字段规则画像、1 个抽取范围、安全约束；无既有项目定义。
- 实际输出：11 字段项目定义、8 项抽取覆盖、2 个未映射 Span、11 字段 Proposal Contract 和 6 字段论证图种子。
- 实际结果：`NEED_USER_INPUT`，7 个 Finding、7 个未决项、7 个用户问题和 5 条警告。
- 后续处理：`PROJECT_GAP_RESOLUTION` Gate `gate-d8ebbb20165547e4` 已 `APPROVED/CONFIRM`。

#### `P-PROJECT-DEFINITION-CRITIC` 3.0.0

- Prompt 要点：核对项目对象类型、关系方向、科学问题/技术瓶颈/工程任务区分、参考材料污染、计划与已完成状态。
- 实际输入：项目定义候选、源文档、规则画像、关系矩阵、Proposal Contract 候选、论证图候选和空的确定性 Finding。
- 实际输出：模型给出 `verdict=REVISE`；检查 24 个对象、17 条关系，发现 1 条无效关系，并完成 8 项论证检查。
- 实际结果：运行状态 `NEED_USER_INPUT`，8 个 Finding、5 个未决项、5 个用户问题和 7 条警告。
- 后续处理：`PROJECT_DEFINITION_CONFIRMATION` Gate `gate-bf216738f19a4e76` 已 `APPROVED/CONFIRM`。

#### `P-FACT-EXTRACT` 2.0.0

- Prompt 要点：把来源 Span 拆成单一可判真命题，保留主体、时间、数字、单位、否定和限定词；区分 FACT、PLAN、EXPECTED_RESULT；最多输出 24 条代表性事实。
- 实际输入：16 个来源 Span、权威规则和安全约束；无既有事实和锁定事实。
- 实际输出：23 条事实候选、0 个冲突、10 项覆盖记录。
- 实际结果：`PASS`，无 Finding、未决项或用户问题。

#### `P-FACT-CRITIC` 2.0.0

- Prompt 要点：逐条核对来源，检查计划冒充已完成、主体错配、数字/单位/条件丢失、否定与限定词丢失及锁定事实冲突。
- 实际输入：23 条事实候选、16 个来源 Span、23 条既有事实、权威规则和空锁定集。
- 实际输出：`verdict=ACCEPT`；23 条全部接受，0 条拒绝、0 个冲突、0 个锁定事实违规。
- 实际结果：`PASS`。
- 后续处理：`FACT_CONFIRMATION` Gate `gate-86124cfe30de4576` 已批准。

#### `P-PROJECT-READINESS-CRITIC` 3.0.0

- Prompt 要点：依据准备度矩阵检查完整度、确认度、证据度和冲突，区分可写、带警告可写和阻断；只能提出字段级问题，不能用模型推断填补缺口。
- 实际输入：项目定义、事实包、规则画像、章节 Profile、任务指令、Proposal Contract、论证图；目标阶段为 `READY_FOR_ARGUMENT_ARCHITECTURE`。
- 实际输出：12 个领域评分、11 个章节准备度、4 个可写章节 Profile、7 个阻断章节 Profile、6 个缺失输入和 11 项关键准备度检查。
- 实际判定：`ready_for_argument_architecture=true`、`ready_for_section_planning=false`。
- 实际结果：`NEED_USER_INPUT`，6 个 Finding、5 个未决项、6 个用户问题和 8 条警告。
- 后续处理：`PROJECT_GAP_RESOLUTION` Gate `gate-b1d290fd675240b5` 已 `APPROVED/CONFIRM`，WF-1 随后完成。

## 4. WF-2：参考结构抽取

### 4.1 `P-TEMPLATE-EXTRACT` 3.0.1

- Prompt 要点：只抽取参考材料可复用的论证结构、章节功能、段落角色、图表/公式模式和格式规则；明确剔除参考项目的名称、成果、技术和数字，防止事实污染。
- 实际输入：1 个参考文档对象、17 节章节树、3 字段样式摘要、1 个抽取范围和安全约束。
- 实际输出：8 字段模板对象、12 项覆盖、11 项来源事实排除记录。
- 实际结果：`PASS`，无 Finding、未决项或用户问题。

### 4.2 `P-TEMPLATE-CRITIC` 3.0.0

- Prompt 要点：对照章节树检查模板是否只剩格式、是否过度具体、是否夹带参考项目实体/数字、是否遗漏图表公式模式和论证功能。
- 实际输入：模板候选、参考文档和空的确定性 Finding。
- 实际输出：`verdict=ACCEPT`；检查 11 个模板组件，0 个污染组件，0 个缺失章节功能，完成 5 项逻辑模式检查。
- 实际结果：`PASS`。
- 后续处理：`TEMPLATE_CONFIRMATION` Gate `gate-020a1342158d4d2d` 已批准，WF-2 完成。

## 5. WF-3：混合联网辅助与公开研究

### 5.1 实际流程

WF-3 不是“把内部项目原文直接交给联网模型”。它先在离线环境生成安全包，经独立 Critic 和人工外发 Gate 后，才形成在线研究计划；检索结果先归档，再由在线模型综合和审查，最后回到离线环境执行导入审查。

```text
用户提供公开研究需求
  -> P-SAFE-ONLINE-PACKAGE（离线脱敏与最小化）
  -> P-SAFE-ONLINE-PACKAGE-CRITIC（离线复核）
  -> OUTBOUND_SECURITY_APPROVAL（人工批准外发）
  -> P-PUBLIC-RESEARCH-PLAN（在线公开研究计划）
  -> PUBLIC_SEARCH（连接器检索、抽取、归档、覆盖检查）
  -> P-PUBLIC-RESEARCH-SYNTHESIS（逐来源形成公开 Claim）
  -> P-PUBLIC-RESEARCH-CRITIC（来源质量与支持度审查）
  -> P-ONLINE-RESULT-IMPORT-CRITIC（离线导入审查）
  -> ONLINE_RESULT_IMPORT_APPROVAL（人工批准导入）
```

### 5.2 初始研究需求与人工输入

输入 Gate 收集了四项信息：研究问题、为什么需要联网、期望输出和任务类型。最终研究需求为：

- 问题：检索与人机协同决策优势评估相关的公开研究、代表性方法、比较基线和评价指标，并说明适用边界。
- 联网原因：内部材料无法独立确认相关工作、比较基线和评价依据，需要公开且可核验的资料。
- 期望输出：来源绑定的公开材料、方法/基线、评价指标、适用边界以及可用于申请书论证的摘要。
- 任务类型：`PUBLIC_RESEARCH`。

### 5.3 安全在线任务包

#### `P-SAFE-ONLINE-PACKAGE` 2.0.0

- Prompt 要点：确认任务确需联网；删除项目身份和非必要内部背景；用占位符替换实体；抽象为公共研究问题；生成删除字段清单、禁止推断和禁止输出规则。
- 实际输入：研究需求、2 个内部来源对象、安全策略、任务类型和 4 项人工回答。
- 实际输出：公开级任务包 `pkg-6679954d2dc46370-public`，包含：
  - 4 条初始通用查询；
  - 5 类允许上下文；
  - 3 个实体占位符；
  - 9 个被删除的项目标识/内部字段；
  - 4 条禁止推断；
  - 5 类禁止输出；
  - 安全等级 `PUBLIC`。
- 初始查询：
  1. `human-machine collaborative decision making advantage evaluation methods`
  2. `human-AI team decision performance metrics comparison baseline`
  3. `decision support system evaluation framework metrics academic literature`
  4. `human factors in collaborative decision making assessment`
- 重要边界：公开方法不得直接映射为本项目创新，公开性能不得作为本项目预期成果，公开指标不得直接变成本项目验收标准。

#### `P-SAFE-ONLINE-PACKAGE-CRITIC` 2.0.0

- Prompt 要点：逐项核对禁止字段，测试重识别风险、场景/参数组合推断、任务范围和有效期；只能建议批准，不能替代人工批准。
- 实际输入：安全包候选、2 项来源摘要、安全策略和确定性扫描结果。
- 实际输出：`ACCEPT_FOR_HUMAN_APPROVAL`，重识别风险 `LOW`，核对 9 个禁止字段，无额外脱敏要求。
- 实际结果：`PASS`。
- 人工 Gate：`OUTBOUND_SECURITY_APPROVAL` Gate `gate-c4ad953991eb4a4c` 已 `APPROVED`。

### 5.4 公开研究计划

#### `P-PUBLIC-RESEARCH-PLAN` 2.1.0

- Prompt 要点：严格继承已批准安全包；分解研究问题；查询不得包含项目实体；每条查询必须用稳定 ID 和零起始问题下标显式绑定研究问题；优先一手/权威来源；定义时间范围、证据要求、冲突处理和禁止推断。
- 实际输入：已批准安全包、公开研究任务类型、空的已知公开来源、时间约束和 4 条证据要求。
- 实际输出：4 个研究问题、7 条模型规划查询、7 类来源优先级、时间范围、证据要求和禁止推断。

四个研究问题分别聚焦：

1. 决策优势评估方法及其核心机制/维度；
2. 性能比较基线、典型任务和对比条件；
3. 评价指标类别、测量方式、优缺点和适用条件；
4. 各类方法与指标的适用边界和局限。

模型原始计划提出 7 条查询：

1. `human-machine collaborative decision making advantage evaluation methods academic literature 2019-2025`
2. `human-AI team decision performance metrics comparison baseline experimental design`
3. `decision support system evaluation framework metrics measurement validation`
4. `human factors collaborative decision making assessment trust calibration`
5. `human-agent teaming performance measurement methodology HCOMP CHI AIES`
6. `adaptive automation decision support evaluation metrics systematic review`
7. `situation awareness measurement human-automation collaboration SAGE PANAS`

计划中的来源优先级包括 ACM Digital Library、IEEE Xplore、Nature/Scientific Reports、Frontiers/Human Factors、AI 安全与人机决策会议、NIST/DARPA 报告，以及 Google Scholar/Semantic Scholar 的引文发现。但这只是检索优先级，不代表这些数据库在本次执行中都产生了实际命中。

计划证据要求包括：方法/指标定义必须有原始文献；性能比较应记录实验条件、被试、样本量和统计显著性；局限必须来自原文；优先 2020 年以后综述或元分析。时间范围设为 2019-01-01 至 2025-05-31，并兼顾经典奠基文献。

### 5.5 实际检索执行、失败筛除与重试

第一次检索使用 SearXNG 结果，但返回集合包含撤稿论文、天体物理论文、网络安全论文以及超出计划时间范围的 2025/2026 文献等无关或不合格来源。对应综合结果 `run-ef65772880274b45` 为 `REVISE`，Critic `run-de8115e860304095` 为 `BLOCK`。这些结果没有进入最终公开 Claim 集，已被存入 `superseded_public_search_results`，替换原因明确记录为：

> critic rejected withdrawn/irrelevant SearXNG results; operator-verified connector retry

随后进行 1 次经操作员核验的连接器重试：

- 模式：`VERIFIED_CONNECTOR_ARCHIVE`；
- 连接器输入：`data/operator_inputs/wf-f29b7e641f4c4987-research-retry.json`；
- 实际执行查询：5 条；
- 最终来源：10 个；
- 每条查询固定覆盖 2 个经核验来源；
- 归档模式：`LIVE_CONNECTOR_ARCHIVE`；
- Provider：`connector`；
- Query failure：0；Warning：0。

实际执行的 5 条查询是：

| Query ID | 实际查询 | 绑定研究问题 | 命中数 |
|---|---|---:|---:|
| `retry-q01` | `human-AI collaboration evaluation framework metrics` | 0 | 2 |
| `retry-q02` | `human-AI decision making appropriate reliance measurement` | 1 | 2 |
| `retry-q03` | `human-AI team complementarity and comparison baselines` | 2 | 2 |
| `retry-q04` | `human-AI teaming surveys and experimental study design` | 3 | 2 |
| `retry-q05` | `human-AI team trust alignment and real-time benchmark` | 3 | 2 |

这 5 条查询是最终执行计划；前述 7 条是模型生成的原始计划。执行侧保留 4 个研究问题不变，并用 `linked_question_indexes` 显式记录查询与问题的绑定。

### 5.6 实际数据源

本次最终 10 个来源全部来自 `arxiv.org`，归类为学术仓储来源。计划中列出的 ACM、IEEE、NIST/DARPA 等来源未在最终来源目录中出现，因此不能表述为“已逐一检索并采用”。

| 来源 ID | 标题 | 年份 | Authority | 匹配查询 | 主要用途 |
|---|---|---:|---:|---|---|
| `public-src-complementarity-2024` | [Complementarity in Human-AI Collaboration: Concept, Sources, and Evidence](https://arxiv.org/abs/2404.00029) | 2024 | 88 | complementarity/baselines | 互补性绩效、信息/能力不对称、三类对比条件 |
| `public-src-ai-delegation-2023` | [Human-AI Collaboration: The Effect of AI Delegation on Human Task Performance and Task Satisfaction](https://arxiv.org/abs/2303.09224) | 2023 | 86 | complementarity/baselines | 196 人委托式协作实验、绩效/满意度/自我效能 |
| `public-src-information-asymmetry-2022` | [On the Effect of Information Asymmetry in Human-AI Teams](https://arxiv.org/abs/2205.01467) | 2022 | 86 | trust/alignment/benchmark | 信息与能力不对称对团队表现的影响 |
| `public-src-human-calibration-2023` | [Human-Aligned Calibration for AI-Assisted Decision Making](https://arxiv.org/abs/2306.00074) | 2023 | 85 | appropriate reliance | 置信度对齐与依赖策略 |
| `public-src-appropriate-reliance-2022` | [Should I Follow AI-based Advice? Measuring Appropriate Reliance in Human-AI Decision-Making](https://arxiv.org/abs/2204.06916) | 2022 | 85 | appropriate reliance | 正确采纳/自主决策的二维测量 |
| `public-src-hai-survey-2021` | [Towards a Science of Human-AI Decision Making: A Survey of Empirical Studies](https://arxiv.org/abs/2112.11471) | 2021 | 85 | evaluation framework | 百余项实证研究的设计与指标分类、可比性缺口 |
| `public-src-agent-alignment-2024` | [Designing for Human-Agent Alignment: Understanding what humans want from their agents](https://arxiv.org/abs/2404.04289) | 2024 | 84 | trust/alignment/benchmark | 偏好对齐、交互质量及其与客观绩效的组合 |
| `public-src-crew-benchmark-2024` | [CREW: Facilitating Human-AI Teaming Research](https://arxiv.org/abs/2408.00170) | 2024 | 84 | surveys/experiment | 实时人机协作平台、认知任务、生理信号、RL 基线 |
| `public-src-haic-framework-2024` | [Evaluating Human-AI Collaboration: A Review and Methodological Framework](https://arxiv.org/abs/2407.19098) | 2024 | 82 | evaluation framework | 三类协作模式下的定量/定性指标选择框架 |
| `public-src-hai-lptm-survey-2024` | [A Survey on Human-AI Teaming with Large Pre-Trained Models](https://arxiv.org/abs/2403.04931) | 2024 | 80 | surveys/experiment | 大模型人机协作机制、伦理和适用范围综述 |

### 5.7 信息抽取、归档与确定性校验

连接器对每个来源保留以下字段：

- `source_id`、标题、作者、发布日期、Publisher、URL/Canonical URL；
- 实际匹配查询、检索时间、连接器运行标识；
- 原始快照路径、抽取文本路径、元数据路径；
- `snapshot_sha256`、`text_sha256`、字节数、文本长度；
- Authority Rank、是否属于近期工作、是否支持基线/局限；
- 一个短摘录以及与查询的 relevance 标签。

归档目录分层如下：

| 目录/文件 | 内容 |
|---|---|
| `connector/connector_response.json` | 连接器原始响应 |
| `raw/*.json` | 每个来源的原始快照 |
| `text/*.txt` | 从来源抽取的纯文本 |
| `metadata/*.json` | 标准化元数据 |
| `source_index.csv` | 来源总索引 |
| `manifest.json` | 会话、查询、来源、Hash、覆盖和验证总清单 |
| `claim_bindings/*.json` | Claim 到来源及证据层的绑定报告 |

本次每个来源只形成了一个较短摘录，抽取文本约 240～344 个字符，并不是论文全文。因而它适合支撑概念级归纳和来源发现，不足以替代对论文方法、量表、实验细节和统计结果的全文审阅。

确定性覆盖检查结果为 `PASS`：

- 5 条执行查询均有 2 个权威来源；
- `uncovered_queries=[]`；
- “近期工作”覆盖 10 个来源；
- “可比较基线”覆盖 6 个来源；
- “局限机制”覆盖 2 个综述来源；
- 归档校验 `PASS`，10 个来源 Hash 全部通过。

### 5.8 综合与信息归类

#### `P-PUBLIC-RESEARCH-SYNTHESIS` 2.0.0

- Prompt 要点：只能使用提供的检索来源；逐 Claim 绑定来源；区分事实、观点和推断；并列呈现来源分歧；声明适用范围和时效。
- 实际输入：研究计划、10 个来源、10 个抽取 Passage、安全在线任务包。
- 实际输出：10 个 `PUBLIC_CLAIM`、3 个跨来源比较、0 个冲突、5 项局限和 1 段覆盖摘要。
- 实际结果：`PASS`；保留 2 个非阻断未决项。

10 个 Claim 的归类如下：

| Claim | 类别 | 核心内容 | 直接来源 |
|---|---|---|---|
| `claim-001` | 协同优势定义 | 互补性团队绩效；信息/能力不对称产生互补 | complementarity-2024 |
| `claim-002` | 依赖行为测量 | 正确建议时采纳、错误建议时自主决策；二维测量 | appropriate-reliance-2022 |
| `claim-003` | 置信校准 | AI 置信值与决策者置信度对齐，可帮助发现依赖策略 | human-calibration-2023 |
| `claim-004` | 实验基线 | 196 人委托式人机协作及替代任务分配对比 | ai-delegation-2023 |
| `claim-005` | 优势形成条件 | 信息与能力不对称影响团队表现和任务分配 | information-asymmetry-2022 |
| `claim-006` | 评价框架 | AI 中心、人类中心、共生三种协作模式的指标选择 | haic-framework-2024 |
| `claim-007` | 实验平台/基准 | CREW 的认知任务、生理信号和人类引导 RL 基线 | crew-benchmark-2024 |
| `claim-008` | 综述与研究设计 | 百余项实证研究的任务、模型、辅助元素、指标分类 | hai-survey-2021 |
| `claim-009` | 对齐评价 | 偏好对齐和交互质量必须与客观绩效联合使用 | agent-alignment-2024 |
| `claim-010` | 范围综述 | 大模型人机协作机制、有效协作、伦理和领域边界 | hai-lptm-survey-2024 |

跨来源比较形成三类归纳：

1. 测量维度：适当依赖、置信校准、偏好对齐分别覆盖行为、认知和偏好层面，单一维度不足以完整测量决策优势。
2. 基线标准化：具体实验和 CREW 提供了基线设计，但跨研究统一标准仍然不足。
3. 适用边界：多个综述一致认为不存在跨领域通用单一指标，应按任务和应用情境组合评价。

局限被归为：实验可比性/泛化性不足、单一维度不充分、指标需适配领域、LPTM 综述不能提供单一验证指标、信息/能力不对称的量化尚未统一。

两个非阻断未决项是：

- 尚未抽取具体任务场景分类及各场景典型指标；
- 尚未抽取具体量表名称、测量频次等工具细节。

#### `P-PUBLIC-RESEARCH-CRITIC` 2.0.0

- Prompt 要点：核验来源权威和时效、Claim 是否被来源直接支持、是否遗漏反证、是否超出安全包范围，并给出导入建议。
- 实际输入：研究计划、综合候选、10 个来源和安全在线任务包。
- 实际输出：`ACCEPT_FOR_IMPORT_REVIEW`；10 个来源逐项评级，其中 7 个 `HIGH`、3 个 `MEDIUM`；无不受支持 Claim，无缺失反证主题。
- 实际结果：`PASS`。
- 主要警告：部分实验任务/指标细节未展开，系统性综述比例有限；当时模型输出认为来源元数据不完整，但后续归档清单实际保留了标题、作者、日期和 URL。

### 5.9 离线导入审查

#### `P-ONLINE-RESULT-IMPORT-CRITIC` 2.0.0

- Prompt 要点：回到离线环境，核对 Manifest、批准任务 Hash、Prompt 注入、范围越界、每个 Claim 的公开来源以及是否推断内部项目；将内容分为可导入、拒绝或需用户确认。
- 实际输入：已批准安全包、10 个 Claim 的结果包、10 个公开来源、Transfer Manifest 和安全策略。
- 实际输出：`IMPORT_PUBLIC_CLAIM_CANDIDATES`；`claim-001` 至 `claim-010` 全部接受，0 个拒绝；未检测到 Prompt 注入或范围越界，无需额外人工确认。
- 确定性 Claim 绑定检查：10 个 Claim 均为 `DIRECT_SOURCE_SUPPORTED`，每个 Claim 都具备 `ORIGINAL_SNAPSHOT -> SOURCE_EXTRACT -> MODEL_SYNTHESIS` 三层证据链。
- 人工 Gate：`ONLINE_RESULT_IMPORT_APPROVAL` Gate `gate-e93bf21af2434033` 已 `APPROVED`，WF-3 完成。

## 6. WF-4：已完成的阶段 0

### 6.1 `P-ARGUMENT-ARCHITECTURE` 9.0.0

- Prompt 要点：从 Proposal Contract、项目子图、确认事实、论证图种子、模板上下文和当前章节构造论证架构；中心命题必须可比较/可证伪；每条研究线程应形成“局限机制 -> 缺口 -> 问题 -> 目标 -> 工作包 -> 方法 -> 评价 -> 创新/贡献”的闭环；公开 Claim 只能作为文献依据，不能冒充项目成果。
- 实际输入：
  - 11 字段 Proposal Contract；
  - 4 字段项目子图；
  - 33 条确认事实，其中包含 WF-3 导入的 10 个公开来源；
  - 6 字段论证图种子；
  - 8 字段模板上下文；
  - 16 个当前章节对象；
  - 11 字段任务指令；
  - 4 条 Revision Finding；
  - 无人工补充回答。
- 实际输出：
  - Authoritative Authored State；
  - 4 条研究线程；
  - 127 个论证节点和 119 条关系；
  - 4 行研究设计矩阵；
  - 5 个非阻断证据缺口；
  - 正文/附录/排除范围决策；
  - 132 条论证对象—证据绑定；
  - 4 组线程假设；
  - `readiness.ready=true`，无阻断节点。

中心命题将当前项目限定为纯调研性质：围绕决策功能与证据模型、人员主导协同分工、任务—能力—资源候选与解释、多维评价与失效边界四个主题形成概念框架、方法分类、证据要求、比较基线和评价框架；不交付原型、平台或仿真实验。

四条研究线程分别是：

1. 决策功能分解与统一数据/证据底座；
2. 人员主导的多智能体动态分工与短周期冲刺机制；
3. 任务—能力—资源候选生成与可解释偏序排序；
4. 六类指标联合评价、失效边界与可信性保障。

每条线程包含 1 个工作包、2 个方法、2 个评价方案、2 个比较基线和 1 个创新点；全局共形成 8 个方法、8 个评价节点和 8 个基线节点。论证图还包括 28 个假设、12 个理论属性、4 个消融节点、26 个成功标准、9 个最近工作节点和 4 个贡献节点。

WF-3 的公开 Claim 被实际用于阶段 0：阶段 0 输入包含 10 条 `PUBLIC_SOURCE`，输出中产生 99 次公开来源引用。它们主要支持线程 2～4 的互补性、适当依赖、置信校准、实验基线、评价框架、CREW 平台、综述和适用边界。

阶段 0 保留的 5 个非阻断缺口主要是：

- 团队基础、已有算法/软件、数据、平台、人员、合作、经费和算力材料不足；
- 周期、经费规模、申报单位、负责人、团队分工、正式指南和成果数量等项目事实待补；
- 任务—能力—资源方法分类在理由、不确定性、冲突和资源缺口联合表达方面的公开归纳仍不足；
- 正式指标阈值、统计检验、样本量、基线测量和验收阈值仍为 UNKNOWN；
- 团队与平台类基础证据仍待项目方补充。

这些缺口全部 `blocking=false`，因此 Producer 最终为 `PASS`。

### 6.2 Argument Critic 的当前处理

`P-ARGUMENT-ARCHITECTURE-CRITIC` 连续 6 次调用均未形成符合契约的有效审查对象，错误包括非法 JSON、不存在的 Evidence ID、Review Unit 与 Component 不一致、Dimension 与失败质量维度不一致等。没有任何一次有效 Critic 结果被伪造为 `PASS`。

按用户决定，当前工作流只对该 Critic 做实例级跳过，记录为：

- `DETERMINISTIC_PASS_CRITIC_REVIEW_UNAVAILABLE`；
- Producer Run：`run-2966155732654c69`，真实 `PASS`；
- Critic：`SKIPPED_BY_USER_AFTER_CONTRACT_FAILURES`；
- 当前 WF-4：`RUNNING`，步骤 2；
- 下一 Prompt：`P-PROJECT-READINESS-CRITIC`；
- 尚未触发下一次模型调用。

Critic 的轻量化改造、失败降级语义、请求长度预算和历史失败回归测试已记录在 `docs/TODO_STAGE0_CONTRACT_BOUNDARIES_20260821.md`。

## 7. 当前可用资产与后续边界

目前已经具备：

- 经安全分类和人工确认的主输入材料；
- 申报规则画像、项目定义、事实包和论证图种子；
- 经污染检查的可复用结构模板；
- 10 个已批准导入的公开 Claim 和完整的来源归档/Hash/绑定链；
- 4 条研究线程构成的阶段 0 论证架构、研究设计矩阵和证据缺口清单。

当前不需要为了继续 WF-4 再做一次通用联网搜索。WF-4 没有 `PUBLIC_SEARCH` 节点，它消费 WF-3 已导入的公开证据。只有后续出现一个明确、可公开检索且影响写作的具体缺口时，才应回到 WF-3 做小范围补充检索，而不是由 WF-4 自行联网或重复全量检索。

需要保留的解释边界：

- WF-1 的 `COMPLETED` 包含人工接受的 `REVISE/NEED_USER_INPUT` 结果，并不等价于每个模型节点都为 `PASS`。
- WF-3 最终检索集是操作员核验后的连接器重试结果；第一次 SearXNG 集合因撤稿/无关来源被 Critic 阻断并替换。
- WF-3 的实际采用来源全部是 arXiv，不能把计划中的 ACM、IEEE、NIST/DARPA 等优先级描述成实际采用来源。
- 当前抽取层主要是短摘要/短摘录，不是论文全文；若正文需要量表、统计方法、实验过程或精确阈值，仍需专项全文核验。
- 安全包中的 `valid_until=2025-02-15T23:59:59Z` 早于本次 2026 年工作流运行时间，研究计划的资料时间范围也截至 2025-05-31。这是当前历史产物的时效性限制；在未来重新外发或补搜前应重新生成并审批安全包，而不能直接复用该过期字段。
- WF-4 Argument Critic 被跳过，因此阶段 0 是“Producer + 确定性校验通过、语义 Critic 不可用”的临时完成状态，不是独立语义复核通过。
