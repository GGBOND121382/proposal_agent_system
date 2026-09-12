# 阶段 0 契约边界后续事项

## 本次修复边界

本次只处理当前阶段 0 无法继续的刚需逻辑，不修改任何 schema，也不提前改造后续尚未开始的 prompt。

已纳入本次修复：

- 语义检查结果转回原生产者时，由运行时构造完整的 canonical Finding；机械字段不再交给模型补齐。
- 跨轮状态写入前，先校验生成的 Finding，并用候选状态预构建下一轮输入；无效状态不得落库。
- 人工 Gate 回答按“`question_id` + 规范化问题文本”判定覆盖关系。同一路径下的不同问题、以及不同轮次复用同一位置编号的不同问题必须全部保留；同一问题的新回答仍覆盖旧回答。
- 阶段 0 的语义模型输入保留最小问题语境（问题标识、问题文本、类型化答案和目标），避免只剩下含义不明的“是/否”。
- 布尔 Gate 只允许单一可判断命题；复合、条件式或二选一问题由生成约束要求拆分，运行时仍检测到歧义时确定性降级为 STRING，不猜测拆分后的语义。
- 所有 Gate 创建前统一检查控件是否足以承载完整回答；开放式 BOOLEAN、多值/分组 ENUM 只会扩大为 STRING，已有完整 properties/items 的 OBJECT/ARRAY 保持不变。

## 后续 TODO

- [ ] 在后续 prompt 开始改造时，逐个审计模型输入投影，区分必须由模型推理的语义字段与应由运行时确定性生成的机械字段。
- [ ] 为其余跨轮适配器建立统一的“构造、canonical 校验、下一轮输入预检、原子提交”边界；先盘点和测试，不以修改 schema 代替修复。
- [ ] 统一人工 Gate 问题的展示与答案词汇（例如“是/否”），同时保持前端字符串到布尔值、枚举值及空值的确定性转换。
- [x] 禁止用一个布尔 Gate 承载两个条件命题；阶段 0 已增加单命题生成约束和确定性 STRING 兜底。
- [ ] 增加覆盖多轮 Gate、问题重定向、同路径多问题和旧工件迁移的端到端不变量测试。
- [ ] 后续 prompt 改造时单独记录请求长度预算，删除对模型无推理价值的哈希、内部 ID 和重复上下文；不得在本次阶段 0 修复中顺带扩大范围。
- [ ] 盘点其他 canonical 对象是否存在手写不完整字典；优先收敛为集中构造器和公共校验入口，但每一类对象单独评审、单独提交。
- [ ] 阶段 1 Argument Critic 的最终状态目前仍以“存在任意 finding”判为 `REVISE`；应区分阻断性 finding 与 advisory finding，避免非阻断建议触发整轮返工。位置：`app/model_semantic_contracts.py::_critic_final_status_from_canonical_state`。
- [ ] 重构 Argument Critic 为轻量语义审查，不再让模型重复完成运行时已经能够确定性完成的机械校验。模型只审查论点价值与可证伪性、方法实质性、创新点区分度、评价方案能否回答研究问题等主观语义；图拓扑、矩阵闭环、ID/引用存在性、证据与基线覆盖、路由、优先级及 `blocking` 等字段继续由运行时生成和校验。
- [ ] Argument Critic 输入按研究线程或高层语义单元投影，删除要求模型回显全部 `reviewed_unit_keys`、机器 ID、证据 ID 和维度映射的内容；为请求长度设置预算并增加回归断言。只有通过契约校验的 P0/P1 语义 finding 才能阻断阶段 0；Critic 自身连续输出契约失败时应记录为 `REVIEW_UNAVAILABLE`/告警，不得推翻已经通过确定性校验的 Producer 结果。
- [ ] 将本次 Argument Critic 的六次历史失败请求与响应纳入回归测试，覆盖不存在的 `evidence_ids`、review unit 与 component 不一致、dimension 与失败质量维度不一致等错误。若仍执行模型重试，下一轮必须携带精确的验证反馈，禁止对同一请求进行无反馈的原样重试。
- [ ] 后续 Critic 路由的 `ORIGINAL_PRODUCER` 重生成目前会整体替换已有 Producer 结果；应建立精确基线、局部修复范围和确定性非退化验收，禁止新候选通过删除既有内容来减少表面问题。位置：`app/workflows.py::_prepare_original_producer_regeneration`。
- [ ] 章节写作与整稿 acceptance regeneration 也采用整体替换；后续 prompt 开始改造时，应为 `app/workflow_authoring_base.py` 与 `app/full_proposal_sections.py` 增加基线保留、目标范围和非退化校验，并单独评估请求体积。
- [ ] 为阶段 1 及后续阶段统一“advisory 不阻断、blocking 才返工”的状态不变量，并增加跨 Producer/Critic/acceptance 的契约测试；本次不提前修改这些尚未运行的阶段。

## 2026-08-28 仍未关闭的问题

以下事项是在 WF-3 v51 边界修复和历史失败响应回放之后仍然存在的问题。它们不应被描述为“已经解决”，也不得通过放宽 canonical schema 掩盖。

### 本轮已经关闭的确定性故障（2026-08-28）

