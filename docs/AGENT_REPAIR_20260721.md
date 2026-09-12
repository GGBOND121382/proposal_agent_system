# 申请书智能体修复说明（2026-07-21）

## 1. 修复目标

本轮只修复智能体，不继续修改或生成申请书。修复范围对应上一轮暴露的三类结构性问题：

1. 创新章和结论章的关键闭环约束发现过晚；
2. 全文审查 finding 的显式修复路由会被宽泛类别覆盖；
3. 便携运行的 Trace 未形成固定、异常安全、可打包的运行证据目录。

在端到端失败路径验证中，还发现直接执行 `python scripts/run_portable_workflow.py` 时项目根目录未加入 `sys.path`，会在 Trace 初始化前因 `ModuleNotFoundError: app` 退出。本轮一并修复。

## 2. 修复内容

### 2.1 单章质量门下沉

修改文件：

- `app/proposal_quality.py`
- `app/full_integration_quality.py`

新增/强化确定性约束：

- `INNOVATION` 章节必须同时绑定：
  - `CLOSEST_PRIOR_WORK` 节点；
  - `NOVEL_MECHANISM` 节点。
- `CONCLUSION` 章节必须绑定并闭合：
  - 唯一中心命题；
  - 全部研究问题；
  - 全部创新贡献节点。

上述检查在 `P-WRITE-CONTENT` 和 `P-WRITE-CRITIC` 单章链路中即可阻断，不再等到 14 个章节全部生成后才发现。全文集成质量门仍保留同类检查，作为第二道独立保险。

### 2.2 修复路由优先级

修改文件：

- `app/full_proposal_repair.py`
- `app/workflow_authoring_base.py`

新规则：

1. finding 显式 `suggested_route` 优先；
2. 只有 `suggested_route` 缺失时，才允许根据 `category` 或 `code` 兜底推断；
3. 每个 finding 只解析出一个有效责任路由。

因此：

- `category=ARGUMENT` 且 `suggested_route=WRITING_AGENT` 的结论闭环问题，只重写结论章；
- 不会再错误返回论证架构阶段、废弃完整候选集或耗尽架构修复轮次。

同步修正基础写作 Mixin 中的遗留同名实现，避免未来改变 MRO 或直接复用基础类时旧缺陷复发。

### 2.3 Trace 持久化与异常打包

新增文件：

- `app/portable_run_trace.py`

修改文件：

- `scripts/run_portable_workflow.py`

每次便携运行现在具有固定目录：

```text
${APP_DATA_DIR}/portable_runs/<idempotency-key>/
```

也可通过 `PORTABLE_RUN_DIR` 显式指定。默认将以下证据放在同一运行树中：

- 模型原始请求、响应、解析对象及哈希；
- Chat Bridge 请求和响应；
- Human Gate 请求、响应和已消费记录；
- SQLite 一致性备份；
- 解码后的项目、文档、工作流、Gate、Prompt Run、Skill Run、Artifact 和 Audit Event；
- 每次状态迁移及每个 Gate 后的检查点；
- `RUN_METADATA.json`；
- `LATEST_STATE.json`；
- `RUN_RESULT.json`；
- `events.jsonl`；
- `TRACE_MANIFEST.json`（逐文件 SHA-256）；
- 最终 ZIP 证据包。

成功、阻断、运行时异常和工作流启动前异常都会执行 finalize。即使项目 ID 不存在，也会生成 `FAILURE.json`、结果文件、哈希清单和 ZIP 包。

### 2.4 直接运行兼容

`run_portable_workflow.py` 现在会确定性地把项目根目录加入 `sys.path`，支持：

```bash
python scripts/run_portable_workflow.py --help
```

无需 editable install，也无需调用者额外设置 `PYTHONPATH`。

## 3. 新增回归测试

新增测试覆盖：

1. 显式 `WRITING_AGENT` 路由覆盖 `ARGUMENT` 类别兜底；
2. 创新章在单章阶段检测缺少最近工作绑定；
3. 结论章在单章阶段检测缺少创新贡献绑定；
4. Trace 保存数据库快照、解码状态、Manifest 和 ZIP；
5. 脚本在任意工作目录下均可直接运行 `--help`。

## 4. 测试结果

当前测试集共 238 项。修复完成后又以一个连续的本地 pytest 进程重新执行了完整测试集，并独立执行了 5 项定向回归：

- 完整测试集：238 项收集，236 项通过，2 项按原设计跳过，0 项失败，用时 221.10 秒；
- 定向回归：5 项通过，0 项失败，用时 9.85 秒；
- `python -m compileall -q app scripts tests` 通过；
- Trace ZIP 中全部清单文件的大小与 SHA-256 复核通过。

重型 14 章并发测试已分别验证：

- 五组并发隔离与完成；
- 重启复用已完成并发组；
- 全文 finding 只重写责任章节；
- 上游修订使并发候选失效；
- 完整全文集成审查。

## 5. Trace 冒烟证据

`repair_evidence/trace_failure_smoke.zip` 是一次故意使用不存在项目 ID 的失败运行。该运行返回非零退出码，但仍生成：

- `RUN_METADATA.json`；
- `FAILURE.json`；
- `RUN_RESULT.json`；
- `events.jsonl`；
- `TRACE_MANIFEST.json`；
- 完整 ZIP 包。

该证据验证 Trace 生成不依赖申请书流程成功完成。

完整日志保存在：

- `repair_evidence/full_pytest.log`；
- `repair_evidence/targeted_tests.log`；
- `repair_evidence/collected_tests.txt`；
- `repair_evidence/verification_summary.json`。

## 6. 后续运行原则

下一次生成申请书时应从干净运行目录和新的 idempotency key 启动，不复用上一轮未持久化的临时结果。全文审查若发现局部章节问题，应由修复路由自动重写责任章节，并一直运行到全文 PASS 或明确 BLOCKED 后再汇报最终状态。
