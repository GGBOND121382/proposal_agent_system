# DOCX/PDF 导出后验收

## 1. 阶段目标

本阶段位于全文 Integration Critic 之后、G3 正式能力验收之前。它不重新判断模型语义质量，而是证明最终审查通过的候选集合被完整、可追溯地转换为 DOCX/PDF，并在导出后完成结构、内容一致性、页面视觉、责任路由、修复复核和断点恢复。

## 2. 输入硬门

导出后验收只接受：

1. 由 `P-EXPRESSION-POLISH` 生成且被后续独立 `P-EXPRESSION-CRITIC` 判为 PASS 的最终章节；
2. 与最新 PASS 全文 Integration Critic 的章节顺序、候选 ID、Polish Run 和 Expression Critic Run 完全一致的候选集合；
3. 已关闭全部 P0/P1，且满足原有导出授权条件的项目；
4. 可用的 LibreOffice、Poppler 与页面截图工具链。

每次验收记录稳定 `candidate_set_hash`、逐章候选与运行来源、DOCX/PDF/截图哈希以及验证运行 ID。

## 3. 导出后检查

### 3.1 DOCX 结构与候选一致性

- 章节集合和顺序必须与最终候选一致；
- 每个候选的可见文本单元必须出现在 DOCX；
- 标题、列表、表格、公式、图题和参考文献不得丢失；
- 禁止空章节、重复标题、占位值、内部运行术语、绝对路径和未渲染指令；
- 引用编号必须有对应参考文献；
- 表格结构、表头和正文行必须可用。

### 3.2 PDF 与页面视觉

- LibreOffice 转换失败必须显式阻断，不允许静默降级；
- PDF 文本层必须覆盖最终候选内容；
- 每页必须生成带 SHA-256 的截图；
- 检查空白页、内容触边、异常留白、叠字风险、过小图形和过小表格文字；
- DOCX、PDF、结构报告、视觉报告和截图属于同一次验证运行。

## 4. Finding 责任路由

导出后的问题分成两类，禁止互相掩盖：

- **正文或候选内容缺陷**：路由到 `WRITING_AGENT` 和具体 `section_id`。在候选集合未变化且没有新的全文 Integration Critic 复审前，禁止再次导出放行。
- **导出、转换或版式缺陷**：路由到 `EXPORT_ENGINEERING`。只允许在 `candidate_set_hash` 不变时受控重导出，修复后必须由新的 Delivery Validator Run 复核。

Finding 只有同时具备责任方修复证据和后续独立复核证据才能进入 `VERIFIED`。人工修改 DOCX/PDF、手工改库或改正文规避版式问题均不构成修复证据。

## 5. 持久化与恢复

每次尝试保存为 `POST_EXPORT_ACCEPTANCE` Artifact，包括：

- 候选快照与候选集合哈希；
- 全文 Integration Critic 对应关系；
- DOCX、PDF、交付 ZIP、结构/视觉报告和逐页截图；
- Finding 路由、开放阻断项和验证运行；
- 每个文件的路径、大小与 SHA-256。

重启后只有在候选集合、全部文件大小和哈希均未变化且没有开放 P0/P1 时，才允许复用既有 PASS；否则重新执行验收。

## 6. 固定材料验收

`scripts/run_post_export_acceptance.py` 使用明确标记的固定材料执行：

1. 生成并关闭 14 章节并发编制检查点；
2. 确认全文 Integration Critic PASS；
3. 真实生成 DOCX，使用 LibreOffice 转换 PDF；
4. 执行候选一致性、结构和逐页视觉检查；
5. 注入仅用于测试的正文占位故障，验证路由到责任 Writing Agent；
6. 注入仅用于测试的缺章结构故障，验证路由到 Export Engineering；
7. 验证故障副本不改变正式交付包；
8. 重建 Manager 并验证已通过结果可从 SQLite 和文件哈希恢复。

固定材料验收证明导出与复核工程能力，不替代 G3 中无 Replay、真实模型和真实公开调研的正式语义能力证明。
