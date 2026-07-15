# G0 冻结契约变更记录

## 2026-07-15 — G0 初始冻结

- 代码基线：`ac7e3032a51c682c6bee6e2461d3393cc14835d7`；
- 产品版本：`0.6.0`；
- Prompt 注册表：30 个 Prompt，注册表版本 `2.0`；
- 固定五条工作流、Gate 角色、Critic/Producer 映射和 SQLite/Artifact 接口；
- 安全链路无批准变更；
- 修复 `docker-compose.yml` 镜像标签从 `0.5.0-offline` 漂移到 `0.6.0-offline`；
- 新增 G0 契约校验、恢复包生成/校验、回归测试和专用 CI Gate。

后续修改冻结路径时，必须在对应 JSON 契约的 `approved_changes` 中登记精确路径、Git blob SHA、所有者、原因和审批引用，并在本文件新增记录。
