# 统一状态本体

所有名为 `knowledge_status` 的字段只能使用以下八个值：

- `CONFIRMED`：已经由具有确认权限的人或冻结工件明确确认；
- `USER_ASSERTED`：用户明确提供，但尚未经过独立核验；
- `DOCUMENT_EXTRACTED`：可从已绑定文档原文直接抽取；
- `ESTIMATED`：模型归纳、估计或工作假设，尚未由用户确认；
- `UNKNOWN`：当前没有足够依据；
- `NOT_APPLICABLE`：该字段在当前对象上不适用；
- `CONFLICTED`：高权威来源之间存在未解决冲突；
- `SUPERSEDED`：已被更新版本替代。

禁止在 `knowledge_status` 中创造或复用 `PROJECT_DESIGN`、`CONFIRMED_DESIGN`、`PROVISIONAL_TARGET`、`WORKING_ASSUMPTION`、`PLANNED` 等值。这些词表达的是其他语义维度：

- 项目设计或计划：使用 `claim_type=PLAN` 与 `temporal_status=PLANNED`；
- 暂定目标或预期成果：使用 `claim_type=EXPECTED_RESULT` 与 `temporal_status=EXPECTED`，并保留限定语；
- 工作假设：使用 `knowledge_status=ESTIMATED`，必要时使用 `claim_type=MODEL_INFERENCE`；
- 已完成、计划中、预期发生等时间含义只允许写入 `temporal_status`；
- 可直接写、须限定、禁止写等权限只允许写入对应写作权限字段。

状态依据必须来自 `source_refs`：用户确认优先于普通用户陈述，文档抽取必须有可定位文档引用，模型归纳不得升级为确认状态。无法确定时使用 `UNKNOWN`，不得创造近义枚举。
