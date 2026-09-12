# WF-3 TODO：知识图谱驱动的 Prompt Contract 自动生成/校验

> 状态：**暂缓实现**。当前优先级是先完成现有 WF-3 的真实 LIVE 闭环验证，不在本轮继续扩大基础设施改造范围。

## 背景

WF-3 已建立知识对象与依赖关系的显式图谱。长期目标是不再人工维护每个 Prompt 的 provider-facing 输入 Schema，而是从知识图谱中目标输出对象的**最近直接语义父节点**自动生成或静态校验模型输入契约。

允许形成模型输入依赖的边仅包括：

- `SEMANTIC_CAUSE`
- `CONSTRAINT`
- `EVIDENCE`

默认禁止作为模型推理输入的边包括：

- `DETERMINISTIC`
- `TRACE`
- `CONTROL`

生成型 Prompt 的理想输入：

```text
I*(O) = Parents_{SEMANTIC_CAUSE | CONSTRAINT | EVIDENCE}(O)
```

Critic 的理想输入：

```text
ReviewedObject + DecisionCriteria + Evidence
```

只取最近有效父节点，不自动展开全部祖先，避免祖先重复和上下文膨胀。

## 后续 TODO

- [ ] 将 WF-3 知识对象、owner、object class 与六类边固化为 `contracts/wf3_knowledge_graph.yaml` 或等价的类型化契约。
- [ ] 为每个模型节点登记 `produces`、`semantic_inputs`、`optional_inputs` 与禁止输入类别。
- [ ] 从目标输出对象的直接父节点自动生成 provider-facing input schema，或至少由静态检查器验证人工 schema 与图谱一致。
- [ ] 静态检查 **缺失依赖**：目标输出需要的直接 `SEMANTIC_CAUSE / CONSTRAINT / EVIDENCE` 父节点未进入模型输入。
- [ ] 静态检查 **冗余依赖**：输入包含目标输出的兄弟、后代、已被直接父节点封装的祖先，或重复表达同一语义对象。
- [ ] 静态检查 **控制污染**：`DETERMINISTIC / TRACE / CONTROL` 对象不得进入 provider-facing model input。
- [ ] 静态检查 **输出越权**：model output schema 不得要求模型生成 Runtime-owned 的 ID、Hash、TTL、路径、severity、blocking、route、status、Gate 等字段。
- [ ] 增加“同一语义对象多份拷贝”的检测，例如批准边界经过模型重写后又作为下游约束使用。
- [ ] 将检查器接入 CI；任何 Prompt 输入新增字段必须能在知识图谱中找到到目标输出的合法直接依赖边。
- [ ] 在此机制稳定后，再评估由图谱自动生成 model-level JSON Schema；**canonical/persistence schema 不随之自动修改**。

## 暂不实施的原因

当前 WF-3 刚完成 provider-facing SEMANTIC contract 与运行时 expander 的边界重构。立即叠加知识图谱驱动 Schema 生成会同时改变：

1. Prompt 输入来源；
2. model-level Schema 生成方式；
3. 静态契约验证；
4. CI 基础设施。

这会扩大真实 API 验收时的故障归因面。因此当前策略是：

```text
先用知识图谱人工/静态审计现有六个 Prompt
        ↓
修复会影响正确性的最小问题
        ↓
真实 LIVE 闭环通过
        ↓
再实现知识图谱驱动 Prompt Contract 自动化
```

## 未来验收条件

- 同一 Prompt 的人工 schema 与图谱派生 schema 完全一致，或差异均有显式豁免理由；
- 新增 Runtime/Trace/Control 字段无法进入模型输入；
- 删除任一必要直接父节点时 CI 必须失败；
- 加入无依赖字段时 CI 必须失败或要求显式 justification；
- provider request 长度相对当前手工精简版不出现无原因增长；
- canonical 输出和后续 WF-4 接口保持兼容。
