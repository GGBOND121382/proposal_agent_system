# 项目申请书智能体系统

本项目把附件 `proposal_prompt_pack_v2` 从静态 Prompt 交接包落成一个可运行的多智能体系统。运行时动态读取 26 个 Prompt、52 个输入/输出 Schema、模型路由与安全策略，不在业务代码中复制 Prompt 正文。

## 已实现

- 26 个 Prompt 的动态注册、输入/输出 Schema 严格校验和完整 Schema 内联；
- OpenAI-compatible 离线/在线模型网关，离线失败不会自动切换在线；
- `REPLAY`、`MOCK`、`SIMULATED`、`LIVE` 四种运行模式；
- 五条核心工作流状态机与十三类人工 Gate；
- 一次定向修复额度与 Critic/Producer 分离；
- DOCX、PDF、Markdown、TXT、JSON、CSV 材料解析；
- 上传存储元数据与 Prompt `document_context` 严格隔离，确保真实材料替换 Replay 种子；
- 项目、材料、Prompt Run、Artifact、Workflow、Gate 和审计事件持久化；
- 受控在线公共研究接口，默认关闭，支持自建 SearXNG；
- DOCX 新文档生成、简单章节定向补丁、完整性报告和导出审计包；
- 浏览器操作台及 FastAPI 接口文档；
- Prompt 包静态校验和端到端自动化测试。

## v0.5.0 离线部署、弱模型 Skill 与可核验公开研究

本版本完成三项生产化改造：

- **离线部署双路径**：外网构建 Ubuntu/Windows 源码依赖包，内网执行 manifest 校验并一键安装；或外网构建并 `docker save` 应用镜像，内网 `docker load` 后启动；
- **弱模型任务拆分**：将长申请书拆成规则/事实抽取、研究计划、单章蓝图、单章正文、Critic、跨章审查和定向修复；模型只生成 Mermaid 源码，代码负责安全检查、Playwright/Chromium 渲染、缓存、失败重启和 DOCX 插图；
- **公开研究 Skill**：Research Plan Agent 生成原始查询，`public_research.archive` 调用 SearXNG、批准的连接器或受控记录集，强制覆盖全部查询并保存原始响应、网页/PDF/JSON 快照、提取文本、元数据、URL、访问时间和 SHA-256；已接受 `PUBLIC_CLAIM` 才能进入写作上下文。

复杂物流课题验收命令：

```bash
python scripts/build_transport_optimization_materials.py data/transport_optimization_materials_v1
python scripts/run_transport_optimization_complex_e2e.py \
  --materials-dir data/transport_optimization_materials_v1 \
  --output-dir data/transport_optimization_complex_e2e
```

离线部署和模型配置详见：

- `docs/OFFLINE_DEPLOYMENT.md`
- `docs/HYBRID_DEPLOYMENT.md`
- `docs/MODEL_API_CONFIGURATION.md`
- `docs/PUBLIC_RESEARCH_ARCHIVE.md`
- `docs/SKILLS_AND_MERMAID.md`

本次可重复端到端验收使用 `SIMULATED` 模型边界，因为未配置用户真实模型密钥；公开研究不是模拟：Research Agent 的10个查询通过批准连接器执行，39个去重来源被系统自身归档并完成哈希复核。

## v0.4.0 复杂申请书、公开证据、图形与全链路Trace

本版本针对复杂科研申请书的真实验收缺口进行系统修复：

- 新增 `SIMULATED` 确定性模型模式：不调用外部模型API，但按26个Prompt各自Schema生成项目相关输出，不再返回通用Replay句子；
- 26个Prompt在复杂端到端测试中全部实际触发，`P-TARGETED-REPAIR`通过“Critic发现问题→定向修复→再次审查”真实运行；
- 公开研究综合产出结构化 `PUBLIC_CLAIM`，在导入审查后进入写作事实上下文；
- 每次模型调用保存安全域内 `PROMPT_TRACE`，包含完整System Prompt、输入Envelope、输出Schema、原始响应、解析输出、路由、模型、端点、状态和耗时；
- DOCX导出器新增 `[[FIGURE]]` 图形工件协议，支持逻辑结构图、技术路线图、关键执行流图等图示；
- 新增41章“后勤保障智能体”复杂测试材料、36条公开参考资料、11类核心图示及完整质量门；
- 自动验收覆盖章节、页数、参考文献、图示、Prompt覆盖、定向修复、Trace完整性、隐私泄漏和重复段落。

复杂端到端运行：

```bash
python scripts/build_logistics_agent_materials.py data/logistics_agent_materials_v1
python scripts/run_logistics_agent_complex_e2e.py \
  --materials-dir data/logistics_agent_materials_v1 \
  --output-dir data/logistics_agent_complex_e2e
```

`SIMULATED` 用于验证运行时能力、编排、审计和导出，不代表真实模型语义能力。参考资料元数据来自公开论文和官方标准页面；生产使用仍需配置真实模型与搜索端点。

