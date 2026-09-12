# Search / Background Workflow Phase 3 实施报告

日期：2026-09-03
前置基线：`7a4e8fd update phase 2 (playwright and search fallback)` + `docs/SEARCH_BACKGROUND_PHASE2_REPORT_20260902.md`

## 1. 范围

本批只实施 Phase 3：升级现有 `WF-3_HYBRID_ONLINE_ASSIST` 的检索执行合同与充分性统计。

- 未创建 WF-3B，未修改 WF-4，未新增数据库表，未改 UI。
- 未运行 LIVE LLM。
- 修改了 Prompt JSON Schema（`P-PUBLIC-RESEARCH-PLAN` 2.1.0 → 2.2.0）与 Prompt 文案，未改 Model JSON Schema（模型仍只见语义字段）。

## 2. Phase 2 基线复核结果（接手第一组操作）

- 范围回归：`217 passed`（Search/Fetch、dependency preflight、WF-3 契约/质量/语义边界、replay 技能链路）。
- Phase 2 批次文件 Ruff：通过（`scripts/check_config.py` 的 E402 sys.path 引导为仓库既有模式，HEAD 上即存在）。
- `compileall`：通过；`git diff --check`：通过（仅既有 CRLF 提示）。
- 无 LLM 真实 Web Search capability：
  - 默认 Brave 模板：`status=FAIL`，`llm_invoked=false`，`net::ERR_CONNECTION_TIMED_OUT`（本机网络到 search.brave.com 不通，真实失败并留存 receipt `data/capability_tests/web_search/20260903T013822.912154_0000/`）。
  - Bing 模板（`BROWSER_SEARCH_URL_TEMPLATE=https://www.bing.com/search?q={query}`）：`status=PASS`，`llm_invoked=false`，HTTP 200，5 条真实网页命中，receipt `data/capability_tests/web_search/20260903T014003.413668_0000/capability_receipt.json`，结果页 SHA-256 `124644c162476afd1d5c9e0924512e2c78bfe8b2b1b426b9bafdc07020297072`。
- 既有基线问题保持不碰：`tests/test_runtime.py::test_runtime_recovers_safe_package_scalar_source_ref_drift_without_model_call` 在干净 HEAD 上同样失败（已用 stash 复验），属 Phase 0/1 已记录的非搜索基线问题；Argument `review_units`、G0、Full Integration 超时等同理。

## 3. 已实现能力

### 3.1 检索执行合同（Plan 契约 2.2.0）

`P-PUBLIC-RESEARCH-PLAN` 输出新增 5 个运行时字段（代码生成/注入，模型不生成）：

- `required_channels`：`ACADEMIC` / `WEB_SEARCH`；
- `provider_execution_requirements`：`required_providers` + `execute_all_approved_queries`；
- `minimum_fulltext_sources_per_query`；
- `allow_snippet_only`；
- `require_web_discovery`。

来源优先级：已批准 plan 中显式值 > 工作流 options（`payload.retrieval_contract`，`context_base.py:_wf3_retrieval_contract` 注入）> 环境配置（`PUBLIC_SEARCH_REQUIRED_CHANNELS` / `PUBLIC_SEARCH_REQUIRED_PROVIDERS` / `PUBLIC_SEARCH_REQUIRE_WEB_DISCOVERY` / `PUBLIC_RESEARCH_MIN_FULLTEXT_SOURCES_PER_QUERY` / `PUBLIC_SEARCH_ALLOW_SNIPPET_ONLY`）> provider 推导默认（hybrid → `[ACADEMIC, WEB_SEARCH]`，academic → `[ACADEMIC]`，searxng/browser → `[WEB_SEARCH]`）。

- `app/skills/research_plan.py`：`normalize_execution_contract` + `validate_execution_contract`；strict 模式拒绝未知通道与 `require_web_discovery=true` 但无 WEB_SEARCH 通道的矛盾合同；旧 plan 缺字段时按默认规范化，保持可读。
- `app/skills/research_execution.py`：5 字段纳入 `_plan_projection`（plan_hash 锁定）；`validate_plan_transition` 将其列为 immutable，但旧 lock 缺少这些键时不视为 mismatch（in-flight 工作流兼容）。
- `app/model_semantic_contracts.py:expand_public_research_plan_model_output`：从 `payload.retrieval_contract` 原样注入输出；`app/simulated_llm.py` 同步镜像。
- Replay：`prompt_pack/replay/cases/public_research_plan/` 5 个 case 全部迁移到 2.2.0 并补字段；`validate_pack.py` PASS；`SHA256SUMS.txt`/`MANIFEST.md` 已重新生成。

### 3.2 Provider 通道与 profile 驱动执行

