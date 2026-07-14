# 公开研究检索与资料归档

## 核心原则

公开资料不是由系统外人工整理后直接塞给写作智能体。标准链路为：

```text
Public Research Plan Agent
  -> 生成研究问题和原始查询
  -> Research Skill 调用批准的检索连接器
  -> 校验每个计划查询均有响应
  -> 归档连接器原始响应和逐来源快照
  -> Research Synthesis 生成 PUBLIC_CLAIM
  -> Critic 与人工导入 Gate
  -> 写作智能体引用已接受 Claim
```

## 运行模式

### `searxng`

系统直接向受控 SearXNG 发起查询，并逐条访问公开 URL。每个来源保存原始 HTML/PDF/文本、提取文本、元数据和哈希。适用于应用进程具有受控互联网出口的混合部署。

### `connector`

用于应用进程无法直接联网、但部署平台提供批准的搜索连接器时。连接器必须执行 Research Plan Agent 生成的原始查询，并按约定 JSON 返回结果。Research Skill 会：

1. 校验 `responses` 覆盖全部 Agent 查询；
2. 保存完整 `connector/connector_response.json`；
3. 为每条结果保存原始 JSON 快照、文本、元数据和 SHA-256；
4. 去除重复 URL；
5. 输出来源引用和 passage；
6. 记录连接器名称、运行 ID、查询和网页引用标识。

配置：

```env
PUBLIC_SEARCH_PROVIDER=connector
PUBLIC_RESEARCH_CONNECTOR_FILE=/path/to/connector_response.json
```

### `recorded`

用于经过审批的可重复回放或严格物理隔离导入。输入是已经形成的公开来源集合，系统仍会生成来源快照、文本、元数据、Hash、CSV 索引和会话 manifest。该模式不能冒充实时检索，验收报告会明确标为 `RECORDED_VERIFIED_SOURCE_SET`。

## 连接器输入契约

```json
{
  "run_id": "research-run-001",
  "connector": "approved-search-connector",
  "created_at": "2026-07-14T00:00:00Z",
  "agent_generated_queries": ["query A", "query B"],
  "responses": [
    {
      "query": "query A",
      "retrieved_at": "2026-07-14T00:00:00Z",
      "results": [
        {
          "title": "...",
          "url": "https://...",
          "authors": ["..."],
          "published_at": "2025-01-01",
          "content_text": "connector page snapshot or abstract",
          "verification": {
            "status": "CONNECTOR_PAGE_OPENED",
            "web_reference": "connector-reference-id"
          }
        }
      ]
    }
  ]
}
```

缺少任一计划查询时，Research Skill 直接失败，不允许进入 Research Synthesis。

## 归档目录

```text
data/research_archive/<project_id>/<session_id>/
├── manifest.json
├── source_index.csv
├── connector/
│   └── connector_response.json
├── raw/
│   └── <source_id>.json|html|pdf
├── text/
│   └── <source_id>.txt
└── metadata/
    └── <source_id>.json
```

每条记录包含 `source_id`、原始 URL、最终 URL、作者、日期、发布者、DOI、检索词、访问时间、检索提供者、原始快照 SHA-256、文本 SHA-256 和本地路径。

## 安全控制

- 只接受 HTTP/HTTPS 公共 URL；
- 实时抓取拒绝 localhost、`.local`、私网、回环、链路本地、保留地址及其重定向；
- 单来源大小和抓取超时可配置；
- 在线检索只处理已审批的 PUBLIC 任务包；
- 公开结论经 Import Critic 和人工 Gate 后才能进入内部写作上下文；
- 模型不得根据自身记忆补充未归档来源。

## 验证方式

验证者可从正文引用找到 `claim_id`，再映射到 `source_id`；使用 `source_index.csv` 定位本地快照和 URL，重新计算文件哈希，并使用保存的连接器运行 ID、查询和网页引用标识独立核验来源。
