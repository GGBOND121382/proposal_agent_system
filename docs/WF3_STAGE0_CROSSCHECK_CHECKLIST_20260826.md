# WF-3 对照 WF-4 Stage 0 问题检查清单

## 1. 目的与边界

本文用于在优化 `WF-3_HYBRID_ONLINE_ASSIST` 前，逐项核对 2026-08 的 WF-4 Stage 0 修复过程中已经暴露的问题是否也存在于 WF-3。

检查依据：

- 当前对话中记录的 Stage 0 历史故障；
- `docs/TODO_STAGE0_CONTRACT_BOUNDARIES_20260821.md`；
- `tests/test_stage0_historical_regression_inventory.py` 中登记的历史回归项；
- 当前 WF-3 实现、Prompt、输入输出 Schema 与测试；
- 项目 `project-8595abee7b9c4047` 的实际 WF-3 `wf-f29b7e641f4c4987` 请求、响应、数据库记录和检索归档。

本轮只做审计和建立检查清单：

- 不修改任何 schema；
- 不修改 WF-3 运行代码或 Prompt；
- 不重跑工作流或调用模型；
- 后续优化必须先补测试，再做小范围修改；
- WF-4 特有的论证图、研究矩阵和双阶段组装问题，不机械移植到 WF-3。

## 2. 状态说明

| 状态 | 含义 |
|---|---|
| `存在` | 当前代码或实际 WF-3 请求/响应已经证明存在同类问题 |
| `部分存在` | 共享运行时已有缓解，但 WF-3 仍有未覆盖分支或缺少专项回归 |
| `待实测` | 静态检查无法证明当前版本是否完全解决，需离线回放或一次受控运行 |
| `不适用` | 仅属于 WF-4 Stage 0 特有数据结构或流程 |

## 3. 审计结论摘要

| 类别 | WF-3 结论 | 优先级 | 核心证据 |
|---|---|---:|---|
| 模型生成机械字段、ID、Hash 和控制字段 | `存在` | P0 | Plan、Synthesis、Critic、Import Critic 的输出仍要求模型生成或回显多类 ID、类型、布尔控制字段；历史 Synthesis 请求还包含完整来源 Hash |
| `blocking`、Finding 与最终状态不一致 | `存在` | P0 | `run-ef65772880274b45` 返回 `REVISE`，4 个 Finding 的 `blocking` 全为 `false`；当前通用流程仍会把该结果置为 `BLOCKED_CONTENT` |
| Critic 审查范围与修复路由不闭合 | `存在` | P0 | `run-de8115e860304095` 要求 Synthesis Producer 删除 `retrieved_sources`、重新检索全文；这些对象不属于 Synthesis 的可写输出 |
| 全量/跨轮结果缺少明确非退化基线 | `存在` | P0 | 初次 14 个来源被操作员连接器结果整体替换为 10 个来源；当前 WF-3 没有“候选集合不得缩水/覆盖维度不得退化”的统一验收 |
| Prompt 体量和重复上下文 | `存在` | P1 | 历史实际 provider 请求约 47–97 KB；Synthesis/ Critic/Import 重复携带来源、摘录、计划和完整输出契约，当前没有 WF-3 请求长度预算回归 |
| 模型输出空值、缺字段、错位字段、非法引用 | `部分存在` | P0 | 共享 Schema/引用校验可拦截一部分；WF-3 没有使用历史真实失败响应覆盖字符串 `"null"`、对象残缺、字段错位和合法形状下错误引用的专项测试 |
| 人工 Gate 的字符串到类型转换、控件容量和复合布尔问题 | `部分存在` | P1 | 共享 Gate 层已有确定性转换与控件扩容；WF-3 有输入 Gate 和两个审批 Gate，但缺少覆盖其全部实际 Gate 类型的端到端回归 |
| 失败请求/响应完整落盘 | `部分存在` | P0 | MiniMax/Audited Gateway 的共享层已有原始失败响应落盘测试；缺少 WF-3 六个模型节点的故障注入回归与证据包验收 |
| 重试必须携带精确反馈且不能使用失败候选污染基线 | `部分存在` | P0 | provider 重试有共享持久化逻辑；WF-3 的语义 `REVISE`、Critic 修复和检索重跑没有完整的历史响应回放证明 |
| WF-4 双阶段 Skeleton/Design、线程索引重基和图闭环 | `不适用` | — | WF-3 没有 Argument Skeleton/Design、论证图或研究设计矩阵 |

结论：优化 WF-3 前不能只调整检索数量或查询词。至少应先收口四个 P0 边界：字段归属、状态不变量、Critic 路由、跨轮非退化。

## 4. 逐项对照检查

### 4.1 模型字段与运行时字段的归属

Stage 0 历史问题：`question_type`、`target_area`、ID、索引、Hash、状态、路由、`blocking` 等确定性字段被反复要求模型生成，既增加 Prompt 体量，也制造无业务价值的契约失败。

WF-3 当前情况：`存在`。

具体表现：