## v0.3.0 中性端到端测试与在线隐私防护

本版本使用完全中性的“户外活动便携保温杯研制”材料验证完整申请书生成，并强化在线公共研究的数据最小化：

- 新增确定性出站隐私保护器：按项目配置替换姓名、组织、详细地址和地点，并用通用规则替换电话号码与电子邮箱；
- `P-SAFE-ONLINE-PACKAGE` 输出在持久化、人工审批和在线调用前执行二次净化，原值不会写入 PUBLIC 任务包；
- 所有 `ONLINE_PUBLIC` Prompt 在调用模型前再次扫描，发现个人信息或项目专有实体即阻断执行；
- 新增姓名、组织、地址、地点、电话和邮箱测试夹具，验证内部申请书可保留虚构值、在线任务包必须使用占位符；
- 修复多章节导出中的列表连续编号、中文表格缺字和重复段落问题；
- 模拟模型端到端覆盖五条工作流、全部人工 Gate、12 个正式章节、3 次在线 Prompt 调用和最终 DOCX/审计包导出。

运行命令：

```bash
python scripts/run_outdoor_thermos_simulated_e2e.py \
  --output-dir data/outdoor_thermos_simulated_e2e
```

该脚本以 REPLAY 模型边界运行五条工作流和十二章逐章编制，用于验证编排、门禁、候选聚合和导出。隐私替换与在线调用阻断由自动化测试覆盖；本脚本不等同于真实大模型语义能力测评。

## v0.2.0 完整申请书编制修复

本版本针对完整材料端到端测试中发现的问题进行了修复：

- `WF-4_PROPOSAL_AUTHORING` 按当前申请书的正式章节逐章执行写作蓝图、蓝图 Critic、正文生成和正文 Critic，不再只生成第一个章节；
- Context Builder 按工作流中的 `active_section_id` 注入当前章节，并从真实 `P-WRITE-CONTENT` 运行记录聚合 `candidate_sections`、`candidate_document` 和追踪关系；
- 跨章节一致性审查和最终保密审查不再使用 Replay 单节示例，避免“流程通过但实际未审查完整正文”的假阳性；
- 多章节成果采用清稿方式导出，去除初始模板中的占位语和编写说明；单章节任务仍保留定向补丁能力；
- DOCX 导出支持二级、三级标题、列表和表格结构块，表格标题行重复显示，行内容不跨页拆分；
- 新增十二章便携保温杯测试夹具、模拟模型端到端脚本和多章节回归测试。

## v0.1.1 兼容性修复

修复上传文档解析结果中的内部存储字段 `safe_filename` 被传入严格 Prompt Schema 的问题。该问题会使 `source_documents` 或 `reference_document` 的安全替换失败，并保留 Replay 示例输入。v0.1.1 在 Context Builder 中移除该内部字段，并新增包含该字段的回归测试。

## 快速启动

### Linux / macOS / WSL

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash scripts/run.sh
```

浏览器打开 `http://127.0.0.1:8080`，接口文档位于 `http://127.0.0.1:8080/docs`。

### Windows PowerShell

