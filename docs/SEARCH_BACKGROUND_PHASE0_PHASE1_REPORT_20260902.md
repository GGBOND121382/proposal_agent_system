# Search 与背景调研升级：Phase 0—Phase 1 第一批交付报告

日期：2026-09-02  
分支：`agent/staged-proposal-workflow-20260723`  
冻结提交：`ff7cbd90f890d17d8e687adca366f1057fe89310`

## 1. 授权边界执行情况

- 未修改数据库 Schema，未新增数据库表。
- Prompt JSON Schema 与数据库 Schema 被作为两类独立对象处理。本批对 Prompt JSON Schema 的修改仅用于修复已有 Prompt Pack 版本/Replay 一致性问题。
- 未新增、伪装或复用 `PUBLIC_RESEARCH` 作为 WF-3B。WF-3B 尚未进入实施阶段。
- 未运行 LIVE MiniMax，也未启动任何模型调用。
- Phase 2 才加入 Browser Search/Playwright。本批没有把 Browser Search 设计成唯一来源；Academic、SearXNG、Connector、Recorded 的适配边界已经保留。
- 开始时工作区相对 `HEAD` 为 clean；因此 Phase 0 工作区 checkpoint patch 是合法的 0 字节 no-op patch，而不是遗漏未提交文件。

## 2. Phase 0：基线冻结

### 2.1 基线身份与 checkpoint

- HEAD：`ff7cbd90f890d17d8e687adca366f1057fe89310`
- Phase 0 no-op checkpoint patch：`proposal_agent_search_background_phase0_baseline_ff7cbd9_20260902.patch`
- patch SHA-256：`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`
- `wf4_critic_review_graph_closure_checkpoint_20260901.patch`：`7bcb7b3d9d1e34903c5cc3eb4803dffd5ea1492d957acd6ae873e650a181ea41`
- `workflow_oneclick_frontend_combined_gitapply_no_ps1_20260831.patch`：`0864b35d58f309afbd7e9ac765f1b7cefd01d25780bb6c6cc00526190a5bb8ee`
- `workflow_oneclick_frontend_combined_for_7589a674_20260831.patch`：`fbc9a911d02dfdba128d7ec4820309a1e98ff2561a8785b6c542a45f3a43f50d`

### 2.2 核心基线文件 Hash

| 文件 | Phase 0 SHA-256 |
|---|---|
| `app/skills/public_research.py` | `f9d4ced111c16d986f9bd99a28dcbdd927f6bff4a75617ce92ea0ad29731aaed` |
| `app/skills/verifiable_public_research.py` | `ae6c488951e1dfd874de07d747223966b1dcc0f21e9ddb3787691d2b20039a22` |
| `app/skills/academic_search.py` | `38f3d83f50c6236eedf213ef616ce0a87d94b3ca7368f02f3db1f2f38acddd38` |
| `app/research.py` | `b732387b1abe47878938e684767ac2ca11ad10bc07287869fe06963d67003476` |
| `app/context_base.py` | `3cfadca7e90e4b5de81fe12289deb1e1064ec9c6bd60d1d48a26e14eea684807` |
| `app/workflow_defs.py` | `297eb732bf50a93f697d088647eeb29c9268dc404093b636c1c5d4bc03ba4000` |
| `app/workflows.py` | `14ef175ea14f055518635f52ca87282f42905db557ed3359798f1f137ec0b2f4` |
| `app/dependency_preflight.py` | `e26309897d42ddaa488c41ac62cb3b9398492384433852e497f9ea67dec7f9d1` |
| `prompt_pack/config/prompt_registry.json` | `b41684a364134ea0c1ec3f239253b36bc8cbd8131a07f3b1ee98774273fc2b6b` |

### 2.3 Phase 0 发现并收口的基线问题

1. `tests/test_dependency_preflight.py` 的旧流程 fixture 仍把 `PUBLIC_SEARCH` 当作第 3 步；当前流程已插入 Plan Scope Critic，实际搜索是第 4 步。fixture 已迁移。
2. Argument Architecture Critic 的 Registry 已是 `10.0.0`，但 Prompt 输入/输出 Schema 与 5 份 Replay 仍声明 `9.1.0`。已统一为 `10.0.0`。
3. 30 份 `missing_input` Replay 以及 Expression 的 `need_user_input` Replay 中，`answer_schema.type=OBJECT` 缺少 `properties`，与统一问题契约不一致。Replay 和生成器一并修复。
4. `P-PUBLIC-RESEARCH-PLAN-SCOPE-CRITIC` 是有意采用 `OFFLINE_LOCAL` 的公共研究 Critic；旧校验器把所有公共研究 Prompt 一律要求为 `ONLINE_PUBLIC`。校验规则已按该明确例外修复。