- `P-PUBLIC-RESEARCH-PLAN` 要求模型生成 `plan_id`、`query_id`、`linked_question_indexes`、`task_type` 和 `binding_contract_version`；其中 ID、固定版本和任务类型均可由输入或运行时确定，查询到研究问题的关联含有语义成分，但不应依赖脆弱的裸数组下标作为长期身份。
- `P-PUBLIC-RESEARCH-SYNTHESIS` 要求模型生成 `claim_id`、`source_refs` 中的 `source_hash`、`authority_rank`、`security_level`、Span 和来源类型。当前共享来源绑定器会重新绑定可信来源字段，历史成功响应中已经出现 `SYSTEM_TRUSTED_SOURCE_REF_NORMALIZATION`，说明模型确实在重复生成运行时随后覆盖的字段。
- `P-PUBLIC-RESEARCH-CRITIC` 要求模型逐项回显 `source_id`，同时生成质量等级、Finding 的路由、`blocking` 和修复控制字段。
- `P-ONLINE-RESULT-IMPORT-CRITIC` 要求模型回显全部 accepted/rejected Claim ID，并生成 `prompt_injection_detected`、`scope_violation_detected` 和确认 ID 列表；其中语义判断与机械分区、ID 回填尚未分层。
- `findings`、`unresolved_items`、`user_questions` 的 ID、路径、答案类型、优先级、路由和阻断属性仍主要由模型一次性生成，不具备 Stage 0 Argument Producer 已实现的“语义内容与机械元数据分离”。

检查项：

- [ ] 为 WF-3 七个执行步骤（六个模型节点和一个 Public Search 技能）分别建立字段归属表：`MODEL_SEMANTIC`、`RUNTIME_DERIVED`、`INPUT_COPIED`、`GUARD_CONTROLLED`。
- [ ] 固定协议版本、Prompt 身份、工作流身份、对象 ID、Hash、来源元数据和 Gate 控制字段不得让模型计算或猜测。
- [ ] 模型只选择语义关系时，运行时负责把选择结果映射回 canonical ID；不得让模型回显整份机器目录。
- [ ] 查询与研究问题的关联必须有稳定身份；如暂时保留数组下标，应验证研究问题顺序冻结且重试不可重排。
- [ ] 任何字段归属优化不得修改 schema；应在模型投影、输出规范化和确定性构造层完成。

验收标准：

- 当前输出 Schema 保持不变；
- 模型可见请求中不再出现无推理价值的 Hash 和工作流内部 ID；
- 删除一个模型生成的机械字段不会丢失业务信息，运行时能确定性恢复 canonical 输出；
- 相同语义响应重复执行时，运行时生成的 ID、类型和路由稳定一致。

### 4.2 Prompt 体量、重复上下文与完整 Schema

Stage 0 历史问题：请求包含大量 Hash、内部 ID、重复上下文和完整旧对象；所谓“修复”请求甚至比第一次请求更长。

WF-3 当前情况：`存在`，但当前版本相对历史版本已有共享投影缓解，仍需重新量化。

历史实际 provider 请求文件大小：

| Run | Prompt | 请求文件大小 |
|---|---|---:|
| `run-65a65c0e963a425e` | Research Plan | 47,306 bytes |
| `run-c6168c0320354a95` | Research Synthesis | 83,649 bytes |
| `run-9adaf66163eb4db6` | Research Critic | 80,635 bytes |
| `run-ca78064053684e54` | Import Critic | 91,778 bytes |
| `run-de8115e860304095` | 首轮 Research Critic | 96,880 bytes |

历史 Synthesis 请求同时携带：完整研究计划、10 个 `retrieved_sources`、10 个重复摘录的 `extracted_passages`、每个来源的 ID/Hash/权限字段、共享规则、字段归属枚举和完整输出 Schema。当前 `app/executor.py::_prepare_provider_envelope` 会剥离多数 Hash 与少量运行时 ID，但没有 WF-3 专项预算测试，且来源与 passage 的正文重复仍可能存在。

检查项：

- [ ] 对当前代码生成的六个 WF-3 模型请求分别记录验证上下文字符数、provider 可见字符数、system Prompt 字符数和预计输入 token；另行记录 Public Search 技能输入大小。
- [ ] 分别设置 Plan、Synthesis、Research Critic、Import Critic 的请求预算，而不是只设置全局上限。
- [ ] 检查 `retrieved_sources` 与 `extracted_passages` 是否重复携带同一段文本；模型只保留一次业务文本和最小来源句柄。
- [ ] 检查 Research Critic 是否需要完整复制 Synthesis 的全部来源元数据；机械引用校验由运行时完成。
- [ ] 检查 Import Critic 是否需要再次携带已经通过公开研究 Critic 的全部自然语言内容；安全检查使用最小必要内容和可信 manifest。
- [ ] 禁止在修复/重试请求中附加完整失败响应、完整旧对象和完整新任务三份重复内容。
- [ ] 增加请求长度回归断言；历史请求作为上限基线，优化后不得反向膨胀。

验收标准：模型可见请求体积可解释、可测量、有固定预算；运行时验证仍使用完整可信上下文。

### 4.3 `blocking`、Finding、Unresolved Item 与最终状态

Stage 0 历史问题：非阻断建议触发 `REVISE`，或所有 `blocking=false` 但问题未展示、流程仍返工；状态、Finding 和 Gate 之间语义不闭合。

WF-3 当前情况：`存在`，已有真实响应证据。

- `run-ef65772880274b45` 的顶层状态为 `REVISE`。
- 该响应包含 4 个 Finding，`blocking` 全部为 `false`，严重级别为 P0/P1/P1/P2。
- 当前 `app/workflows.py` 在所有机器修复/重生成路径不适用后，会把仍为 `REVISE` 的节点置为 `BLOCKED_CONTENT`；它不会根据“全部 Finding 非阻断”自动降为 PASS。
- Prompt 写的是“P0/P1 必须影响 status”，但同时又允许这些 Finding 输出 `blocking=false`。严重级别和阻断性没有唯一决策规则。
- 当前最终成功 Synthesis 保留 2 个 `blocking=false` 的 unresolved item 并 PASS，这一分支是合理的；缺少统一规则保证其他响应也这样处理。

