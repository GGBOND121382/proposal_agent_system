# 统一模型输出契约 v3

## 1. 修复目标

本次改造解决模型输出在不同字段、不同阶段和不同版本之间发生枚举漂移后，严格 JSON Schema 校验将工作流置为 `BLOCKED` 的系统性问题。

典型故障包括：

- `knowledge_status=PROJECT_DESIGN`；
- `claim_type=PROJECT_DESIGN`；
- `fact_role`、`claim_role`、`claim_status` 和 `status` 之间发生近义词或位置漂移；
- Critic 在 `PASS / ACCEPT / REJECT / BLOCK` 多套词表之间漂移；
- Stage 3 与 Stage 4 使用不同关系名称和相反连边方向；
- 历史工件仍使用旧阶段状态词表。

v3 不放宽目标 Schema，也不把任意未知词静默映射为合法值。它只执行已登记、可确定解释的兼容转换；未知漂移仍由严格 Schema 阻断。

## 2. 统一执行顺序

所有模型输出统一遵循：

```text
原始模型响应
→ JSON 结构提取
→ 路径感知枚举规范化
→ 跨字段与跨阶段适配
→ 严格 JSON Schema 校验
→ 业务质量门禁
→ 写入阶段工件
```

原始响应和规范化结果分别保留，不覆盖审计依据。

## 3. 核心模块

### `app/contract_registry.py`

统一提供：

- 核心状态词表；
- 各字段的已登记别名；
- 目标 Schema 路径感知规范化；
- 大小写、空格和连字符的确定性规范化；
- 两个枚举字段发生明确位置交换时的修复；
- Stage 3/Stage 4 关系名称和边方向适配；
- 根据目标 Schema 自动生成 Prompt 枚举约束；
- 未登记值的 unresolved 报告。

### `app/staged_contracts.py`

为 Stage 1—7 文件桥接流程提供统一入口：

- 模型请求写盘前自动注入合法枚举；
- 模型响应进行 Schema 校验前统一规范化；
- 在 `quality/contract_normalization/` 保存原始对象、规范化对象和转换报告；
- 重启或重复写请求时，枚举契约采用标记替换，避免重复追加和旧契约残留。

### `app/status_ontology.py`

保留 Stage 2、Stage 3 的来源感知迁移语义：

- 证据/来源强度进入 `knowledge_status`；
- 设计、目标、假设进入角色字段；
- 计划、预期、未知进入时间字段；
- 不把 `PROJECT_DESIGN` 粗暴统一映射为 `ESTIMATED`。

## 4. Prompt Pack 与分阶段流程接入

### 通用 Prompt Pack

`app/executor.py` 使用已展开 `$ref` 的输出 Schema：

1. 自动向系统 Prompt 注入逐路径合法枚举；
2. 在原有特殊 Prompt 修复逻辑和 Schema 校验前调用统一规范化器；
3. 将转换警告写入运行输出和 Trace。

### Stage 1—7

所有含 `ingest_*` 的阶段工具均接入：

- `normalize_in_place()`；
- `set_contract_trace_context()`；
- `prepare_staged_artifact()`。

Stage 8 只负责导出，不接收模型枚举输出，因此无需模型契约入口。

## 5. 转换原则

### 可自动转换

只有注册表中明确登记、且目标值属于当前 Schema 合法枚举的转换才会执行，例如：

```text
claim_type: PROJECT_DESIGN → PLAN
fact_role: PROJECT_DESIGN → DESIGN
claim_role: PROJECT_DESIGN → DESIGN_HYPOTHESIS
claim_status: PROJECT_DESIGN → PROJECT_PLAN
Critic verdict: PASS → ACCEPT（仅当目标 Schema 要求 ACCEPT）
```

### 可修复字段位置交换

只有两个字段各自非法、交换后双方均唯一合法时才交换，例如：

```json
{
  "knowledge_status": "PLAN",
  "claim_type": "DOCUMENT_EXTRACTED"
}
```

可确定修复为：

```json
{
  "knowledge_status": "DOCUMENT_EXTRACTED",
  "claim_type": "PLAN"
}
```

### 仍会严格失败

以下未登记的新词不会被猜测：

```text
MODEL_APPROVED_PLAN
LIKELY_CONFIRMED
AI_GENERATED_STATUS
```

它们会保留原值，写入 unresolved Trace，并由目标 Schema 拒绝。

## 6. 跨阶段关系适配

Stage 3 的：

```text
RC → OBJ, IMPLEMENTS
```

进入要求反向关系的 Stage 4 Schema 时，可确定转换为：

```text
OBJ → RC, REALIZED_BY
```

转换同时修改关系词和 `from_id/to_id`，不做仅替换字符串的错误处理。

## 7. 历史工件迁移

使用：

```bash
python scripts/migrate_contract_artifact.py \
  --input old_artifact.json \
  --schema stage2_tools/guide_fact_base.schema.json \
  --output migrated_artifact.json \
  --report migrated_artifact.trace.json
```

Stage 1—5 主工件若包含可识别的 `stage` 字段，可省略 `--schema`。

迁移报告保留：

- 原始 payload；
- 规范化 payload；
- 原始与规范化 SHA-256；
- Stage 专用迁移；
- 通用注册表转换；
- unresolved 项；
- 严格 Schema 校验结果。

默认情况下，迁移后仍不合法会返回非零状态；仅做调查时才使用 `--allow-invalid`。

## 8. `section_contracts` 与章节证据门禁修复

### 空合同问题

`P-REVISION-PLAN` 过去会直接用 `scope.target_object_ids` 过滤模型生成的章节合同。两者使用不同 ID 命名空间时，非空合同会被全部过滤为 `[]`。

v3 处理方式：

- ID 已匹配时正常过滤；
- 数量一致但 ID 命名空间不同时，按稳定顺序一一重绑，并同步改写合同间引用；
- 数量不一致时保留原始合同进入后续质量门禁，不再静默变成空数组。

### 必用证据问题

全文质量门禁过去只检查章节是否绑定了“同类型的任意证据节点”，可能绕过章节合同中的 `must_use_evidence_ids`。

v3 对创新、研究基础和成果指标章节优先检查合同指定的必用证据；仅在合同没有指定对应类型证据时，才退化为类型级检查。

## 9. 自动审计与 CI

`scripts/validate_contract_registry.py` 检查：

- Prompt Pack 和 Stage Schema 中全部 `enum/const` 路径；
- 三个核心字段是否出现非法词表；
- Prompt Pack 是否接入 Schema 规范化、Prompt 注入和展开 Schema；
- Stage 1—7 是否全部接入统一入口和 Trace；
- 已登记别名能否在真实 Schema 词表中被消费。

CI 新增统一契约门禁，并在主 CI、G0 和导出验收中运行审计。

## 10. 当前验收结果

- Prompt Pack：`PASS`；
- Schema 数量：123；
- `enum/const` 路径：826；
- 已登记别名自测：619，失败0；
- 测试收集：344；
- 通过：342；
- 跳过：2，均因交付环境没有 Git 历史/检出目录；
- 业务测试失败：0。

完整 `pytest -q` 在当前工具单次命令时限内无法一次跑完，因此按测试文件分组执行了全部344项；所有非环境跳过项均通过。
