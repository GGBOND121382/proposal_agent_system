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
- [ ] 后续 Critic 路由的 `ORIGINAL_PRODUCER` 重生成目前会整体替换已有 Producer 结果；应建立精确基线、局部修复范围和确定性非退化验收，禁止新候选通过删除既有内容来减少表面问题。位置：`app/workflows.py::_prepare_original_producer_regeneration`。
- [ ] 章节写作与整稿 acceptance regeneration 也采用整体替换；后续 prompt 开始改造时，应为 `app/workflow_authoring_base.py` 与 `app/full_proposal_sections.py` 增加基线保留、目标范围和非退化校验，并单独评估请求体积。
- [ ] 为阶段 1 及后续阶段统一“advisory 不阻断、blocking 才返工”的状态不变量，并增加跨 Producer/Critic/acceptance 的契约测试；本次不提前修改这些尚未运行的阶段。

## 不变量

- 不修改 schema。
- 不让模型生成可由运行时确定性得到的类型、路由、定位和控制字段。
- 不因目标路径相同而丢弃不同问题的人工回答。
- 任何跨轮状态必须在持久化前通过 canonical 校验和下一轮输入预检。