检查项：

- [ ] 定义唯一状态真值表：只有可信的阻断项可触发 `REVISE`/`BLOCK`/`NEED_USER_INPUT`；advisory 只能进入 warning 或非阻断 unresolved/finding。
- [ ] 明确 P0/P1 与 `blocking` 的关系；禁止出现“P0 但 non-blocking，同时顶层 REVISE”的矛盾状态。
- [ ] 状态由运行时根据 canonical 问题集合计算；模型状态只作为候选，不作为最终控制信号。
- [ ] `NEED_USER_INPUT` 必须具有至少一个具体、可回答且 `blocking=true` 的问题。
- [ ] `REVISE` 必须至少有一个可执行修复项，且路由目标具备修改对应对象的能力。
- [ ] advisory 不得消耗语义重试预算，不得创建空 Gate，不得阻止 WF-3 导入流程。

验收用例：

- [ ] 回放 `run-ef65772880274b45`，验证不会仅因 4 个 `blocking=false` Finding 进入返工或内容阻断。
- [ ] 构造 P1/P2 advisory 与一个 P0 blocking 混合场景，验证只有 blocking 项参与状态计算。
- [ ] 构造 `status=PASS` 但存在 blocking Finding 的响应，验证确定性拒绝。
- [ ] 构造 `status=REVISE` 但无 blocking/可执行 Finding 的响应，验证确定性规范化或契约失败，不能含糊流转。

### 4.4 Critic 职责、机械复核与修复路由

Stage 0 历史问题：Critic 被要求重复机械校验、回显大量 ID，并生成容易自相矛盾的 review unit、dimension、evidence ID 和路由；连续契约失败会推翻已经通过确定性校验的 Producer。

WF-3 当前情况：`存在`。

真实证据 `run-de8115e860304095`：

- Critic 正确发现撤稿和无关来源，但两个阻断 Finding 的目标是 `payload.retrieved_sources[...]`；
- Critic 的 `suggested_route=ORIGINAL_PRODUCER`，而 `CRITIC_PRODUCER` 将它映射到 `P-PUBLIC-RESEARCH-SYNTHESIS`；
- Synthesis Producer 只能修改综合结果，不能删除检索归档来源、重写搜索计划或重新执行检索；
- 其他 Finding 又要求“重新检索 ACM/IEEE/AIES/HCOMP”“获取全文”，同样超出 Synthesis Producer 权限；
- 因此 Finding 虽有路径和修复说明，却不是可执行的闭环修复协议。

当前 `P-PUBLIC-RESEARCH-CRITIC` 还要求模型：

- 对每个来源回显 `source_id` 和质量等级；
- 判断所有 claim ID 是否受支持；
- 生成 Finding 的 severity、category、target、repairable、route、blocking；
- 同时检查来源完整性、Hash/引用、范围、安全、时效和反证。

其中来源存在性、Hash、一致性、时间范围、查询覆盖、Claim—Source 引用和 manifest 完整性已有确定性代码，应与主观语义审查分离。

检查项：

- [ ] 列出 Research Critic 的纯语义职责：来源与论断是否实质支持、是否过度概括、是否缺关键反证、研究问题是否真正被回答。
- [ ] 将来源 ID 存在性、Hash、年份范围、重复项、查询覆盖、Claim 引用、manifest 和安全标签移到确定性报告。
- [ ] Critic 只引用运行时提供的简短稳定 review key；最终 Finding 的 canonical 路径、路由、阻断性由运行时构造。
- [ ] 为每类 Finding 建立能力路由：`RETRIEVAL`、`PLAN`、`SYNTHESIS`、`USER`、`BLOCK`，禁止一律路由 Synthesis Producer。
- [ ] 检索结果有问题时，必须回到 Search/Plan 边界；Synthesis 只能删除或收缩自身 Claim，不能伪装成已重新检索。
- [ ] Critic 自身输出契约失败时记录 `REVIEW_UNAVAILABLE`/告警；不得把已经通过确定性来源与 Claim 绑定校验的结果伪造为 PASS，也不得无条件推翻 Producer。
- [ ] Critic 的 PASS 应表示“语义审查通过”，不能被解释成“文献已经充分饱和”。文献覆盖充分性必须由独立确定性 coverage gate 给出。

验收用例：

- [ ] 回放 `run-de8115e860304095`，验证撤稿/无关来源被路由到 Retrieval，而非 Synthesis 定向修复。
- [ ] 回放最终 `run-9adaf66163eb4db6`，验证 Critic PASS 不会覆盖 coverage gate 对来源数量、全文比例和来源多样性的不足判断。
- [ ] 构造不存在的 source ID、claim ID、错误 Hash 和越界年份，验证无需调用 Critic 即可确定性报错。
- [ ] 构造来源真实但论断过度概括的场景，验证只有该语义问题进入 Critic。

### 4.5 跨轮重试、基线保留和非退化

Stage 0 历史问题：一次相对完整的候选在 `REVISE` 后被整轮重生成结果覆盖；新结果通过删除对象减少表面问题，却从 6 个缺口退化到 18 个缺口。后续修复要求：失败候选不得污染已接受基线，新候选只有严格改善才能替换。

