# WF-1 引用编号与局部修复：修改说明

日期：2026-09-11。本文是下一位智能体的实施说明，不代表修改已经完成。

## 1. 修改目标与范围

解决《美空军DASH系统调研分析报告》WF-1 中的三类问题：

1. 模型漏填可确定处理的协议字段，程序却反复调用模型修复。
2. 原生成请求中的合法引用进入修复后被判为不存在。
3. 修复模型看不到原候选、局部编号映射或字段类型，无法正确修改。

本次优先限定在 **WF-1 项目定义抽取及其局部修复路径**。复用已有语义契约、编号生成、来源绑定和手动文种功能，不另建通用图谱框架，不顺带重构全部工作流。

用户已明确接受：项目手动指定为 `SURVEY_REPORT`；调研受理可以跳过完整科研论证图。不得继续要求用户先提供调研答案、我方创新点或研发团队基础。

前序交接：`docs/WF1_MANUAL_DOCUMENT_TYPE_HANDOFF_20260910.md`。其中一些“尚未设置”的状态随后可能已变化，以数据库和实际请求为准。

## 2. 最新失败的真实依据

项目：`project-d39c11d09c7445d3`。

本次审计的最新流程：`wf-11d5a0f0ef814cfc`，`BLOCKED_CONTRACT`，内部步骤 4，最后更新时间为 2026-09-10 22:37 北京时间。

最后一次抽取运行：`run-204588b00ed34e38`。

请求文件：

`data/model_calls/requests/call-provider-8592a19347e0039ae2cae346-cycle-f5528dce9653fd43-attempt-4.json`

已确认：

- 项目配置、canonical 输入和实际模型输入均已携带 `SURVEY_REPORT`。
- 实际调用记录 `provider_model_name=MiniMax-M3`，使用 direct tool arguments。
- 模型输出 9 个条目、0 条关系，本次不再因复杂关系图而直接失败。
- 最后失败是三个问题缺少 `gap_keys`，合同缺少 `max_main_pages`、`max_core_research_questions`。
- 实际输出 Schema 中两个 max 字段均允许 `null`；`gap_keys` 是数组，允许空数组。
- 请求中的证据卡编号为 `S1…S8`。
- 该流程共 33 条 prompt run，20 条为 `P-TARGETED-REPAIR`，16 条状态 ERROR。prompt run 数量不等于底层 HTTP 调用次数。

一次修复响应：

`data/model_calls/responses/call-contract-repair-c1d5047ca6f21627171e7d78-cycle-509ba242375093e1-attempt-1.parsed.json`

该响应把 `gap_keys` 的新值写成字符串 `"[]"`，并提出 `max_main_pages=30`。在当前读取到的局部修复 Schema 中 `changes[].value` 是任意 JSON 值，因此数组本应直接输出 `[]`，不能混用 JSON 字符串和数组。30 页也不能在没有授权约束的情况下补入。

最终修复请求和响应：

- `data/model_calls/requests/call-contract-repair-ac11b2417f658b2a3762a44d-cycle-7e35e7dec2f0b69d-attempt-1.json`
- `data/model_calls/responses/call-contract-repair-ac11b2417f658b2a3762a44d-cycle-7e35e7dec2f0b69d-attempt-1.parsed.json`

最终请求的 `previous_attempt_feedback` 把 `S1…S8` 中多个编号判为不在允许的输入编号范围。最终响应则因“不知道正文最大页数”选择 ESCALATE，尽管原 Schema 允许 null。

**结论边界：** 可以确认原请求中存在这些证据编号，也可以确认修复阶段报了不存在。到底在哪一层丢失、误分类或错用了校验器，需要沿调用链定位；不能未经验证就认定是某一个函数造成的。

当前代码已存在 `app/output_integrity.py::_collect_inherited_repair_entity_ids`，从 `payload.inherited_source_catalog` 继承来源编号。**不要重复实现同名机制，先确认实际失败路径是否构造、传递并使用了它，以及它是否保留了正确的引用类型。**

## 3. 编号职责：哪些交给代码，哪些仍需模型判断

| 编号 | 产生者 | 模型职责 | 校验范围 |
|---|---|---|---|
| `doc-… / docv-… / sec-…` | 文档解析和持久化代码 | 从给定证据卡引用，不自行编造 | 实际输入绑定的文档、版本与段落 |
| `S1…Sn` | 本次模型输入投影代码 | 选择支持内容的证据卡 | 该次生产请求的证据目录 |
| `I1…In` | 当前模型条目的局部编号 | 保持唯一，关联只能指向本次已声明条目 | 本次候选条目集合及类型 |
| `item-… / relation-…` | canonical 装配代码 | 不生成、不猜测 | 已验证候选映射与持久化对象 |
| Hash、版本和安全元数据 | 运行时代码 | 不生成、不修补 | 系统记录和权威来源 |

局部编号可以暂时保留由模型生成，避免扩大改造。代码必须检查重复、悬空与端点类型，不可把不存在的 `I37` 自动换成最相似的已有条目。

