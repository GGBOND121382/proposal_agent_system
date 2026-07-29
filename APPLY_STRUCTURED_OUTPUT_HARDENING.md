# 结构化输出契约加固应用说明

## 重要原则

本补丁用于替换程序代码、Prompt Pack 和测试，不用于替换现有运行状态。

应用时必须保留当前实例中的：

- `.env`；
- `data/proposal_agents.sqlite3`；
- `data/model_calls/`；
- `data/uploads/`；
- `data/exports/` 与 `data/runtime_exports/`；
- 其他真实项目材料和恢复证据。

交付 ZIP 已排除上述可变运行时文件，避免覆盖当前 blocked 工作流及 `run-939e847628be457d` 的历史输出。

## 建议步骤

1. 停止当前服务，并备份整个工程目录和 SQLite 数据库。
2. 将交付 ZIP 解压到新目录。
3. 把当前实例的 `.env`、`data/proposal_agents.sqlite3`、`data/model_calls/`、上传材料与导出目录复制或挂载到新目录。
4. 使用工程声明环境安装依赖：Python 3.12、pytest 8.x。
5. 执行：

   ```bash
   python scripts/sync_project_metadata.py --check
   python scripts/validate_contract_registry.py
   python prompt_pack/tools/validate_pack.py
   pytest -q
   ```

6. 启动服务，继续推进原工作流 `wf-f1f90c80a90b4bb9`。

若数据库、工作流 ID、Prompt 输入哈希及历史失败输出均保持不变，执行器会优先重新消费 `run-939e847628be457d` 的 provider output，完成字段归属修复和完整 Schema 重验，不应再次调用 MiniMax。

## 预期恢复标记

成功恢复后，新的 Prompt Trace 应包含：

```text
recovery_kind = CONTRACT_RENORMALIZATION
recovered_from_run_id = run-939e847628be457d
SYSTEM_FIELD_OWNERSHIP_NORMALIZATION
```

恢复后的对象应满足：

```text
result.source_fact_exclusions 存在
result.template.source_fact_exclusions 不存在
```

如果历史错误输出没有真正写入 SQLite，或输入已经变化，系统会明确报告无法本地恢复并走正常模型调用；不会伪装成恢复成功。