WF-3 当前情况：`存在`，但表现形式与 WF-4 不完全相同。

- 初次检索有 14 个来源，最终操作员连接器结果整体替换为 10 个来源；旧结果被存到 `superseded_public_search_results`，这是审计留痕，不是自动非退化验收。
- 新结果质量更高，但数量、查询集合和来源范围都发生变化：原 7 个查询变为 5 个查询，最终覆盖检查只针对新的 5 个查询。
- 当前没有规则要求新候选至少保持原研究问题覆盖、原查询覆盖、来源类型多样性、全文比例、基线/局限/反证覆盖和可用 Claim 数量。
- `P-PUBLIC-RESEARCH-CRITIC` 的一般修复路径使用通用 Targeted Repair；历史 Critic Finding 的目标却可能位于检索输入而非 Synthesis 输出，基线和写入范围不闭合。

检查项：

- [ ] 为 Plan、Search Result、Synthesis 三类对象分别保存精确 accepted baseline，不得只按最新时间选择。
- [ ] provider/结构失败重试不得把失败响应当下一轮基线。
- [ ] Plan 重生成不得静默减少研究问题或使已有查询失去绑定。
- [ ] Search 重跑不得只因结果更少、更短而通过；至少比较研究问题覆盖、查询覆盖、来源质量、多样性、全文可用性和撤稿/无关项数量。
- [ ] Synthesis 重生成不得减少已验证 Claim、丢失来源绑定、局限、冲突或原本已覆盖的研究问题，除非明确删除了已证明无效的内容并留下原因。
- [ ] 新候选未严格改善时保留 accepted baseline，并把新候选作为 rejected candidate 留档。
- [ ] 不做复杂、通用的任意 JSON 融合；优先使用“完整候选 + 确定性非退化验收 + 基线回退”。

验收用例：

- [ ] 以首轮 14 来源和最终 10 来源做历史回放，验证质量改善可被识别，同时 7→5 查询的覆盖变化不能被静默忽略。
- [ ] 构造来源数减少且丢失一个研究问题覆盖的候选，验证拒绝替换。
- [ ] 构造删除撤稿来源、保留其余覆盖且提高全文比例的候选，验证允许替换。
- [ ] 构造 Synthesis Claim 数缩水但 Finding 数也减少的候选，验证不能只按 Finding 数判断改善。

### 4.6 字符串 `"null"`、残缺对象、空响应和字段错位

Stage 0 历史问题包括：

- `cannot_proceed_reason` 输出为字符串 `"null"`；
- 空流响应、非 JSON 对象、JSON 截断；
- 数组中对象残缺；
- 本属于 `success_criteria` 的内容被放进 `evidence_ids`；
- 缺失对象被直接判死，或错误修复时凭空补默认值；
- 合法形状下的错误 ID/引用逃过 Schema 检查。

WF-3 当前情况：`部分存在`。

- WF-3 没有 `cannot_proceed_reason`，因此该字段本身不适用；但 `subject_id`、`document_version_id`、`section_id`、Span、`time_scope` 等含可空字段，精确字符串 `"null"` 仍可能成为合法字符串或触发契约失败。
- 共享 JSON parser、Schema validator、可信来源绑定器和引用校验器可以拦截一部分错误。
- `validate_public_claims` 能检测重复 Claim ID、未知来源、Hash 错误、缺证据和未知 comparison source。
- 但当前历史回归清单只登记 Stage 0 响应，未登记 WF-3 的真实请求/响应；WF-3 Replay 的 `schema_error` 样例不能替代本项目实际失败数据。
- 历史首轮 Synthesis 曾出现 `subject_id` 与 `source_refs.source_id` 不一致、来源 ID 拼写差异等问题，说明“形状合法但语义引用错误”必须保留专项测试。

检查项：

- [ ] 对 WF-3 所有 nullable 字段测试 JSON `null`、字符串 `"null"`、空字符串和缺字段四种情况。
- [ ] 仅对语义上等价且确定的精确字符串 `"null"` 做局部规范化；不得把普通文本中的 `null` 子串改写为空值。
- [ ] 空响应、非对象响应、截断 JSON 必须保留完整/部分原始响应并走有界 provider 重试。
- [ ] 残缺数组行不得凭空补语义内容；确定性字段可构造，缺失语义字段应重试完整阶段或报精确路径。
- [ ] 检查所有数组字段的“字段错位”问题：query binding、source ID、claim ID、accepted/rejected ID、required confirmation ID。
- [ ] 引用检查必须基于输入可信命名空间，不能让输出中另一个字段自我授权一个新 ID。
- [ ] 修复/重试不得按旧数组位置直接套用到已重排的新数组。

验收用例：

- [ ] 把 Stage 0 的精确 `"null"`、空对象、截断响应、数组残缺、字段错位用例参数化到 WF-3 四个模型节点。
- [ ] 回放 WF-3 首轮 Synthesis 的错误来源 ID，验证错误被精确定位或由可信别名映射确定性修复，并留下规范化记录。
- [ ] 构造一个合法 source ID 被放入 claim ID 列表的响应，验证不能因字符串格式合法而通过。

### 4.7 人工 Gate 的问题类型、答案类型和问题身份

Stage 0 历史问题：前端总是提交字符串；`"true"` 未转换为布尔值；“是/否”与 `true/false` 风格不统一；复合命题错误使用布尔控件；同一路径多问题或多轮问题被覆盖；控件承载不了完整回答。

