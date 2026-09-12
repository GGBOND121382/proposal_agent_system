# WF-3 输入上下文与用户门禁修复

日期：2026-07-29

## 问题

`WF-3_HYBRID_ONLINE_ASSIST` 在 `P-SAFE-ONLINE-PACKAGE` 调用模型前，可能因以下必填字段仍为 LIVE Schema 占位值而直接 `BLOCKED`：

- `payload.research_need.*`
- `payload.source_items`
- `payload.target_task_type`

旧实现既未从 WF-1 结果推导，也未把启动参数映射至这些字段，更没有在不可推导时创建可填写的用户门禁。

## 修复

1. 新增 `app/wf3_input.py`，统一解析显式启动参数、从已确认论证图推导公开研究需求，并生成稳定的 `need_id`。
2. `ContextBuilder` 为 `P-SAFE-ONLINE-PACKAGE` 注入 `research_need`、来源对象引用和任务类型。
3. 无法可靠推导研究问题时，抛出业务输入请求并创建 `PUBLIC_RESEARCH_NEED_INPUT` 门禁，而不是技术性 `BLOCKED`。
4. 门禁答案会写回 `workflow.state.options`，保持当前步骤不变，再次推进时使用真实输入构造上下文。
5. 旧版因 `LiveContextBlocked` 停在 WF-3 第 0 步的工作流可直接迁移恢复，即使技术重试次数已耗尽。
6. 前端支持：
   - 启动 WF-3 时可选填研究问题、联网原因、期望输出和任务类型；
   - 人工门禁显示并提交问题答案；
   - 新建项目时单独确认“允许联网”和“允许脱敏后外发”。
7. 新增路径级审计事件：`WORKFLOW_INPUT_REQUIRED`、`WF3_RESEARCH_NEED_PROVIDED`。

## 现有工作流恢复

应用补丁并重启服务后，对原工作流执行一次“继续”。

- 若 WF-1 已产生可用的研究问题/研究差距，系统会自动推导并继续执行；
- 否则工作流进入 `WAITING_GATE`，在 `PUBLIC_RESEARCH_NEED_INPUT` 门禁中填写研究问题后继续；
- 不需要删除工作流、修改 SQLite 或重新执行 WF-1。

## 验证

- 新增 WF-3 专项测试：5/5 通过；
- `tests/test_runtime.py`：49/49 通过；
- Runtime recovery + v0.3/v0.4 + WF-3 专项：21/21 通过；
- Prompt Pack 校验：PASS；
- Python 静态编译与前端 JavaScript 语法检查：PASS。