```powershell
Copy-Item .env.example .env
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MODEL_RUNTIME_MODE = "REPLAY"
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### Docker

```bash
cp .env.example .env
docker compose up --build
```

## 运行模式

### REPLAY

默认模式。每个 Prompt 返回附件 Replay 中的合法样例输出，用于验证系统编排、门禁、Schema、持久化和导出，不代表真实模型能力。

```env
MODEL_RUNTIME_MODE=REPLAY
```

### MOCK

同样不调用模型，输出静态样例并增加 MOCK 警告，适合前后端联调。

### SIMULATED

不调用外部模型API。按Prompt职责生成项目相关、Schema合法的确定性结果，用于覆盖全部智能体、定向修复、公开研究导入、图表导出和全链路Trace测试。

```env
MODEL_RUNTIME_MODE=SIMULATED
```

### LIVE

调用实际 OpenAI-compatible 模型端点。

```env
MODEL_RUNTIME_MODE=LIVE
OFFLINE_LLM_ENABLED=true
OFFLINE_LLM_BASE_URL=http://127.0.0.1:8000/v1
OFFLINE_LLM_API_KEY=...
OFFLINE_GENERAL_MODEL=...
OFFLINE_CRITIC_MODEL=...
```

离线端点应部署在不联网的受控环境。系统不会把离线失败自动回退到在线模型。

## 在线公共研究

在线能力默认关闭。启用前必须同时配置在线模型、项目级联网许可、外发安全审批和搜索服务。

```env
ONLINE_LLM_ENABLED=true
ONLINE_LLM_BASE_URL=https://your-public-model.example/v1
ONLINE_LLM_API_KEY=...
ONLINE_PUBLIC_MODEL=...
PUBLIC_SEARCH_PROVIDER=searxng
PUBLIC_SEARCH_BASE_URL=http://your-searxng:8080
```

工作流 `WF-3_HYBRID_ONLINE_ASSIST` 的顺序是：离线生成 Safe Online Package → 离线 Critic → 人工外发审批 → 在线公开研究 → 离线导入 Critic → 人工导入审批。在线模型只接收 `PUBLIC` 上下文。

## 五条工作流

| 工作流 | 功能 |
|---|---|
| `WF-1_PROJECT_INTAKE` | 材料、安全分类、申报规则、项目定义、事实和准备度 |
| `WF-2_TEMPLATE_EXTRACTION` | 参考申请书结构/风格提取与污染检查 |
| `WF-3_HYBRID_ONLINE_ASSIST` | 经审批的公共研究与结果隔离导入 |
| `WF-4_PROPOSAL_AUTHORING` | 修改计划、逐章蓝图与正文、逐章 Critic 和跨章节一致性 |
| `WF-5_SECURITY_REVIEW_AND_EXPORT` | 正文保密审查、内容审批和最终导出审批 |

## 材料角色

上传材料时应准确选择角色。`REFERENCE_PROPOSAL` 仅允许用于结构、风格和论证模式，系统不会把它作为本项目事实、指标、成果或技术设计来源。

## 导出

系统提供两种导出：

- DOCX：仅在 `FINAL_CONTENT_SECURITY_APPROVAL` 和 `FINAL_EXPORT_APPROVAL` 都通过后生成；
- 审计包 ZIP：包含 DOCX、Manifest 和完整性报告。

完整多章节申请书采用清稿方式生成，避免把初始模板中的占位语和编写说明带入正文；单章节修改在目标章节为纯文本时尝试按标题定向补丁。目标章节包含表格、公式、图片、批注或修订时，为避免破坏 OOXML，系统不会声称已安全补丁，而会记录跳过原因并回退为新文档生成。生产部署仍应接入单位专用 DOCX 完整性验证器。

## 安全设计

- 默认拒绝路由；
- 输入最高安全等级控制模型环境；
- 在线只接受 PUBLIC、已批准且通过确定性隐私扫描的任务包；
- 在线回传只进入候选区；
- Prompt、响应、缓存、日志和导出继承安全标签；
- 普通审计日志只记录 ID、Hash、状态、耗时等元数据；
- Gate 决定绑定目标版本、上下文 Hash、问题版本和角色；
- 过期或角色不匹配的决定会被拒绝。

## 验证

```bash
bash scripts/validate.sh
```

当前自动测试共19项，覆盖：

- 26 个 Prompt 的正常 Replay 输入/输出；
- 材料解析与 Context Builder；
- 未审批在线调用阻断；
- 在线任务包确定性脱敏与调用前个人信息阻断；
- 工作流门禁暂停；
- 五条工作流完整运行；
- 多章节逐章生成、真实候选聚合和终审输入；
- 十二章模拟模型端到端申请书生成；
- 41章复杂申请书、26/26 Prompt覆盖和定向修复闭环；
- 全量System Prompt、输入、输出Schema、原始响应和路由Trace；
- 公开证据进入写作上下文、参考文献与图形工件导出；
- Research Agent 原始查询覆盖、39个公开来源快照和哈希复核；
- Mermaid 源码安全检查、持久化 Playwright Worker、28组三联工件和弱模型回退；
- Ubuntu/Windows 离线依赖包、Docker 离线镜像包和 manifest 往返校验；
- 最终 DOCX 和审计包生成。

## 目录

```text
app/
  context.py       # 最小、Schema 合法上下文构建
  executor.py      # Prompt 执行、双向校验和在线调用前隐私门禁
  llm.py           # OpenAI-compatible 模型网关
  security.py      # 安全模型路由
  privacy.py       # 在线出站实体替换、电话邮箱净化和阻断
  workflows.py     # 五条工作流和人工 Gate
  skills/          # Mermaid 与公开研究 Skill、执行日志和注册表
  documents.py     # 材料解析
  exporter.py      # DOCX/审计包导出
  main.py          # FastAPI
  static/          # 浏览器操作台
deploy/            # Ubuntu、Windows 与 Docker 离线部署脚本
docs/              # 部署、模型API、研究归档和Skill说明
prompt_pack/       # 原 V2 Prompt 交接包
scripts/           # 启动、验证和复杂端到端验收
tests/             # 自动测试
```

## 生产上线前必须完成

1. 将 `PUBLIC/INTERNAL/SENSITIVE/CLASSIFIED` 映射为单位正式密级；
2. 将抽象 Gate 角色映射为真实身份认证和授权系统；
3. 配置并测评真实离线模型，运行 130 组 Replay 与安全红队；
4. 将 SQLite 替换为满足并发、备份和审计要求的受控数据库；
5. 接入单位文件病毒扫描、DLP、密钥管理和日志平台；
6. 对复杂 DOCX 模板实现部署单位专用的 OOXML 补丁与完整性验证；
7. 对在线模型、搜索服务和回传材料完成正式外发/导入审批。