WF-3 当前情况：`部分存在`，共享层已有修复，但需 WF-3 专项证明。

WF-3 涉及：

- 缺少研究问题时的 `PUBLIC_RESEARCH_NEED_INPUT`；
- 外发安全审批 `OUTBOUND_SECURITY_APPROVAL`；
- 在线结果导入审批 `ONLINE_RESULT_IMPORT_APPROVAL`；
- 任一模型节点返回 `NEED_USER_INPUT` 时创建的动态问题 Gate。

已有共享保护：

- 浏览器字符串可恢复为布尔/数值/枚举；
- 歧义转换会拒绝；
- 控件容量不足时会从 BOOLEAN/ENUM 扩大为 STRING；
- 复合布尔问题会降级为 STRING；
- Gate 创建与决定使用事务、context hash 和 compare-and-swap；
- 问题按身份保留，禁止空问题 Gate。

检查项：

- [ ] 使用 WF-3 实际 Gate 类型验证 `"true"`、`"false"`、`"是"`、`"否"`、数值字符串、JSON 对象/数组和空字符串。
- [ ] 外发审批与导入审批的 action 不得与问题答案混为同一字段。
- [ ] 动态问题中的 `question_type`、`answer_schema`、`blocking`、`priority` 和稳定 question ID 应由运行时构造或校验。
- [ ] 一个 BOOLEAN 问题只能包含一个可判断命题；复合、条件式、二选一或开放解释必须使用 STRING/OBJECT。
- [ ] 多轮 Plan/Synthesis 问题不得因 target path 相同而互相覆盖。
- [ ] Gate 回答注入下一次模型请求时必须保留问题文本、类型化值和目标语境，不能只剩 `true/false`。

验收标准：上述四类 WF-3 Gate 都具有端到端测试，并证明前端字符串不会直接冒充其他 JSON 类型。

### 4.8 失败响应、精确错误位置和重试反馈

Stage 0 历史问题：失败 response 曾未出现在证据包中；重复请求没有携带精确错误；代码只能报告“契约失败”而不能指出具体路径；历史错误没有进入回归测试。

WF-3 当前情况：`部分存在`。

- `app/runtime_evidence.py` 和 audited gateway 已支持在 parse/contract 失败时先保存原始或部分响应。
- `tests/test_minimax_gateway.py`、`tests/test_runtime_recovery.py` 已覆盖通用失败响应落盘。
- `SkillExecutor` 会保存 skill input/output/error，Public Search 失败也会分类记录。
- 但没有证据证明 WF-3 的 Plan、Synthesis、Research Critic、Import Critic 在空流、截断、非法 JSON、Schema 错误和引用错误下都能形成完整证据包。
- 当前 WF-3 Replay 没有纳入本项目首轮失败的真实请求与响应；因此仍可能重复 Stage 0 的“测试通过但真实失败未被覆盖”。

检查项：

- [ ] 六个模型节点故障注入：空流、截断 JSON、非对象 JSON、Schema 缺字段、非法 ID、未知来源、错误 Hash。
- [ ] 每次失败均能关联 request、raw response、parsed/rejected candidate、parse report、validation errors、run ID、call key 和 workflow checkpoint。
- [ ] 验证错误必须包含精确 JSON Pointer 或稳定对象身份，不能只给数组位置和笼统描述。
- [ ] 结构重试必须携带上一轮精确 validation errors，并生成新 call key；禁止无反馈原样重试。
- [ ] provider 重试不能消费语义修复预算；语义 `REVISE` 不能冒充网络/空响应重试。
- [ ] 将本项目 WF-3 的首轮 Synthesis、首轮 Critic、最终 Synthesis、最终 Critic 和 Import Critic 请求/响应固定为历史回归夹具或不可变摘要。
- [ ] 证据包测试必须检查文件内容，而不仅检查“文件存在”或总长度。

### 4.9 Canonical 校验、下一轮预检和原子提交

Stage 0 历史问题：无效 Finding/问题或跨轮状态先落库，下一轮才发现无法消费；Gate、artifact、workflow state 可能处于不一致状态。

WF-3 当前情况：`部分存在`。

- 当前通用 Gate 和 decision 持久化已有事务与 stale context 防护。
- Public Search archive 有 Hash 校验，Synthesis PASS 后有确定性 Claim—Source 绑定校验。
- Plan 在 LIVE Search 前由 `normalize_and_validate_plan(strict=True)` 验证。
- 但 Critic Finding 是否能被目标 Producer 执行，没有在写入修复状态前完成能力预检；`run-de8115e860304095` 已证明这类断裂可能发生。
- 检索集合替换前没有统一的下一轮 Synthesis/coverage 预构建与非退化验收。

检查项：

- [ ] Plan 写入前验证其可生成 Search 输入。
- [ ] Search Result 写入前验证 archive、coverage 和 Synthesis 输入均可构造。
- [ ] Synthesis 写入前验证 Claim binding、Critic 输入和 Import Critic 输入均可构造。
- [ ] Critic Finding 写入 repair checkpoint 前验证目标对象、路径、能力路由和下一轮输入均有效。
- [ ] Gate 创建、workflow checkpoint、artifact、audit event 必须同一事务提交。
- [ ] 任一预检失败时保留上一 accepted baseline，并只记录 rejected candidate/failure evidence。

## 5. WF-4 特有问题：不要错误迁移

以下历史问题属于 WF-4 Stage 0 的 Argument Skeleton/Design 与论证图结构，不能直接复制成 WF-3 规则：