**编号存在不代表证据支持结论。** 编号校验解决可解析性，语义审查仍负责“这段来源是否真能支持该结论”。

## 4. 按优先级实施

### P0-A：调研模式的已知空字段由代码处理

先减少不必要的模型修复，再处理复杂关联修复。

在原生产语义结果校验之前，增加明确、有限的调研受理归一化规则。建议复用 `apply_semantic_model_output_defaults` 的调用阶段，但为 WF-1 报告模式建立专用白名单，不把“任意缺字段都补空”推广到全局。

| 字段 | 允许的确定性处理 | 限制 |
|---|---|---|
| `argument_seed.research_questions[*].gap_keys` | 在明确采用无差距图的调研受理模式时，缺失可补 `[]` | 不覆盖已有值；科研申请书缺少研究差距关联不能自动清空 |
| `proposal_contract.max_main_pages` | 优先使用已确认交付约束；没有设定页数上限时使用 `null` | 不把“6000—10000 字”换算为 30 页；不能因为模型漏填就忽略材料中的真实页数要求 |
| `proposal_contract.max_core_research_questions` | 优先使用已确认数量约束；未规定上限时使用 `null` | 当前生成了三个问题，不等于用户要求“最多三个问题” |

原始模型响应保持不变。归一化结果单独留痕，记录原路径、新值和规则来源，并在正式语义 Schema 校验前应用。归一化应幂等。

现有默认补全主要处理顶层数组，这次缺项位于嵌套对象中，不能假设现有通用函数已经覆盖。

如果必需的条目内容、真实证据绑定或研究对象缺失，仍交给模型修复或明确阻断，不能用空数组冒充完成。

### P0-B：修复必须继承原生产请求的编号范围

修复作用于候选 A，就必须继承生成 A 时的证据编号目录，而不是重新从当前项目最新资料中编号。

生产请求被裁剪、重新排序或补充材料后，`S1` 可能代表不同内容。不能因为编号字符串相同就认定是同一证据。

最少需要绑定：

- 原生产 run/call 标识与请求摘要。
- 修复基线候选摘要。
- `S编号 → 文档、版本、段落` 的准确映射。
- 候选条目局部编号集合及条目类型。
- 已有的局部编号与 canonical 编号映射（修复反馈使用 canonical ID 时才需要）。

这些内容由代码从持久化的原请求和候选构建，不让修复模型返回或修改。不要把候选输出中出现的任意字符串自动登记为合法输入证据，否则模型虚构的 `S99` 会获得合法性。

校验必须区分：

- `evidence_ids` 对证据卡目录校验。
- `from_key/to_key` 对候选条目集合校验。
- `gap_keys` 对 GAP/PROBLEM 类型的候选条目校验。
- canonical 引用对展开后的对应编号范围校验。

可以复用现有 inherited catalog 和语义引用校验器，但不得以“允许全部 `_id` 字符串”方式修复。

### P0-C：修复值要携带目标类型和原候选上下文

修复目标不能只有一个路径和“字段缺失”。建议每个目标至少携带：

```json
{
  "path": "/proposal_contract/max_main_pages",
  "value_present": false,
  "current_value": null,
  "expected_value_schema": {"type": ["integer", "null"], "minimum": 1},
  "problem": "Missing required field",
  "local_context": {"confirmed_page_limit": null}
}
```

上例只是结构建议，不表示当前已有这些字段。`value_present` 用于区分“缺失”和“明确为 null”。

对数组目标，提供数组 Schema 和合法成员目录；对关系目标，提供该关系、两个端点、局部编号、节点类型与允许的关系约束。

当前 `changes[].value` 应使用原生 JSON 类型：

```json
{"path":"/argument_seed/research_questions/0/gap_keys","value":[]}
```

不要全局尝试对每个字符串执行 `json.loads`；这会把合法文本误改为数值、数组或对象。若历史调用协议明确采用字符串化 JSON，只能在该协议版本的适配层做一次有类型校验的解码，并保留原响应。

新增或修改修复字段时，需要同步模型输入 Schema、构造器与测试，不只修改提示词。

### P1：修复应用后回到原生产契约校验

建议顺序：

```text
原生产请求 + 候选
  → 报告模式的确定性缺省处理
  → 原生产模型 Schema 与语义引用校验
  → 若仍有可局部修复的错误，构建最小修复上下文
  → 修复模型只返回 patch
  → 检查允许路径、JSON 类型、基线摘要
  → 在基线副本上原子应用全部 patch
  → 用原生产契约和原编号目录重新校验
  → canonical 装配，代码生成正式 ID 与来源元数据
  → canonical 引用校验与质量审查
  → 接受，或给出具体失败类型
```

禁止只通过 `P-TARGETED-REPAIR` 自己的输出 Schema 就宣布原对象修好。也不能把尚未展开的 `S/I` 局部对象直接交给只认识 canonical 编号的校验器。

修复状态 `APPLY` 必须只包含实际变更，`ESCALATE` 必须没有变更。一次补丁中的任何路径、类型或基线检查失败，全部不应用，不能留下部分修复结果。

