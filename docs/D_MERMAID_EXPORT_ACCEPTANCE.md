# D 轨道：Mermaid、图表与交付物生成

> 分支：`agent/mermaid-export`  
> 对应计划：D1–D6

## 1. 交付范围

| ID | 实现 | 硬门 |
|---|---|---|
| D1 | `app/skills/mermaid.py` 生成 `.mmd`、SVG、PNG、元数据和 SHA-256 | 缓存复用前重新计算三类工件哈希；论证架构、技术路线和系统架构三类图均执行两次渲染复核 |
| D2 | `app/figure_protocol.py` 定义 `artifact://` 图形协议 | 多个连续 `[[FIGURE]]` 独立解析；目录穿越、外部 URI、缺图、坏图直接阻断；绝对路径不写入正文 |
| D3 | `app/exporter_base.py`、`app/exporter_render.py` | 仅选择 `P-EXPRESSION-POLISH` 且被后续 `P-EXPRESSION-CRITIC` PASS 的候选；标题、列表、表格、图、公式和引用均有确定性渲染规则 |
| D4 | `app/pdf_exporter.py` | LibreOffice 不存在、超时、非零退出、未生成文件或生成的 PDF 不可读取时直接失败，并保存转换日志和哈希 |
| D5 | `app/delivery_validator.py` 的结构验证 | 检查缺章、空小节、重复标题、占位符、内部运行术语、路径泄漏、残留指令、引用错位和异常表格 |
| D6 | `app/delivery_validator.py` 的页面视觉验证 | `pdftoppm` 逐页生成 PNG；检查空白页、边缘裁切风险、异常留白、过小图片、过小表格文字、叠字风险和公式/指令残留 |

## 2. 图形协议

新的可移植协议只在工件根目录内解析：

```text
[[FIGURE]]artifact://diagram_artifacts/<project>/<section>/<image>.png|图题|15|source=artifact://diagram_artifacts/<project>/<section>/<source>.mmd
```

连续多图可以放在同一块中，每个指令单独处理：

```text
[[FIGURE]]artifact://diagram_artifacts/p/s/a.png|图1|14
[[FIGURE]]artifact://diagram_artifacts/p/s/b.png|图2|14
```

协议拒绝 `..`、HTTP URI 和 `APP_DATA_DIR` 之外的文件。历史绝对路径只有在解析后仍位于 `APP_DATA_DIR` 内时才可读取，路径本身不会写入 DOCX/PDF。

## 3. 最终交付包

`DocxExporter.export_package()` 现在执行完整串行硬门：

1. 检查最终内容安全审批和最终导出审批；
2. 选择通过表达 Critic 的最终章节；
3. 生成 DOCX；
4. 使用 LibreOffice 转换 PDF；
5. 生成 DOCX/PDF 结构 Findings；
6. 生成逐页 PNG 和视觉 Findings；
7. 仅在所有阻断项关闭后打包 ZIP。

ZIP 包含 DOCX、PDF、完整性清单、导出 Manifest、PDF 转换日志、结构/视觉/总体验证报告和逐页截图。

## 4. 本地验收

依赖：Chromium/Chrome/Edge、LibreOffice、Poppler（`pdftoppm`）以及 `requirements-dev.txt`。

```bash
python -m pytest -q tests/test_d_track.py
python scripts/run_d_track_acceptance.py \
  --output-dir recovery_evidence/d/local
```

验收脚本会实际生成并重复渲染论证架构图、技术路线图和系统架构图，构建 DOCX，转换 PDF，逐页截图并执行 D5/D6 检查。最终证据位于：

```text
recovery_evidence/d/local/D_TRACK_ACCEPTANCE.json
```

## 5. 失败语义

以下情况不得降级为“只交 DOCX”或“插入缺图提示”：

- Mermaid 工件缺失、哈希漂移或图片无法解码；
- 图指令格式错误、多个指令被合并、路径越界；
- 缺少表达 Critic PASS 记录；
- LibreOffice 或 `pdftoppm` 不可用；
- DOCX/PDF 结构或页面视觉出现阻断 Finding。

失败时保留已生成日志和 Findings，责任归属为 `EXPORT_ENGINEERING`，不得让写作 Agent 修改正文掩盖工程缺陷。
