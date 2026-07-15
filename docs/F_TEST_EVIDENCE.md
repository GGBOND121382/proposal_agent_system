# F 轨道：测试、CI、证据与可恢复交付

> 分支：`agent/test-evidence`  
> 对应计划：`docs/CONCURRENT_DEVELOPMENT_PLAN.md` 的 F1—F6  
> 边界：SIMULATED/REPLAY 只用于编排、Schema、审计与恢复回归，不作为真实模型语义能力证明。

## 1. 交付物

| 工作包 | 可执行交付物 | 验收方式 |
|---|---|---|
| F1 单 Agent 测试矩阵 | `tests/test_f_agent_matrix.py` | 30 个注册 Prompt 分别执行 3 个正向、5 个负向、1 个边界和 1 个重载测试 |
| F2 工作流故障注入 | `tests/test_f_workflow_recovery.py` | 在启动、Prompt/Gate 落库、等待 Gate、Gate 决策和继续执行等持久化边界重建运行时；Prompt、Artifact、Gate、审计数量和终态一致 |
| F3 Prompt Pack 一致性 | `governance/f/test_evidence_manifest.json`、`scripts/validate_f.py` | Prompt、Schema、Replay、Workflow、Gate、CI Job 均从注册表/冻结契约核验；漂移使 CI 失败 |
| F4 Trace 审计 | `scripts/audit_prompt_traces.py` | Prompt Run 与 Trace 一一配对；复算输入/输出哈希，核验原始响应、解析对象、状态、耗时、模型、端点和责任 Agent |
| F5 阶段恢复包 | `scripts/f_recovery_bundle.py` | ZIP 含源码、环境、材料、SQLite 快照、请求、响应、Trace、日志和验收报告；逐文件 SHA-256、Zip Slip 和 SQLite 完整性检查 |
| F6 CI 并发矩阵 | `.github/workflows/f-track.yml` | 7 个组件 Job 并行；3 个小型 E2E 仅在所有组件 Job 通过后运行 |

## 2. 本地验收

```bash
python -m pip install -r requirements-dev.txt
python scripts/validate_g0.py
python scripts/validate_f.py
python prompt_pack/tools/validate_pack.py
python -m pytest -q tests/test_f_agent_matrix.py
python -m pytest -q tests/test_f_workflow_recovery.py
python -m pytest -q tests/test_f_trace_recovery.py
```

生成可审计调用和阶段恢复包：

```bash
OUT=recovery_evidence/f/local
mkdir -p "$OUT"
python scripts/create_f_trace_smoke.py \
  --database "$OUT/runtime/proposal_agents.sqlite3"
python scripts/audit_prompt_traces.py \
  --database "$OUT/runtime/proposal_agents.sqlite3" \
  --output-dir "$OUT/prompt_traces"
python scripts/f_recovery_bundle.py build \
  --database "$OUT/runtime/proposal_agents.sqlite3" \
  --output "$OUT/f-recovery.zip"
python scripts/f_recovery_bundle.py verify \
  "$OUT/f-recovery.zip" \
  --extract-dir /tmp/f-restored
```

## 3. Agent 测试矩阵

每个注册 Prompt 固定执行以下矩阵：

- 正向：`normal`、`high_risk`、`need_user_input`；
- 负向：`missing_input`、`schema_error`、输入 `prompt_id` 错配、输出非法状态、输出 `prompt_id` 错配；
- 边界：`need_user_input` Fixture 的期望状态与实际输出状态一致；
- 重启：重新加载 Prompt Pack 后 Prompt 元数据、正文、输入/输出 Schema 和 Replay 完全一致。

该矩阵由 `prompt_registry.json` 驱动，新增 Agent 未补齐五类 Replay 时，`validate_f.py` 直接失败。

## 4. 故障恢复边界

F2 不修改生产状态机，也不伪造内存恢复。测试在已提交到 SQLite 的边界销毁并重建 `Settings`、`PromptPack`、`Database`、`PromptExecutor` 和 `WorkflowEngine`：

1. 工作流创建后；
2. Prompt 结果与 Trace 提交后；
3. `WAITING_GATE` 且 OPEN Gate 已提交后；
4. Gate 决策提交后；
5. 下一次 `advance` 前。

等待 Gate 时重复调用 `advance` 必须幂等，不得新增 Prompt Run、Artifact、Gate 或审计对象。请求发送前/后和事务内部的强制退出 Hook 属于轨道 A；F 轨道提供持久化边界回归与一致性判定基准。

## 5. Trace 审计规则

`audit_prompt_traces.py` 不只检查数量。它会：

1. 按项目、工作流、Prompt 和输入哈希匹配 `prompt_runs` 与 `PROMPT_TRACE`；
2. 按统一 canonical JSON 规则复算输入/输出哈希；
3. 核验 Trace 的 `context_hash`、输入、解析输出、状态、耗时、模型和端点；
4. 检查非错误调用的原始响应文本；
5. 从 Prompt 注册表写入 `responsibility_agent`；
6. 阻断缺失 Trace 和孤立 Trace；
7. 输出逐调用 `calls.jsonl` 和汇总 `trace_audit.json`。

## 6. CI 依赖关系

并行组件 Job：

```text
lint-and-schema
runtime-recovery-tests
agent-prompt-tests
research-skill-tests
mermaid-export-tests
quality-validation-tests
trace-and-recovery-tests
```

全部通过后才启动：

```text
small-e2e-single-section
small-e2e-three-sections
small-e2e-research-mermaid-export
```

`trace-and-recovery-tests` 上传完整 F 证据 Artifact，保留 14 天。正式 LIVE 能力验收仍需固定材料、固定模型配置、真实端点和人工启动 Gate，不纳入每次普通提交的自动 CI。