来源装配生成的 Hash、span、quoted_text 等不能被误记成模型越权修改：应比较同一阶段的语义对象，或在前后对象上采用完全一致的确定性处理。不要为了避免误报而解除真正业务字段的保护。

### P1：区分错误归属，避免相同错误无效循环

错误记录至少提供：字段路径、引用值、预期编号类型、原生产 run、修复基线，以及具体错误种类。

可采用如下分类（名称是建议，不要求新增一整套框架）：

- 模型引用了本次不存在的条目。
- 模型把证据编号用于条目关联。
- 修复上下文没有包含原证据目录。
- 使用了其他 run 的编号映射。
- patch 值类型不符合目标字段。
- 缺失字段已有确定性默认处理规则。

程序缺少引用目录时，不能反复提示模型“换个合法 ID”，更不能让用户补正文页数来掩盖编号上下文错误。相同候选、相同错误、相同上下文不应无限重试；也不要单纯增加重试上限。

## 5. 代码定位

实施前检查 staged 与 unstaged diff；另一个智能体可能已经部分修过以下位置。函数名比固定行号更可靠。

| 文件 | 优先检查的位置 |
|---|---|
| `app/model_semantic_contracts.py` | `apply_semantic_model_output_defaults`、`_wf1_semantic_reference_errors`、`semantic_model_reference_errors`、`build_targeted_repair_model_input`、`expand_targeted_repair_model_output`、WF-1 语义输入与 canonical 装配 |
| `app/output_integrity.py` | `_collect_inherited_repair_entity_ids`、`collect_allowed_reference_ids`、`validate_reference_ids`、`inject_reference_targets_into_schema`、修复 diff 的代码派生字段处理 |
| `app/runtime_executor.py` | 原生产校验失败后如何构建修复、传递原请求、应用修复并重新验证；区别语义对象和 canonical 对象 |
| `app/executor.py` | 兼容执行路径、协议引用规范化、修复 diff、归一化版本 |
| `app/workflows.py` | 修复次数、候选基线、反馈与预算耗尽错误信息 |
| `prompt_pack/schemas/model/targeted_repair_model_*.schema.json` | 修复上下文、patch 值的真实 JSON 类型 |
| `prompt_pack/schemas/model/project_definition_extract_model_output.schema.json` | 嵌套必填字段、nullable 字段、报告模式兼容要求 |

不要只修其中一个执行器却忘记当前 LIVE 实际走 `RuntimePromptExecutor`。同样，不要从磁盘已经存在一段新代码就推断上次失败请求已经使用了它。

## 6. 必须完成的验收用例

### 确定性缺省与事实边界

1. 手动调研模式、空关系图、缺少 `gap_keys`，补 `[]` 后通过原生产 Schema，不调用修复模型。
2. 没有页数限制时补 null；明确材料要求 20 页时不得补 null 或 30。
3. 当前三个调研问题但没有数量上限时，不虚构“最多三个”的用户约束。
4. 原有非空字段不被默认规则覆盖；重复归一化结果一致。
5. 科研申请书不能通过上述报告默认规则绕过差距关联要求。

### 编号范围与修复

6. 原请求真实存在 S1/S2，修复后保持合法；不存在的 S99 必须失败。
7. 同名 S1 来自不同 run 时，映射必须绑定正确原请求，不得混用。
8. I37 未声明时失败；已声明但类型不属于 GAP/PROBLEM 时，不能用于 gap_keys。
9. 原证据目录缺失时，报修复上下文错误，不误报模型虚构来源。
10. 仅在输出候选里出现 S99，不能把它自动纳入允许目录。
11. 修复 []、null、整数时保持真实 JSON 类型；字符串 `"[]"` 不能冒充数组。
12. patch 指向错误基线、越权路径或部分值无效时，整个 patch 不应用。
13. 修复后分别通过原生产语义校验与 canonical 校验，不把两种编号阶段混在一起。
14. 系统补齐来源元数据不触发模型越权误报；模型改变受保护业务内容仍被拒绝。

### 真实失败回放与在线验证

先用第 2 节中的持久化请求和响应构造隔离回放，证明本次缺字段可以不调用额外模型修复，并且 S1…S8 不会被错误拒绝。

再重建 DASH 项目的 WF-1 验证。保留原始 request/response、归一化记录和实际 Guard 结论。不能把测试中预设 PASS 或人工接受缺口当作真实自动通过。

本任务验收重点是受理和引用修复，不要求本轮完成整份报告或整个通用修复框架。

## 7. 完成时需要交付什么

- 最小代码改动与适用范围说明。
- 对第 2 节真实失败的回放结果：哪些由代码修复、哪些仍需模型。
- 上述关键反例的测试结果，特别是 S99、跨 run 的 S1、明确页数限制不能被覆盖。
- 实际 WF-1 是否通过、停在哪个节点；如未完成，列出真实剩余阻断。
- 新旧模型调用数对比，按 prompt run 和底层请求分别统计，不能混为一个数字。

不以“换了更强模型后碰巧成功一次”作为编号链路正确的证明。模型仍需承担语义选择的一致性，但确定性字段、编号映射和校验上下文必须由代码保证。