- Skeleton 与 Design 的双阶段冻结；
- 研究线程、方法、评价、基线、创新点的图闭环；
- flat provider key 到 nested position 的重基；
- foundation support key 与 evaluation ref 的重基；
- `success_criteria` 被放入 `evidence_ids` 的具体字段名；
- Design 重试不得重生成 Skeleton；
- 线程间引用、图节点与矩阵行的笛卡尔误连；
- 四线程 baseline 保留。

但其背后的通用不变量仍适用于 WF-3：

- 字段不能错位；
- 引用不能自我授权；
- 数组重排后不能沿用旧位置补丁；
- 完整重试不得污染已接受基线；
- 新候选不得通过删除内容制造“问题减少”；
- 空响应、残缺对象和失败候选必须与有效候选严格区分。

## 6. WF-3 优化前必须先完成的 P0 Checklist

### 6.1 先补审计与测试

- [ ] 固定并回放以下历史运行：
  - `run-65a65c0e963a425e`：Research Plan PASS；
  - `run-ef65772880274b45`：首轮 Synthesis REVISE；
  - `run-de8115e860304095`：首轮 Critic BLOCK；
  - `run-c6168c0320354a95`：最终 Synthesis PASS；
  - `run-9adaf66163eb4db6`：最终 Critic PASS；
  - `run-ca78064053684e54`：Import Critic PASS。
- [ ] 为每个历史响应断言状态、Finding 阻断性、路由、对象权限、来源绑定、覆盖变化和候选基线选择。
- [ ] 把 `tests/test_stage0_historical_regression_inventory.py` 的通用故障类型参数化到 WF-3，而不是复制 WF-4 结构细节。
- [ ] 建立 WF-3 自己的历史回归 inventory，删除或改名历史守卫时必须显式失败。

### 6.2 再收口四个 P0 逻辑边界

- [ ] 字段归属：模型只生成语义，运行时生成机械字段。
- [ ] 状态不变量：advisory 不阻断，blocking 才返工；状态由 canonical 问题集合决定。
- [ ] 能力路由：Plan、Retrieval、Synthesis、Critic、User 各自只能修改其拥有的对象。
- [ ] 非退化基线：完整候选重生成可以继续使用，但新候选不改善就回退到 accepted baseline。

### 6.3 最后才优化检索覆盖

- [ ] 检索数量、每查询配额、来源多样性、全文比例和 citation snowballing 作为独立工作项处理。
- [ ] Coverage gate 不得只检查“每个查询至少一个来源”和全局任意一个 baseline/limitation 来源。
- [ ] Critic PASS 不得替代 corpus saturation/coverage PASS。
- [ ] 补充检索应围绕当前 WF-4 Stage 0 四条研究线程建立，不直接重跑同一组宽泛查询。

## 7. 推荐实施顺序

1. 建立 WF-3 历史请求/响应回放和当前请求长度基线。
2. 收口状态真值表与 Finding 能力路由。
3. 分离模型语义字段与运行时机械字段。
4. 建立 Plan/Search/Synthesis 的 accepted baseline 和非退化验收。
5. 补齐 WF-3 Gate、失败响应和事务端到端测试。
6. 在上述边界稳定后，再优化查询生成、数据源、全文抓取、来源数量与覆盖阈值。

## 8. 最终不变量

- 不修改 schema。
- 不让模型计算 Hash 或生成可由运行时确定的 ID、类型、路由、优先级和控制字段。
- 不因 advisory Finding 触发整轮返工。
- 不把 Critic 无法执行的建议伪装成 Producer 定向修复。
- 不用失败候选覆盖 accepted baseline。
- 不通过删除来源、Claim、问题或覆盖维度制造表面改善。
- 不进行无精确反馈的原样重试。
- 不只保存失败长度；必须保存完整请求、原始/部分响应、解析报告和精确验证错误。
- 不用 Replay 示例替代真实历史失败请求/响应回归。

## 9. 本次只读验证

2026-08-26 执行以下现有离线测试，结果为 `30 passed`：

- `tests/test_stage0_historical_regression_inventory.py`；
- `tests/test_gate_answer_contract.py`；
- `tests/test_wf3_input_adapter.py`；
- `tests/test_research_skill_track_c.py`。

该结果只能证明现有共享 Gate、WF-3 输入适配和研究归档基础测试未退化，不能证明本文列出的 WF-3 专项问题已经修复。本文中的未勾选项仍需新增历史回放或端到端测试后才能关闭。

## 10. 2026-08-26 第一轮 P0 收口进展

本轮遵守“不修改 schema、不进行 LIVE 调用”的边界，完成以下代码收口：

