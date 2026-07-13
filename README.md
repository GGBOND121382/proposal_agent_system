# 项目申请书智能体系统

本项目把附件 `proposal_prompt_pack_v2` 从静态 Prompt 交接包落成一个可运行的多智能体系统。运行时动态读取 26 个 Prompt、52 个输入/输出 Schema、模型路由与安全策略，不在业务代码中复制 Prompt 正文。

## 已实现

- 26 个 Prompt 的动态注册、输入/输出 Schema 严格校验和完整 Schema 内联；
- OpenAI-compatible 离线/在线模型网关，离线失败不会自动切换在线；
- `REPLAY`、`MOCK`、`LIVE` 三种运行模式；
- 五条核心工作流状态机与十三类人工 Gate；
- 一次定向修复额度与 Critic/Producer 分离；
- DOCX、PDF、Markdown、TXT、JSON、CSV 材料解析；
- 上传存储元数据与 Prompt `document_context` 严格隔离，确保真实材料替换 Replay 种子；
- 项目、材料、Prompt Run、Artifact、Workflow、Gate 和审计事件持久化；
- 受控在线公共研究接口，默认关闭，支持自建 SearXNG；
- DOCX 新文档生成、简单章节定向补丁、完整性报告和导出审计包；
- 浏览器操作台及 FastAPI 接口文档；
- Prompt 包静态校验和端到端自动化测试。

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
| `WF-4_PROPOSAL_AUTHORING` | 修改计划、蓝图、正文、Critic 和跨章节一致性 |
| `WF-5_SECURITY_REVIEW_AND_EXPORT` | 正文保密审查、内容审批和最终导出审批 |

## 材料角色

上传材料时应准确选择角色。`REFERENCE_PROPOSAL` 仅允许用于结构、风格和论证模式，系统不会把它作为本项目事实、指标、成果或技术设计来源。

## 导出

系统提供两种导出：

- DOCX：仅在 `FINAL_CONTENT_SECURITY_APPROVAL` 和 `FINAL_EXPORT_APPROVAL` 都通过后生成；
- 审计包 ZIP：包含 DOCX、Manifest 和完整性报告。

存在当前申请书 DOCX 且目标章节为纯文本时，系统尝试按标题做定向补丁；目标章节包含表格、公式、图片、批注或修订时，为避免破坏 OOXML，系统不会声称已安全补丁，而会记录跳过原因并回退为新文档生成。生产部署仍应接入单位专用 DOCX 完整性验证器。

## 安全设计

- 默认拒绝路由；
- 输入最高安全等级控制模型环境；
- 在线只接受 PUBLIC 且已批准的任务包；
- 在线回传只进入候选区；
- Prompt、响应、缓存、日志和导出继承安全标签；
- 普通审计日志只记录 ID、Hash、状态、耗时等元数据；
- Gate 决定绑定目标版本、上下文 Hash、问题版本和角色；
- 过期或角色不匹配的决定会被拒绝。

## 验证

```bash
bash scripts/validate.sh
```

当前自动测试覆盖：

- 26 个 Prompt 的正常 Replay 输入/输出；
- 材料解析与 Context Builder；
- 未审批在线调用阻断；
- 工作流门禁暂停；
- 五条工作流完整运行；
- 最终 DOCX 和审计包生成。

## 目录

```text
app/
  context.py       # 最小、Schema 合法上下文构建
  executor.py      # Prompt 执行与双向校验
  llm.py           # OpenAI-compatible 模型网关
  security.py      # 安全模型路由
  workflows.py     # 五条工作流和人工 Gate
  documents.py     # 材料解析
  exporter.py      # DOCX/审计包导出
  main.py          # FastAPI
  static/          # 浏览器操作台
prompt_pack/       # 原 V2 Prompt 交接包
scripts/           # 启动、验证和演示
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
