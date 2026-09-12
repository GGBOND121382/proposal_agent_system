# 工作流输入完整性加固

日期：2026-07-29

工程版本：0.6.0

## 1. 变更目的

本次变更修复工作流启动参数、项目材料和前序工件不足时产生的技术性阻断、错误材料替代及跨工作流工件污染。系统不再使用 Replay 示例、未确认工件或角色不匹配的材料填补真实业务输入。

## 2. 状态机扩展

新增 `WAITING_PREREQUISITE`，用于表示工作流尚未具备已完成的前置工作流。该状态不能被技术重试改回 `RUNNING`；前置条件满足后，系统冻结具体前序工作流 ID，再恢复执行。

新增以下输入 Gate：

- `PUBLIC_RESEARCH_NEED_INPUT`；
- `PROJECT_MATERIAL_INPUT`；
- `APPLICATION_GUIDE_INPUT`；
- `REFERENCE_TEMPLATE_INPUT`；
- `CURRENT_PROPOSAL_INPUT`。

这些 Gate 只接受 `PROVIDE_INFORMATION`、`RETURN` 或 `CANCEL`，不能代替安全审批。

## 3. 输入与工件约束

- 工作流步骤只读取当前工作流、显式父工作流及启动时冻结的前置工作流中的可用工件；
- `BLOCK`、`ERROR`、未完成工作流及无关并发工作流的工件不得进入上下文；
- 未经 `ONLINE_RESULT_IMPORT_APPROVAL` 明确接受的公开研究结论不得进入申请书写作；
- 申报指南、参考申请书和当前申请书必须由正确材料角色提供，不再使用任意项目材料静默替代；
- 用户在信息 Gate 中提交的答案会形成可追溯的 `human_resolutions`，并重新执行原步骤，而不是直接跳过该步骤。

## 4. 安全不变量

本次变更没有改变：

- `OUTBOUND_SECURITY_APPROVAL`、`ONLINE_RESULT_IMPORT_APPROVAL`、`FINAL_CONTENT_SECURITY_APPROVAL` 和 `FINAL_EXPORT_APPROVAL` 的责任角色；
- 安全 Gate 的 `APPROVE`、`RETURN`、`REJECT`、`CANCEL` 动作；
- WF-1 安全分类、WF-3 外发净化及审批、WF-5 最终保密审查的顺序；
- 离线模型和公开在线模型的路由边界。

新增输入 Gate 位于业务输入准备层，不能批准外发、导入公开结果或最终导出。

## 5. 验证范围

回归覆盖：

- WF-3 后续所有 LIVE 输入装配；
- 前置条件等待及旧工作流迁移；
- 工件状态、工作流归属和冻结绑定；
- 公开结论导入审批；
- 通用信息 Gate 答案回填与原步骤重跑；
- 材料角色错误时的专用输入 Gate；
- 五条工作流在严格 LIVE 上下文装配下的完整执行。