- `SearchProvider` 新增 `channel` 类属性；`PROVIDER_CHANNELS` 名称→通道映射覆盖 openalex/crossref/semantic_scholar/academic-multi-source/searxng/browser/browser_search/connector/recorded。
- `_academic_connector_file` hybrid 分支：SearXNG 失败/零命中时 Browser fallback 保持；合同把 `browser_search` 列为 required provider 时即使 SearXNG 成功也强制执行；required provider 被禁用（`BROWSER_SEARCH_ENABLED=off`）时记录 `REQUIRED_PROVIDER_DISABLED` 失败而非静默跳过。

### 3.3 Required channel 语义（禁止 Academic 掩盖网页失败）

- `build_retrieval_health` 改为通道感知（不再硬编码 academic/searxng 名单），`browser_search` 计入 WEB_SEARCH 通道成功。
- `require_web_discovery=true` 且 WEB_SEARCH 通道全部失败 → `REQUIRED_CHANNEL_FAILED:WEB_SEARCH` 进入 `blocking_reason_codes`，`retrieval_health`/`research_sufficiency` 为 `BLOCKING_FAILURE`；strict 运行时（`verifiable_public_research.run`）抛出 `REQUIRED_WEB_DISCOVERY_FAILED`，与质量 profile 无关。
- `require_web_discovery=false` 时同样的失败只产生 DEGRADED 观察（技术调研允许明确降级）。
- 合同声明的 required provider 完全未执行 → `REQUIRED_PROVIDER_NOT_EXECUTED:<name>` 恒为阻断（执行合同违规）。

### 3.4 三级充分性统计

`upgrade_archive_result` 与 validation bundle 新增 `evidence_funnel`：

- `search_hits`：provider 命中总数（只证明搜索执行过）；
- `readable_documents`：非 `SNIPPET_ONLY` 的可读/已提取记录数；
- `full_text_documents`：正文 ≥1000 字符的记录数；
- `snippet_only_records`：被拦截只保留摘要的记录数（不计入可读、不计入 Coverage）；
- `usable_evidence`：Coverage 实际绑定到已批准 query 的去重来源数。

coverage 新增 `query_fulltext_depth` 维度（`proposal_related_work` profile），默认阈值 1 与既有 uncovered-query 条件一致，不改变旧行为；合同声明更高值时才收紧。`SNIPPET_ONLY` 不计入全文来源与 Claim Coverage 的既有规则保持不变并新增显式计数。

## 4. 修改文件

- 契约：`prompt_pack/schemas/prompts/public_research_plan_input.schema.json`、`public_research_plan_output.schema.json`、`prompt_pack/prompts/public_research/research_plan.md`、`prompt_pack/config/prompt_registry.json`、`prompt_pack/replay/cases/public_research_plan/*.json`（5 个）、`prompt_pack/SHA256SUMS.txt`、`prompt_pack/MANIFEST.md`
- 运行时：`app/config.py`、`.env.example`、`app/skills/research_plan.py`、`app/skills/research_execution.py`、`app/skills/search_providers/{base,__init__,academic,searxng,browser_search,connector,recorded}.py`、`app/skills/verifiable_public_research.py`、`app/skills/research_quality.py`、`app/skills/research_audit.py`、`app/skills/research_validation.py`、`app/model_semantic_contracts.py`、`app/simulated_llm.py`、`app/context_base.py`
- 测试：`tests/test_wf3_research_quality_gate.py`（+5）、`tests/test_wf3_research_batch_a.py`（+4）、`tests/test_search_fetch_contracts.py`（+1）

## 5. 验证结果

- Phase 3 范围回归：`227 passed in 8.62s`（217 基线 + 10 新增）。
- 新增验收覆盖计划 §9 搜索能力用例 3（Academic 成功 + 网页全失败 + `require_web_discovery=true` → 阻断，含 DEGRADED 对照与 browser_search 满足通道对照）和用例 4（10 命中仅 2 可读 → Coverage/usable_evidence 按 2 计）。
- 本批 Python 文件 Ruff：`All checks passed`（test_wf3_research_batch_a.py 既有 2 处 F401、academic_search.py 既有 1 处 F401 为 HEAD 基线问题，未顺手修改）。
- `compileall` 通过；`git diff --check` 通过（仅既有 CRLF 提示）。
- 未运行 LIVE LLM；真实 capability 证据见第 2 节。

## 6. 尚未实施

- Phase 4：独立 `WF-3B_TOPIC_BACKGROUND_RESEARCH`（新工作流类型、新 Prompt、新 Schema、Replay、UI）。
- Phase 5：WF-3B → WF-4 lineage、Argument Architecture/Revision Plan/Section Contract 背景证据路由、`_scoped_facts()` 修复。
- Phase 6：完整 mutation/replay/lifecycle/recovery 与经授权的小规模 LIVE 闭环。
