# 轨道 A：运行时、真实模型调用与断点恢复验收

> 分支：`agent/runtime-recovery`  
> 范围：A1—A6  
> 安全边界：未修改安全分类、密级映射、外发 Gate、审批状态和 WF-1/WF-3/WF-5 安全步骤。

## 1. 交付映射

| 工作包 | 实现 | 可执行证据 |
|---|---|---|
| A1 LIVE Gateway | `app/runtime_gateway.py`、`app/runtime_evidence.py` | 每次调用在 `model_calls/requests` 与 `model_calls/responses` 保存完整系统 Prompt、输入 Envelope、输出 Schema、原始响应、解析对象、模型/端点、时间戳和 SHA-256 |
| A2 无 Replay 上下文 | `app/runtime_context.py` | LIVE 模式从 JSON Schema 生成空脚手架，再注入数据库项目、材料和前序工件；不调用 `replay_input`；未替换的必填脚手架字段触发 `LiveContextBlocked` |
| A3 原始响应保护 | `ModelCallEvidenceStore`、`RuntimePromptExecutor` | 原始文本哈希、原始 JSON 对象哈希、Gateway 解析对象哈希和消费对象哈希分别记录；不一致拒绝消费；能力验收模式禁止质量后处理改写模型对象 |
| A4 幂等恢复 | `app/runtime_executor.py`、`app/runtime_workflows.py` | 确定性 `call_key`、单事务写入 Run/Output/Trace/Commit 事件；事务后中断时复用已提交结果；WAITING_GATE 无开放 Gate、可恢复 BLOCKED 和遗留 RUNNING 均从原步骤继续 |
| A5 故障注入 | `FaultInjector`、`app/runtime_export.py`、`scripts/run_runtime_fault_matrix.py` | 请求落盘前后、模型请求前、响应落盘后、数据库事务前后、Critic、Repair、Gate、Export 均有一次性持久化故障点；支持异常和 `os._exit` 两种动作 |
| A6 能力验收硬门 | `app/runtime_policy.py`、`scripts/capability_runtime_acceptance.py` | `CAPABILITY_ACCEPTANCE_MODE=true` 时只允许 LIVE；拒绝 REPLAY/MOCK/SIMULATED、recorded/replay 检索、自动响应器、样例章节回退和未由 LIVE Context Builder 证明的输入 |

## 2. 证据目录

默认目录可通过环境变量覆盖：

```text
${APP_DATA_DIR}/model_calls/
├── requests/
│   ├── <call_key>.json
│   └── <call_key>.meta.json
├── responses/
│   ├── <call_key>.raw.txt
│   ├── <call_key>.parsed.json
│   └── <call_key>.meta.json
├── commits/
│   └── <call_key>.json
└── fault_markers/
    └── <call_key>.<fault_point>.fired
```

请求和响应文件使用临时文件、`fsync` 和原子替换落盘。已有文件只能在哈希完全一致时复用；部分文件、哈希变化或原始 JSON 与解析对象不一致均阻断。

## 3. 恢复语义

1. **请求落盘后、模型调用前中断**：重启后读取同一请求证据并继续调用。
2. **响应落盘后、数据库事务前中断**：重启后校验并复用原始响应，不再次请求模型。
3. **数据库事务提交后、步骤推进前中断**：重启后通过 `MODEL_CALL_COMMITTED` 和 `call_key` 复用唯一 Run，不重复生成 Output/Trace。
4. **Gate 创建后中断**：开放 Gate 已具有幂等唯一性；工作流保持 `WAITING_GATE`。
5. **Gate 已决策但状态遗留为 WAITING_GATE**：发现无开放 Gate 后恢复为 `RUNNING`。
6. **运行时故障导致 BLOCKED**：仅带 `runtime_recoverable=true` 的技术故障可恢复；事实缺失、质量失败、安全拒绝等业务阻断不会被自动绕过。
7. **Export 完成后中断**：按项目、候选 Run 和最终 Gate 状态计算导出键，验证文件 SHA-256 后复用成品。

## 4. 能力验收环境

```bash
export MODEL_RUNTIME_MODE=LIVE
export CAPABILITY_ACCEPTANCE_MODE=true
export PUBLIC_SEARCH_PROVIDER=disabled   # 或真实在线提供方，禁止 recorded/replay
export MODEL_RESPONSE_AUTOMATION=false
export SAMPLE_SECTION_FALLBACK=false
export AUTO_RESPONSE_ENABLED=false

python scripts/capability_runtime_acceptance.py
```

能力验收模式还要求 Prompt 输入由 LIVE Context Builder 从持久化材料构建。直接提交 Replay Case、样例 Envelope 或外部拼装输入会被拒绝。

## 5. 本地验证

```bash
python -m compileall app scripts
python scripts/capability_runtime_acceptance.py --self-test
python scripts/run_runtime_fault_matrix.py --self-test
python -m pytest -q tests/test_runtime_recovery.py
python -m pytest -q
```

## 6. 验收结论

A1—A6 的代码路径、负向硬门、持久化证据、一次性故障点和恢复规则均已落库。正式 LIVE 能力质量仍必须使用真实模型端点和真实材料执行；本轨道不把 Replay、模拟器或固定响应作为语义质量证明。
