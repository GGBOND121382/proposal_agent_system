# 列表型产物自动修复缺陷修复 v4

基线：已应用 `unified_model_output_contract_v3_for_latest_20260723.patch` 的工程。

## 修复问题

`P-FACT-EXTRACT` 的 `fact_candidates` 是列表。旧版 `WorkflowRepairMixin._auto_repair()` 把生产者产物一律当作字典，执行 `original.get(...)`，导致：

```text
AttributeError: 'list' object has no attribute 'get'
```

## 修复范围

1. 对字典型和列表型生产者产物分别适配；列表按结果字段名包装，如 `{"fact_candidates": [...]}`。
2. 将 Critic 给出的索引、对象 ID、JSON Pointer 或点路径统一为定向修复路径。
3. 定向修复结束后恢复原始产物形状；`fact_candidates` 仍写回列表，不污染下游接口。
4. 扩展 `P-TARGETED-REPAIR` 的范围保护，使列表集合只允许修改 Critic 指定的对象和字段；模型擅自改写其他字段、添加或删除条目时会被确定性回滚。
5. 补齐已登记生产者角色与 targeted-repair 输入 Schema 的一致性。
6. 增加 `P-FACT-CRITIC → REVISE → P-TARGETED-REPAIR → 列表覆盖` 回归测试。

## 验证结果

- 补丁在 v3 完整工程干净副本上：`git apply --check` 通过。
- Prompt Pack 校验：PASS。
- 统一契约注册表校验：PASS。
- 状态契约校验：PASS。
- 列表型修复与作用域测试：7 passed。
- 相关自动修复与恢复测试：19 passed。
- v3 状态与迁移测试：25 passed。

## 应用

```powershell
git apply --check .\workflow_repair_list_artifact_fix_v4.patch
git apply --whitespace=nowarn .\workflow_repair_list_artifact_fix_v4.patch
```

补丁适用于已经应用 v3 统一契约补丁的分支。
