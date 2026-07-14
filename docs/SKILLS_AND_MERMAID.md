# Skill 与 Mermaid 图形链路

## Skill 边界

Skill 是受代码约束、可单独审计的工具能力。每次执行都会写入：

- `skill_runs`；
- 输入/输出 Hash；
- 完整输入和输出 JSON；
- 状态、耗时和错误；
- `SKILL_OUTPUT` 工件；
- 审计事件。

当前内置 Skill：

| Skill | 作用 |
|---|---|
| `mermaid.render` | Mermaid 校验、源码保存、SVG/PNG 渲染和元数据记录 |
| `public_research.archive` | 搜索或导入公开资料，保存快照、提取文本、来源元数据和校验 Hash |

可通过 `GET /api/skills` 查看注册能力，通过 `GET /api/skill-runs` 查看执行记录。

## Mermaid 标记

写作模型在独立段落输出：

```text
[[MERMAID]]图题|15.5
flowchart LR
A[输入] --> B[处理]
B --> C[输出]
```

后处理过程：

1. 提取 Mermaid 源码；
2. 检查图类型、危险指令和复杂度；
3. 使用本地打包的 Mermaid JS 和 Chromium 渲染；
4. 保存 `.mmd`、`.svg`、`.png` 和 `.json`；
5. 将正文标记替换为 `[[FIGURE]]`；
6. DOCX 导出器插入 PNG；
7. `.mmd` 保留给用户修改。

禁止 `click`、JavaScript、`<script>` 和动态初始化指令。Mermaid 使用 strict security level，不加载外部 CDN。

## 弱模型回退

关键章节没有输出图形，或模型 Mermaid 源码校验/渲染失败时，系统按章节类型选择确定性模板。回退本身也会生成 `.mmd`，并在 Prompt warning 与 Skill 日志中记录原因。