- [x] WF-3 Producer 顶层状态由 canonical `blocking=true` 内容项和人工问题确定性推导；`run-ef65772880274b45` 所代表的“顶层 `REVISE`，但 4 个 Finding、4 个 unresolved item 和 2 个问题全部 non-blocking”会规范化为 `PASS`，advisory 不再消耗修复预算或触发内容阻断。
- [x] `PASS` 中存在 blocking 内容项时确定性规范化为 `REVISE`；存在 blocking user question 时统一规范化为 `NEED_USER_INPUT`。原始业务对象不被改写，规范化原因写入 warning。
- [x] Research Critic 的 blocking Finding 在进入通用修复链前按 `RETRIEVAL / PLAN / SYNTHESIS / USER / BLOCK` 做能力预检。`run-de8115e860304095` 中指向 `retrieved_sources` 的撤稿和无关来源问题会明确路由为 `RETRIEVAL`，不再误送给 Synthesis 定向修复。
- [x] 越过 Synthesis 写权限的 Critic Finding 会以精确 route report 和原 Finding 留在 workflow state 中并内容阻断；当前没有伪造自动重检索，也没有把 Critic 建议假装成已经执行。
- [x] Public Search 重跑采用“完整候选 + 确定性非退化验收”：丢失既有 query、query coverage、coverage dimension 或 archive verification 的候选不得覆盖当前 accepted baseline；来源缩水只有在关键问题数确实下降且覆盖不退化时才允许。
- [x] 被拒绝的 Search 候选完整内容继续保存在不可变 Skill archive/artifact；workflow state 只记录候选 Hash、比较指标和拒绝原因，避免失败大对象污染 accepted context。
- [x] Synthesis、Research Critic 和 Import Critic 的模型视图去除了 `retrieved_sources.quoted_text` 与 `extracted_passages[*].source_ref.quoted_text` 的重复正文；passage 正文只保留一次。
- [x] `source_hash` 继续保留在内部验证上下文中用于可信引用绑定，但现有 provider business projection 在发给模型前确定性剥离；没有把 Hash 重新放回 Prompt。
- [x] Synthesis 无论模型候选状态为何，均执行确定性 Claim—来源绑定校验；不能通过写一个非 PASS 状态绕过校验。
- [x] 建立 `tests/fixtures/wf3_historical_regressions_20260826.json`，固定实际 run/call 与完整请求/响应 SHA，并回放 advisory-only Synthesis 与跨能力 Critic 控制面数据。

本轮明确未完成、不得宣称收口的项目：

- [ ] Plan 与 Synthesis 的完整候选重生成尚未建立和 Search 同等级别的 accepted baseline / 非退化验收；当前 WF-3 没有自动触发这两类全量重生成，因此先不扩大修改范围。
- [ ] Critic 的 `RETRIEVAL` / `PLAN` 路由目前只做到“正确识别并阻止错误修复”；尚未建立携带精确反馈的自动回退 Search/Plan 执行协议。
- [ ] 输出 Schema 仍要求模型生成部分 ID、固定类型和控制字段。本轮只完成模型请求中的 Hash/重复来源文本投影，没有修改 schema，也没有一次性重构所有输出机械字段。
- [ ] 六个 WF-3 模型节点的空流、截断、非对象、残缺行和字段错位故障注入矩阵尚未全部完成。
- [ ] Coverage gate 的来源数量、全文比例、来源多样性和 saturation 阈值尚未优化。

## 11. 2026-08-27 第二轮清单收口

本轮继续遵守“不修改任何 schema、不做 LIVE 调用”的边界，并完成以下项目：

- [x] 为六个模型节点和 Public Search 建立可执行字段归属表 `WF3_FIELD_OWNERSHIP`；模型只提供语义候选，协议常量、稳定 ID、输入复制字段、来源元数据、Gate 控制字段和最终状态由运行时投影或校验。
- [x] 稳定 ID 不再包含数组位置；同一语义对象仅因数组重排不会改变 ID。问题 target path 在生成稳定 question ID 前统一成 JSON Pointer。
- [x] 固定字符串 `"null"` 只在 schema 明确允许 JSON null 的路径转换；大小写变体、带空格值和普通文本中的 `null` 不转换。Safe Package、Plan 和 Synthesis 的业务 nullable 字段已有回归。
- [x] 三份 2026-08-26 LIVE Safe Package 失败响应已用当前规范化器回放，`answer_schema.required`、`payload.security_context` 和 `payload.source_items[n]` 三类错误均可确定性处理，且最终 strict schema errors 为 0。
- [x] 六个 WF-3 模型节点已参数化覆盖非对象、空对象、残缺容器和输出自授权未知来源；错误保留精确 JSON 路径。
- [x] Research Critic 的 source/claim 列表以及 Import Critic 的 accepted/rejected/confirmation 列表均只接受输入可信命名空间；导入 Claim 必须形成无重复、无交集、无遗漏的完整分区，合法 source ID 放入 claim ID 列表也不能通过。
- [x] WF-3 provider 结构重试只追加有界、去重的精确 validation errors；不附加完整失败候选，不把失败候选写成 baseline，成功后清除 checkpoint 中的反馈。
- [x] 六个模型节点分别设置 provider-visible 字符预算；历史请求的 system/envelope/provider-visible 字符数已写入 `tests/fixtures/wf3_historical_regressions_20260826.json` 并受回归测试保护。
- [x] Critic 的控制状态和 verdict 由 canonical blocking finding/question 推导；advisory-only Critic 不能触发 REVISE，blocking PASS 不能放行，无可执行路由的阻断项进入 BLOCK。
- [x] Research Critic Prompt 明确只审查实质支持、过度概括、关键反证和问题回答度；ID、Hash、年份、重复、查询覆盖、manifest 和安全标签由确定性层负责。Critic PASS 不再代表 corpus saturation PASS。
- [x] Plan、Search、Synthesis 均具备“完整候选 + 非退化验收 + exact accepted baseline + rejected candidate 留档”规则，不做任意 JSON 融合。Plan 冻结研究问题顺序和 query binding；Synthesis 不允许静默丢失已验证 Claim、来源绑定、比较主题、局限或冲突。
- [x] Plan 写入 accepted baseline 前验证所有研究问题被合法 query index 覆盖；Synthesis 候选先做 Claim—来源绑定预检。已有 baseline 时，预检失败候选落盘但不能替换 baseline。
- [x] ContextBuilder 按 workflow state 中的精确 accepted run ID 读取 Plan/Synthesis；同时验证 persisted output hash，禁止“按最新时间”误取 rejected candidate。
- [x] WF-3 Gate 的浏览器字符串到 BOOLEAN、NUMBER、OBJECT、ARRAY 的确定性转换已覆盖 `true/false/是/否`、数值字符串和 JSON 字符串；同 target path 的多轮问题仍按稳定 question ID 分开保存，并保留问题文本、类型化答案和目标语境。
- [x] 模型输出规范化版本提升为 `v50`，确保部署后历史 BLOCKED_CONTRACT checkpoint 能按新的本地规范化规则安全重验，而不是无提示沿用旧版本。

