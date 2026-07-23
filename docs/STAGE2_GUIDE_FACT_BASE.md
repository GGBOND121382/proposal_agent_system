# 阶段2：申报规则与事实底座

## 目标

阶段2接收已经通过阶段1确认的 `stage1_design_input.json`，生成以下冻结工件：

- 来源注册表；
- 规则表；
- 原子事实账本；
- 开放事项清单；
- 写作权限分区；
- 阶段放行结论。

阶段2不生成申请书正文，也不把通用写作经验当作正式申报指南。

## 来源权威顺序

1. 正式申报指南；
2. 用户明确要求；
3. 已确认设计输入；
4. 工作假设。

正式申报指南未提供时，必须记录为 `NOT_PROVIDED`，模板、资格、时间节点、评审权重和附件要求保持未决。

## 事实状态

- `USER_ASSERTED`：用户明确提出，可直接陈述；
- `CONFIRMED_DESIGN`：已确认设计工件中的内容，可直接陈述；
- `PROVISIONAL_TARGET`：暂定指标，只能带限定语使用；
- `WORKING_ASSUMPTION`：工作假设，只能带限定语使用；
- `UNKNOWN`：未知信息，禁止作为确定事实写入。

## 新增确定性约束

每个开放事项必须同时满足：

1. 出现在 `open_items`；
2. 出现在 `writing_permissions.unknown_fields`；
3. 至少有一条 `UNKNOWN` 事实通过 `related_design_ids` 绑定对应 `OPEN-*` ID；
4. 该事实必须进入 `prohibited_fact_ids`。

该约束用于防止后续模型只按事实ID过滤时遗漏某个未决字段。

## 命令

```bash
python stage2_tools/stage2_guide_fact_base.py init \
  --run-dir <run_dir> \
  --design-input <stage1_design_input.json>

python stage2_tools/stage2_guide_fact_base.py ingest-generator \
  --run-dir <run_dir> \
  --response-file <generator_response.json>

python stage2_tools/stage2_guide_fact_base.py ingest-critic \
  --run-dir <run_dir> \
  --response-file <critic_response.json>

python stage2_tools/stage2_guide_fact_base.py finalize \
  --run-dir <run_dir> \
  --gate-response <gate_response.json>
```

## 放行边界

阶段2通过后只放行 `STAGE_3_PROJECT_DEFINITION`。在正式指南、模板、团队、经费、周期、研究基础证据和参考文献未补齐前，不放行正式章节规划和正文生成。
