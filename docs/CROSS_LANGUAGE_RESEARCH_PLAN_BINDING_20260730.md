# 跨语言公开研究计划绑定修复

日期：2026-07-30

## 问题

旧版公开研究计划把 `queries` 表示为字符串数组，并通过查询文本与研究问题之间的关键词重合度判断绑定关系。研究问题为中文、查询为英文时，即使查询在语义上正确，也会被误判为 `RESEARCH_PLAN_UNBOUND_QUERY`。该计划契约错误还可能因为发生在 `PUBLIC_SEARCH` 步骤而被误分类为 `WAITING_CONFIGURATION`。

## 新契约

新生成的 `P-PUBLIC-RESEARCH-PLAN` 输出使用显式结构：

```json
{
  "binding_contract_version": "1.0",
  "research_questions": ["..."],
  "queries": [
    {
      "query_id": "query-001",
      "query": "English search query",
      "linked_question_indexes": [0]
    }
  ]
}
```

查询与研究问题可以使用不同语言；绑定只依据 `linked_question_indexes`，不再依赖同语言关键词重合。

## 旧工作流迁移

已持久化的 2.0 计划仍可直接执行：

- 单一研究问题的查询确定性绑定到下标 `0`；
- 多研究问题计划若能得到同语言词法信号，仅将其作为迁移辅助；
- 跨语言且无法确定单个问题时，查询保留在已经批准的计划总体范围内，记录 `RESEARCH_PLAN_LEGACY_BINDING_UNVERIFIED` 警告，但不再误报未绑定；
- 不重新调用规划模型，不需要重新执行前序人工审批。

新 1.0 绑定契约若缺少或提供无效下标，仍会被阻断。重复研究问题、重复 `query_id`、越界或非整数下标也会被确定性拒绝，防止索引关系漂移。

## 错误分类

公开研究错误分为：

- `CONFIGURATION`：搜索 Provider、SearXNG、Connector/Recorded 文件等运行依赖问题，进入 `WAITING_CONFIGURATION`；
- `PLAN_CONTRACT`：研究计划结构或绑定错误，保持内容/契约失败，不冒充配置问题；
- `RETRIEVAL`：没有成功归档来源；
- `SECURITY`：URL 或网络边界安全拒绝；
- `INTEGRITY`：归档 Hash 或证据完整性失败。

只有 `CONFIGURATION` 类错误可以进入 `WAITING_CONFIGURATION`。

## 恢复

对已经停在 `PUBLIC_SEARCH` 的工作流，部署补丁并重启服务后直接继续原工作流。系统会复用已经生成的研究计划，在本地执行兼容迁移并继续检索，不会重新调用 `P-PUBLIC-RESEARCH-PLAN`。