验证结果：WF-3、Gate、输出容器、检索 Skill 和 provider 投影专项共 `180 passed`；WF-3 SIMULATED 完整消费链及全工作流 DOCX 集成另有 `2 passed`。完整仓库套件首先被既有 `G0_REGISTRY_IDENTITY_DRIFT` 阻断（当前 registry digest `8ab91d...`，冻结治理值 `73ad0e...`），排除 F 治理组后又遇到与 WF-3 无关的 WF-4 14 章节集成夹具 150 秒超时。上述两项未通过修改 schema 或放宽治理基线掩盖。

仍需独立处理、但不属于本轮 WF-3 Stage 0 合同收口的事项：

- [ ] Critic 的 RETRIEVAL/PLAN 路由自动回退执行协议；当前已正确识别、保留精确 Finding 并阻止错误送入 Synthesis，但不会伪造已经重新检索。
- [ ] F 治理清单中的 prompt registry 冻结 digest 与当前代码基线对齐；这需要单独审计 prompt registry 变更来源，不能在本轮顺手改治理哈希。
- [ ] WF-4 全量章节集成测试的性能/超时问题；与 WF-3 逻辑无直接依赖。

本轮离线验证：

- `tests/test_wf3_contracts.py`、provider 投影专项：`14 passed`；
- WF-3、Stage 0 历史 inventory、输出容器、Gate、输入适配和研究 Skill 相关回归：`129 passed`；
- 严格 LIVE 输入构建 + SIMULATED provider 的 WF-1 至 WF-5 全流程：通过；
- `compileall` 与 `git diff --check`：通过；
- schema 文件修改数：`0`；LIVE 模型调用数：`0`。

## 12. 2026-08-27 第三轮：真实 Provider 契约与来源归属收口

本轮针对 LIVE 运行 `wf-a0b3416dcff5401f` 的三次 Safe Package 失败继续收口，仍然不修改任何 schema：

- [x] 查明完整校验 Envelope 中虽有 `trusted_source_catalog`，但正常执行器在调用 Provider 前会确定性移除该目录；此前日志中的 `input_envelope` 实际是校验上下文，不是模型真实收到的请求。
- [x] 六个 WF-3 模型节点的顶层 `source_refs` 改为运行时所有：模型值无论为空、字段路径、真实输入 ID 或虚构 ID，都会被忽略；运行时按各节点实际消费的顶层业务输入生成可信来源并补全元数据。
- [x] 六份 Prompt 统一要求顶层 `source_refs=[]`，不再要求模型检查 Hash、版本、Schema、环境或引用存在性，也不再要求模型生成新机器 ID；schema 必填的新 ID 使用 `runtime` 兼容占位并由运行时覆盖。
- [x] Synthesis 只保留必要的语义来源选择：`result.claims[].source_refs[].source_id` 必须逐字复制 `retrieved_sources` 或 `extracted_passages` 中可见的真实来源 ID；Hash、权威等级、安全标签、Span 等元数据仍由运行时绑定。
- [x] Finding 中 provider 可见的 `payload.<field>` 证据路径由运行时绑定到所属输入对象的 canonical ID；目标路径确定性转为 JSON Pointer。
- [x] `PROMPT_TRACE` 同时保存并明确标注 `validation_envelope` 与 `provider_request_envelope`，后者就是实际传给模型的请求，并单独保存 Hash；兼容字段 `input_envelope` 明确标注为 `VALIDATION_ENVELOPE`。
- [x] 将 `run-bbace040b0ad4a17`、`run-4a8258310e274e4a`、`run-9d4263fe9ee84396` 中出现的真实错误来源形式加入回归。三份完整数据库响应离线回放分别得到 `NEED_USER_INPUT`、`NEED_USER_INPUT`、`PASS`，strict schema 和 WF-3 语义错误均为 0。
- [x] 输出规范化版本升级为 `v51-wf3-runtime-provenance`，使旧的来源契约失败 checkpoint 能识别新规范化边界。

验证结果：WF-3、Provider 投影、重试恢复、工作流状态与事务相关测试共 `233 passed`；`compileall` 与 `git diff --check` 通过；schema 文件修改数为 `0`，LIVE 模型调用数为 `0`。

仓库全量 Prompt Pack 校验仍被此前已经存在的 Replay fixture 问题阻断：多份 `missing_input.json` 使用 `answer_schema={"type":"OBJECT","allowed_values":[]}`，而当前共享契约要求 OBJECT 提供 `properties`。该问题跨越全部工作流且涉及 schema/fixture 治理，本轮按“不修改 schema、不过分扩大范围”的边界没有顺手处理；它不是上述 WF-3 修改产生的失败。
