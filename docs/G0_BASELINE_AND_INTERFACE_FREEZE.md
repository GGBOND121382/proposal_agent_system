# G0 基线与接口冻结

> Gate：G0  
> 产品版本：`0.6.0`  
> 代码基线：`ac7e3032a51c682c6bee6e2461d3393cc14835d7`  
> 治理计划提交：`29f988cd9b561428ee4a33f023c6a47d4457673b`

## 1. G0 交付物

G0 不以说明文档作为唯一证据。仓库同时提供以下可执行交付物：

- `governance/g0/baseline.json`：唯一代码基线、产品版本和基线命令；
- `governance/g0/interface_contract.json`：Prompt/Schema、Agent 责任、状态机、Gate、SQLite 与 Artifact 接口；
- `governance/g0/security_freeze.json`：安全分类、外发保护、路由、审批 Gate 和安全工作流冻结范围；
- `governance/g0/layout.json`：Git、SQLite、材料、Trace 和恢复包目录规范；
- `scripts/validate_g0.py`：版本一致性、接口语义、目录、Git 冻结范围和安全不变量校验；
- `scripts/build_g0_recovery_bundle.py`：生成源码、依赖声明、材料清单、SQLite 快照和 Trace 导出的恢复包；
- `scripts/verify_g0_recovery_bundle.py`：逐文件哈希验证并安全解包；
- `tests/test_g0_contract.py`：冻结契约与恢复包回归测试；
- `.github/workflows/g0.yml`：在全新 Python 3.12 环境中恢复依赖、源码、材料和基线测试。

## 2. 唯一版本

以下三处必须同时为 `0.6.0`：

1. `pyproject.toml` 的 `project.version`；
2. `app/main.py` 的 `FastAPI(version=...)`；
3. `docker-compose.yml` 的镜像标签 `proposal-agent:0.6.0-offline`。

`validate_g0.py` 会阻断任一来源发生漂移。

## 3. Prompt、Schema 与 Agent 责任冻结

`interface_contract.json` 固定 30 个 Prompt 的以下字段：

- `prompt_id` 与 `prompt_version`；
- Prompt 文件、输入 Schema 和输出 Schema；
- 模型 Profile、执行环境、执行 Agent；
- 后续人工 Gate。

统一输入、输出 Envelope 必须各包含 30 个 Prompt Schema 引用。所有注册文件必须存在。冻结路径相对代码基线发生变化时，必须在 `approved_changes` 中登记：

- 精确仓库路径；
- 当前 Git blob SHA；
- 所有者；
- 变更原因；
- 审批引用。

未登记变更、哈希不一致或无效审批记录都会使 G0 失败。

## 4. 状态机与 Artifact 接口冻结

状态机契约固定：

- 五条工作流的步骤顺序；
- Workflow 状态：`RUNNING`、`WAITING_GATE`、`BLOCKED`、`COMPLETED`、`CANCELLED`；
- Gate 状态：`OPEN`、`APPROVED`、`REJECTED`、`CANCELLED`；
- Gate 责任角色和安全 Gate 动作；
- Critic 与 Producer 的定向修复映射。

SQLite 契约固定全部业务表的列集合。Artifact 至少保留：

- `PROMPT_OUTPUT`；
- `PROMPT_TRACE`。

Trace 必须包含 Prompt、模型、端点、完整输入、质量上下文、输出 Schema、原始响应、解析输出、状态、耗时和错误字段。

## 5. 安全链路冻结

安全冻结覆盖：

- 安全分类与密级集合；
- 离线、公开在线和原生产者修复环境；
- 外发净化与在线路由；
- `WF-1` 安全分类前缀；
- `WF-3` 外发审批前缀；
- `WF-5` 最终保密审查；
- 安全 Gate 的角色、动作和状态。

普通功能轨道不得修改上述路径。专门安全任务必须同时更新安全契约并登记审批证据，禁止用业务功能修改顺带改变安全行为。

## 6. 目录规范

### Git

- 默认分支：`main`；
- 六条开发轨道和集成分支名称见 `governance/g0/layout.json`；
- 每条轨道使用独立 worktree；
- 一个提交只解决一个可验证问题，并同时包含测试或证据。

### SQLite

- 主库：`${APP_DATA_DIR}/proposal_agents.sqlite3`；
- WAL：`${APP_DATA_DIR}/proposal_agents.sqlite3-wal`；
- SHM：`${APP_DATA_DIR}/proposal_agents.sqlite3-shm`。

恢复包使用 SQLite backup API 生成一致性快照，不直接复制正在写入的数据库文件。

### Trace

运行时 Trace 存储在 SQLite `artifacts` 表，`artifact_type='PROMPT_TRACE'`。恢复包额外导出为：

`trace/prompt_traces.jsonl`

### 恢复包

推荐目录：

`recovery_evidence/g0/<commit>/g0-recovery-<commit>.zip`

生成的 ZIP 包含源码归档、依赖声明、材料清单、SQLite 快照、Trace 导出、冻结契约、逐文件 SHA-256 和恢复说明。

## 7. 本地验收

```bash
python -m pip install -r requirements-dev.txt
python scripts/validate_g0.py
python -m compileall app
python prompt_pack/tools/validate_pack.py
python -m pytest -q

python scripts/build_g0_recovery_bundle.py \
  --output recovery_evidence/g0/local/g0-recovery.zip
python scripts/verify_g0_recovery_bundle.py \
  recovery_evidence/g0/local/g0-recovery.zip \
  --extract-dir /tmp/g0-restored
```

## 8. G0 通过判定

G0 只有在专用 GitHub Actions 工作流同时完成以下步骤后通过：

1. 冻结契约校验；
2. 应用编译、Prompt Pack 校验和完整 pytest；
3. 构建并逐文件验证恢复包；
4. 在全新目录和全新虚拟环境中安装依赖；
5. 从源码归档恢复材料并重新执行基线测试；
6. 上传恢复包和验证报告作为工作流 Artifact。

任何步骤失败均不得进入 G1。