- [x] WF-3 创建时固定完整检索时间窗；Plan 的 `time_scope` 由运行时从该输入复制，且在接受 Plan 基线前复用 Search 的严格校验器。
- [x] Plan 查询条数与执行器上限统一为 12，禁止计划被静默截断。
- [x] SearXNG 不再丢弃返回的发布日期、作者、出版者和 DOI；本机 LIVE 配置改为 hybrid 学术多源 + SearXNG。
- [x] 严格检索容量在最低覆盖数之外保留 20%（至少 5 条）余量，避免一次抓取失败或去重就必然失败。
- [x] Synthesis 的 provider 投影对 passage 正文采用固定总预算，保留全部来源身份；完整归档仍用于确定性质量校验。
- [x] Public Search 在线程中执行，不再同步阻塞 Web 事件循环。
- [x] Safe Package Critic 的人工 `valid_until` 回答会形成有效下游包；Import transfer manifest 使用真实外发审批人、审批时间和有效期，不再写死。
- [x] Import Critic 的注入/越界布尔值及命中后的 Claim 拒绝分区由运行时根据安全 Finding 确定性投影。

以上项目均未修改 schema；本地 WF-3、检索、依赖预检和 provider 投影回归已通过。仍需下面列出的 LIVE 闭环验证，不能把本地通过表述为真实调用已经通过。

### P0：Prompt Pack 的 Replay 契约仍阻塞全量验证

- [ ] 修复 Replay fixture 中不合法的 OBJECT 回答定义。当前约 30 份输出 fixture 仍使用 `answer_schema={"type":"OBJECT","allowed_values":[]}`，但共享契约要求 OBJECT 明确定义 `properties`；因此 `py prompt_pack/tools/validate_pack.py` 仍不能全量通过。
- [ ] 先盘点受影响 fixture 的真实语义，再迁移 fixture 或在输入边界执行有依据的确定性规范化；禁止把未知 OBJECT 静默改成 STRING，也禁止为了兼容旧 fixture 而削弱共享 schema。
- [ ] 将此次问题加入跨工作流回归测试，至少覆盖：合法 OBJECT、缺少 `properties` 的旧 fixture、不可无损迁移的输入，以及迁移后 Prompt Pack 全量审计。

验收条件：

- `py prompt_pack/tools/validate_pack.py` 全量通过；
- Prompt Contract 语义测试和审计测试均通过；
- canonical schema 未修改、未放宽；
- 不合法 fixture 不再被误报成 WF-3 模型输出问题。

### P0：WF-3 最新改动尚未完成 LIVE 闭环验证

- [ ] 基于最新代码重建一个 WF-3 工作流并执行真实 LIVE 验证；当前只完成了历史失败响应回放和本地契约测试，尚不能据此宣称最新流程已经在真实提供方调用下通过。
- [ ] LIVE 验证时保存每个节点的完整 provider request、原始 response、规范化结果、校验错误和最终路由，确认模型看到的是精简后的请求，而不是旧工作流中已经固化的旧 prompt/input snapshot。
- [ ] 在宣称修复完成前验证六个节点的真实执行顺序、Gate 行为、来源引用归属和最终持久化结果；不得只以单个响应可解析作为通过依据。

### P0：提供方失败证据矩阵尚未覆盖完整

- [ ] 在 WF-3 六个模型节点补齐网关级失败注入：空流、截断 JSON、非法 JSON、非对象响应及缺字段响应。现有测试已经覆盖部分非对象/缺字段/未知来源情形，但尚未证明所有节点在空流和截断场景下都能保存完整原始证据。
- [ ] 断言失败时同时保留 provider request envelope、原始 response/stream 片段、解析错误、canonical 校验错误、重试序号和最终状态，避免再次出现“只有长度、没有完整请求或响应”的证据包。

### P1：WF-3 Critic 的跨节点回退仍未自动闭环

- [ ] Critic 已能识别 `RETRIEVAL`/`PLAN` 类问题并避免错误地交给 Synthesis 修复，但尚未实现基于反馈自动回退并重跑 Search/Plan 的完整协议。
- [ ] 后续实现必须明确：问题归属、允许修改的节点、接受基线、非退化条件、最大重试次数和失败后的可见状态；不得用 Synthesis 全量重生成掩盖上游检索或计划缺口。

### P1：检索覆盖率和停止条件仍需校准

- [ ] 为 WF-3 建立可解释的覆盖验收：相关工作数量、全文可得率、来源类型/数据库多样性、主题维度覆盖和检索饱和度；当前规则还不足以稳定避免“结果太少且偏窄”。
- [ ] 使用既有项目输入和历史请求/响应做基线评测，区分查询规划不足、数据源受限、召回过滤过严、全文获取失败和 Synthesis 丢失，不得只调大 top-k 或重试次数。
- [ ] 将覆盖不足作为明确、可路由的问题暴露给用户或上游节点；不得全部写成 `blocking=false` 后静默进入下一阶段。

### P1：模型侧仍承担 canonical 形状的兼容占位成本

- [ ] 当前运行时已经接管来源 ID、引用归属等机械字段的最终值，但模型输出仍需遵守完整 canonical 形状，并为部分运行时字段返回约定占位值。这消除了“让模型猜 ID”，但尚未消除占位字段和完整 schema 带来的请求/输出负担。
- [ ] 后续评估单独的 provider-facing 语义投影：模型只返回语义内容，运行时再构造 canonical 对象。该改造必须保持持久化 schema 不变，并逐节点回放历史请求/响应后再启用。
- [ ] 为各节点记录 system prompt、用户输入、schema 和预估输出的独立长度预算，防止后续修复再次让 prompt 隐性膨胀。

## 不变量

- 不修改 schema。
- 不让模型生成可由运行时确定性得到的类型、路由、定位和控制字段。
- 不因目标路径相同而丢弃不同问题的人工回答。
- 任何跨轮状态必须在持久化前通过 canonical 校验和下一轮输入预检。