## 3. Phase 1：Search/Fetch/Extract 契约抽离

### 3.1 新增的稳定边界

- `SearchQuery`：保留 `query_id`、查询文本、用途、问题绑定与查询约束。
- `SearchHit`：统一 provider、query、rank、title、URL、snippet 和 provider 元数据。
- `ProviderRun`：保存逐查询执行状态、HTTP 状态、阻断类型、原始响应及其 SHA-256。
- `SearchGateway`：按明确顺序运行 provider，隔离 provider 错误，并保留 provider manifest。
- `FetchedDocument`：记录请求/最终 URL、类型、状态、抓取模式、字节数和原文 Hash。
- `ExtractedDocument`：记录提取器、文本质量、失败原因、文本 Hash 和元数据边界。
- `HttpFetchGateway`：保留 HTTP/PDF 快速路径、重定向后公网 URL 复验和最大响应限制。
- `ContentExtractor`：保留 PDF、纯文本和静态 HTML 的确定性提取；Playwright 兜底不在本阶段伪装实现。

### 3.2 已迁移的 provider

- `RecordedSearchProvider`
- `ConnectorSearchProvider`
- `SearxngSearchProvider`
- `AcademicSearchProvider`（继续复用 OpenAlex/Crossref/Semantic Scholar 客户端实现）

现有 `PublicResearchArchiveSkill` 方法仍作为兼容层存在，但实际调用已委托给新 provider、FetchGateway 和 ContentExtractor。`VerifiablePublicResearchArchiveSkill` 的 Academic/Hybrid 发现路径已经通过 `SearchGateway` 执行，并继续输出旧 connector/discovery manifest 能读取的结构。

归档记录新增以下可观测字段，均为兼容性增量，不删除旧字段：

- `fetch_mode`
- `extractor`
- `extraction_quality`
- `extraction_failure_reason`

## 4. 修改文件清单

### 4.1 Phase 1 运行时代码

- `app/skills/search_gateway.py`（新增）
- `app/skills/search_providers/__init__.py`（新增）
- `app/skills/search_providers/base.py`（新增）
- `app/skills/search_providers/recorded.py`（新增）
- `app/skills/search_providers/connector.py`（新增）
- `app/skills/search_providers/searxng.py`（新增）
- `app/skills/search_providers/academic.py`（新增）
- `app/skills/fetch_gateway.py`（新增）
- `app/skills/content_extraction.py`（新增）
- `app/skills/public_research.py`
- `app/skills/verifiable_public_research.py`
- `tests/test_search_fetch_contracts.py`（新增）

### 4.2 Phase 0 基线修复

- `tests/test_dependency_preflight.py`
- `prompt_pack/tools/build_v2.py`
- `prompt_pack/tools/validate_pack.py`
- `prompt_pack/BUILD_REPORT.json`
- `prompt_pack/schemas/prompts/argument_architecture_critic_input.schema.json`
- `prompt_pack/schemas/prompts/argument_architecture_critic_output.schema.json`
- `prompt_pack/replay/cases/argument_architecture_critic/*.json`（5 份）
- `prompt_pack/replay/cases/*/missing_input.json`（30 份）
- `prompt_pack/replay/cases/expression_polish/need_user_input.json`
- `prompt_pack/replay/cases/expression_critic/need_user_input.json`

`prompt_pack/config/prompt_registry.json` 本批未修改。

## 5. 验证结果

### 5.1 通过项

- Prompt Pack：`PASS`；31 个 Registry 项、155 个 Replay、124 个有效输入/输出、0 error。
- Phase 0 相关基线：`82 passed`。
- Phase 0 + Phase 1 专项（最终复验）：`87 passed in 15.86s`。
- Search/Fetch 契约、Recorded/Connector/SearXNG/Academic、旧 WF-3 归档/质量门禁专项：`61 passed`。
- Ruff（本批 Python 文件）：`All checks passed`。
- `git diff --check`：通过，仅报告既有 CRLF→LF 提示，无空白错误。
- Python `compileall`：通过。

### 5.2 全量套件现状

执行 `py -m pytest -q`：

- `1434 passed`
- `1 skipped`
- `26 failed`
- `6 errors`
- 总耗时 `1131.40s`

失败集中于当前 HEAD 已存在的 G0 冻结身份漂移、Argument Critic `review_units` 历史契约、全申请书并发测试超时、旧 provenance/semantic closure/quality guard 断言。典型证据包括：

