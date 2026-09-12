# 统一状态本体与兼容迁移

## 1. 设计原则

系统不再把“证据来源”“时间状态”“项目语义角色”和“写作权限”压缩到同一个状态字段中。所有字段按语义维度独立取值，禁止跨字段复用近义枚举。

## 2. `knowledge_status`

所有名为 `knowledge_status` 的字段统一使用八值词表：

- `CONFIRMED`：具有确认权限的人或冻结工件已经确认；
- `USER_ASSERTED`：用户明确提供，尚未独立核验；
- `DOCUMENT_EXTRACTED`：从可定位文档直接抽取；
- `ESTIMATED`：模型归纳、估计或工作假设；
- `UNKNOWN`：依据不足；
- `NOT_APPLICABLE`：不适用；
- `CONFLICTED`：来源冲突未解决；
- `SUPERSEDED`：已被新版本替代。

`knowledge_status`只表达知识依据与确定程度，不表达“计划中”“预期达到”或“是否允许写入正文”。

## 3. 其他正交维度

- `temporal_status`：`PAST/CURRENT/PLANNED/EXPECTED/TIME_INDEPENDENT/UNKNOWN`；
- 阶段2 `fact_role`：`FACT/DESIGN/TARGET/ASSUMPTION/UNKNOWN`；
- `assertion_policy`：`DIRECT/QUALIFIED/PROHIBITED`；
- Prompt Pack `claim_type`：`FACT/PLAN/EXPECTED_RESULT/REQUIREMENT/PUBLIC_CLAIM/MODEL_INFERENCE`。

例如，一个已经由项目负责人确认的暂定指标应表示为：

```json
{
  "knowledge_status": "CONFIRMED",
  "fact_role": "TARGET",
  "temporal_status": "EXPECTED",
  "assertion_policy": "QUALIFIED"
}
```

`CONFIRMED`只表示“该目标作为项目目标已经被确认”，并不表示目标已经实现。

## 4. 旧值兼容

运行时只对已知旧值执行确定性迁移：

- `PROJECT_DESIGN`、`CONFIRMED_DESIGN`、误放入知识状态的`PLANNED` → 根据来源映射为`CONFIRMED/USER_ASSERTED/DOCUMENT_EXTRACTED/ESTIMATED`，并设置设计与计划语义；
- `PROVISIONAL_TARGET` → 根据来源映射知识状态，同时设置`TARGET + EXPECTED + QUALIFIED`；
- `WORKING_ASSUMPTION` → 用户明确提供时为`USER_ASSERTED`，否则为`ESTIMATED`，并设置`ASSUMPTION + QUALIFIED`。

任意未登记的新状态不会被猜测映射，仍由严格Schema阻断。原始模型响应、规范化结果和转换报告均保留在Trace中。

## 5. 来源感知映射

确定性映射依次检查：

1. 人工确认或已接受冻结工件 → `CONFIRMED`；
2. 可定位正式/技术/公开文档 → `DOCUMENT_EXTRACTED`；
3. 用户明确输入 → `USER_ASSERTED`；
4. 模型推断或无直接来源的工作假设 → `ESTIMATED`；
5. 缺失来源且内容本身未知 → `UNKNOWN`。

## 6. 防回归

- Prompt共享规则显式列出合法状态和字段分工；
- 阶段2 Schema 1.1使用统一八值词表；
- 通用Prompt运行时在Schema校验前执行已知别名转换；
- 阶段2和阶段3保存原始候选与规范化候选；
- 自动测试扫描所有Schema中名为`knowledge_status`的字段，禁止出现统一词表之外的值；
- 测试覆盖MiniMax式近义枚举漂移、来源感知映射和未知新枚举严格失败。
