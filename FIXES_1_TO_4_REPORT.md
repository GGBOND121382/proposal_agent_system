# 问题 1–4 修复报告

## 1. Stage 流水线通用化

- 新增 `app/staged_workflow_config.py`，统一解析项目标题、章节顺序、批次章节、页数预算和安全输出文件名。
- Stage 6A–6D 从 Stage 5 `draft_batches` 动态读取章节，不再固定 `SEC-01`～`SEC-14`。
- Stage 7 从章节合同动态汇总章节数量、标题、页数和全文覆盖集合。
- Stage 8 动态生成标题、章节编号和输出文件名。
- 批次 Critic Schema 允许模板定义的任意章节集合。
- 计划、风险、结论等质量规则从固定编号判断改为按章节合同/章节名称识别。

## 2. 两套工作流统一入口

- 新增 `WF-STAGED_PROPOSAL` 工作流类型。
- 新增 `StagedWorkflowCoordinator`，把 Stage 1–8 文件桥流水线注册到主 SQLite `workflows` 表。
- 新增 `UnifiedWorkflowEngine`，通过同一 `start/advance/get` 接口分发数据库原生工作流和文件化 Stage 工作流。
- FastAPI `/api/workflow-types` 同时公布两类工作流。
- 新增 `/api/workflows/{workflow_id}/staged-files` 查询当前 Stage 请求、门禁和输出文件。

## 3. 移除导入时猴子补丁

- `app/__init__.py` 不再替换兄弟模块中的基础类。
- 新增 `app/runtime_factory.py` 显式组装生产运行时。
- 新增 `app/runtime_api.py` 提供兼容导入别名。
- `app/runtime_bootstrap.py` 保留为不修改类的弃用兼容钩子。
- `app/main.py` 通过工厂构建运行时依赖。

## 4. 文档、版本和统计同步

- 新增 `app/version.py`，FastAPI 版本从 `pyproject.toml` 单一来源读取。
- 新增 `scripts/sync_project_metadata.py`，自动同步 README、开发状态和构建报告中的 Prompt、Schema、Replay、测试数量。
- 更新 G0 校验器，使其能校验 `FastAPI(version=__version__)`。
- 当前统计：版本 0.6.0、30 个 Prompt、89 个 Prompt Schema、34 个 Stage Schema、150 个 Replay、371 项 pytest 用例。

## 验证结果

- Python 静态编译：通过。
- Stage 1–8 原有专项测试：84/84 通过。
- 新增通用化、运行时装配、统一工作流测试：6/6 通过。
- G0、运行时装配与统一工作流组合：7 通过、2 跳过。
- 元数据同步 `--check`：通过。
- 当前容器为 Python 3.13.5 + pytest 9.0.2；工程声明 Python 3.12、pytest 8.x。全量 371 项单进程运行在当前非声明环境中存在顺序相关停滞，相关停留用例单独运行通过。建议在项目 CI 规定环境中完成最终全量验收。