- G0 仍以旧提交 `ac7e303...` 为冻结基线，而当前 HEAD 已是 `ff7cbd9...`，并报告 Prompt Registry Git blob、数据库 artifact contract 及大量已提交文件的冻结 Hash 漂移。
- 多个 Argument 测试在 `candidate.review_units` 上失败，但本批未修改 Argument 运行时代码或其 model schema。
- 6 个 error 均来自同一 Full Integration module fixture 的 150 秒超时级联。

这些失败不位于本批 Search/Fetch/Extract 变更路径；本批专项与原 WF-3 回归均通过。后续不应把它们混入 Phase 2 的 Browser 能力修改中，应作为独立基线治理任务处理。

## 6. Phase 1 结果文件 Hash

| 文件 | SHA-256 |
|---|---|
| `app/skills/search_gateway.py` | `61df500a9b3d29b7d546f75d028064197e362779dd124b0d3f60f54e54014ef4` |
| `app/skills/search_providers/base.py` | `ade72075d8ec9d3d4023869c12f15ad199e4e754bc8acc72c3c806562546c965` |
| `app/skills/search_providers/searxng.py` | `829e5b846ea4cfe5aaf3d410051a939f6010bea84800a0ff3e0eb7286a0179ac` |
| `app/skills/search_providers/academic.py` | `50f27c1de445d05212e50c832562df45e0502f3d0505aba937eb5dabadc5cba8` |
| `app/skills/search_providers/recorded.py` | `443577ea4f048053d81464ebb072623a8199f59f14e5203899bd0a87b0c8dd22` |
| `app/skills/search_providers/connector.py` | `b9658eab98b9535e5824ae3d2e956bd7cf0b44ef13ebf1d5e47c3b0d5f074306` |
| `app/skills/fetch_gateway.py` | `57a0d00a561e02121149e24b4ea9c02deaf7a804f6206927fd541ee4cdcd54d6` |
| `app/skills/content_extraction.py` | `81d5587a45e9c62468d39ad75ef257f70d49a4c3f165462716e89fd53cee4286` |
| `app/skills/public_research.py` | `ae320bb3055f6d168af691bec3e838049cafa9efe32cf7564a5721dac6b4de00` |
| `app/skills/verifiable_public_research.py` | `4c46763dbed045f661937e052a9228294ea30a042ca8f2667303a6a8cf655049` |
| `tests/test_search_fetch_contracts.py` | `c8531aa75e77c51252b586905444a6dcd3f2541d1e0a9c2e0c306b636b2db470` |

## 7. 尚未实施的阶段

- Phase 2：Browser Search provider、Browser Worker/Pool、HTTP→Playwright 动态页面兜底、CAPTCHA/登录墙识别、缓存与限速、无 LLM 的真实 Search/Fetch capability test。
- Phase 3：WF-3 provider profile、required channels、`require_web_discovery`、搜索命中/正文读取/证据可用性的分层充分性判定。
- Phase 4：正式新增 `WF-3B_TOPIC_BACKGROUND_RESEARCH`、独立 Prompt、Prompt JSON Schema、Model Schema、Replay、UI 和 `TOPIC_BACKGROUND_RESULT`。
- Phase 5：WF-3B lineage 与 WF-4 Argument/Revision Plan/Section Contract/Writing 的确定性背景证据路由。
- Phase 6：完整 mutation/replay/lifecycle/recovery、真实能力验收，以及经用户再次授权后才可进行的小规模 LIVE 模型闭环。

## 8. 下一批准确修改范围

下一批只实施 Phase 2，不提前混入 WF-3B Prompt 或 WF-4 路由：

1. 新增 `app/skills/search_providers/browser_search.py`。
2. 新增受控、批次复用的 `app/skills/browser_worker.py`，不为每个 URL 单独启动 Chromium。
3. 扩展 `FetchGateway`：静态提取为 EMPTY/SHORT 或页面呈现 JS 壳时，进入 Playwright fallback。
4. 在每次导航前、重定向后和浏览器子请求上执行公网 URL/协议安全检查。
5. 明确记录 `PROVIDER_BLOCKED`、CAPTCHA、登录墙、403、超时和动态提取失败，不绕过验证。
6. 增加配置、dependency preflight 和测试，但不在该批新增数据库表。
7. 执行至少一个不调用 LLM 的真实 Web Search 通道测试，并保存真实结果页执行 receipt；Academic 结果不得替代该验收。
