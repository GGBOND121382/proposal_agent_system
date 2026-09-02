# Search / Background Workflow Phase 2 实施报告

## 1. 基线与范围

- 技术基线提交：`28bb520 feat(search): extract search fetch and content contracts`
- 基线说明提交：`03e15ec docs(search): record phase zero and phase one baseline`
- 本批仅实施 Phase 2：Browser Search、受控 Playwright Worker、HTTP→浏览器读取兜底、安全检查、缓存限速、dependency preflight 和无 LLM 真实能力测试。
- 未修改数据库 Schema。
- 未修改 WF-3B Prompt / Prompt JSON Schema、WF-4 路由或工作流类型。
- `docs/video_demo_20260902/` 保持为本地未跟踪材料，不属于本批补丁，也不得提交。

## 2. 已实现能力

### 2.1 Browser Search Provider

- 新增正式 provider `browser_search`，兼容配置别名 `browser`。
- 默认通过可配置的 Brave Search 公网页面检索；URL 模板由 `BROWSER_SEARCH_URL_TEMPLATE` 控制，不绑定唯一搜索源。
- 支持 Bing、DuckDuckGo、Google 和 Brave 常见结果 DOM 结构。
- 每次查询均生成独立 `ProviderRun`；即使命中缓存，也保留本次 execution receipt。
- 原始搜索结果页 HTML、SHA-256、查询、状态、最终 URL、结果数和阻断类型均落盘。
- SearXNG 在 hybrid 路径无命中或失败时，可进入 Browser Search fallback；Academic provider 保持独立。

### 2.2 受控 Browser Worker

- 一个研究批次复用一个 Chromium/Edge Browser Context，不为每个 URL 重启浏览器。
- Playwright 延迟导入，Replay/离线路径不会因未使用浏览器而在模块导入阶段崩溃。
- 浏览器 Worker 可在应用关闭时由 Skill Registry 正常释放，并可在下次调用时重新创建。
- 按来源 origin 限速；查询/URL 使用带 TTL 的磁盘缓存。

### 2.3 页面读取兜底

- PDF、纯文本、可用静态 HTML 仍走低成本 HTTP 快速路径。
- 静态正文为空、过短或呈现 JS shell 时，才进入 Playwright 渲染。
- Playwright 成功时保存 rendered DOM，并将 `fetch_mode` 标为 `PLAYWRIGHT_RENDERED`。
- 浏览器仍被阻断或动态读取失败时，仅保留搜索摘要，标为 `SNIPPET_ONLY`；该记录不进入可引用 `sources/passages`，也不计入 Coverage。

### 2.4 阻断分类与安全

- 明确记录 `PROVIDER_BLOCKED`、`CAPTCHA`、`LOGIN_WALL`、`ROBOTS_DENIED`、`HTTP_403`、`HTTP_429`、`TIMEOUT`、`NAVIGATION_ERROR` 和 `DYNAMIC_EXTRACTION_FAILED`。
- 不尝试绕过 CAPTCHA 或登录墙。
- HTTP(S) 原始 URL、重定向链每一跳、浏览器最终 URL和所有浏览器子请求都执行公网 URL 检查。
- 拒绝本机、私网、链路本地、保留地址、组播地址、非 HTTP(S) 协议及带用户名/密码的 URL。
- HTTP 重定向改为受控手动跟随，避免客户端先访问私网中间跳再检查最终 URL。

### 2.5 配置与 dependency preflight

新增配置：

- `BROWSER_SEARCH_ENABLED`
- `BROWSER_SEARCH_URL_TEMPLATE`
- `BROWSER_EXECUTABLE`
- `BROWSER_HEADLESS`
- `BROWSER_NAVIGATION_TIMEOUT_SECONDS`
- `BROWSER_RATE_LIMIT_SECONDS`
- `BROWSER_CACHE_TTL_SECONDS`
- `BROWSER_FETCH_FALLBACK_ENABLED`

显式使用 `browser_search` 时，Playwright 包或 Chromium/Chrome/Edge 缺失是阻断错误；浏览器仅作为 SearXNG/hybrid 的可选兜底时，缺失只产生警告，不会破坏旧工作流。

## 3. 修改文件

- `.gitignore`
- `.env.example`
- `app/config.py`
- `app/dependency_preflight.py`
- `app/main.py`
- `app/skills/browser_worker.py`
- `app/skills/content_extraction.py`
- `app/skills/fetch_gateway.py`
- `app/skills/public_research.py`
- `app/skills/research_audit.py`
- `app/skills/search_providers/__init__.py`
- `app/skills/search_providers/browser_search.py`
- `app/skills/verifiable_public_research.py`
- `scripts/check_config.py`
- `scripts/check_web_search_capability.py`
- `tests/test_browser_search_phase2.py`

## 4. 验证结果

### 4.1 确定性测试

- Phase 2 新增测试及 Search/Fetch、dependency preflight、WF-3 既有回归：`214 passed in 9.55s`。
- 覆盖 Browser 结果解析、原始页面留存、CAPTCHA 分类、SearXNG→Browser fallback、缓存仍生成新 receipt、私网重定向拒绝、JS shell 浏览器兜底、blocked 页面 `SNIPPET_ONLY`、URL credentials 拒绝和浏览器依赖预检。
- 本批 Python 文件 Ruff：`All checks passed`。
- Python `compileall`：通过。
- `git diff --check`：通过，仅有仓库既有换行提示。

### 4.2 无 LLM 真实 Web Search

执行：

```powershell
py scripts/check_web_search_capability.py --query "human AI collaborative decision making research" --limit 5
```

最终结果：

- `status=PASS`
- `llm_invoked=false`
- provider：`browser_search`
- engine：`search.brave.com`
- HTTP：`200`
- 真实网页命中：`5`
- 最终 receipt：`data/capability_tests/web_search/20260902T112128.018504_0000/capability_receipt.json`
- 原始结果页：`data/capability_tests/web_search/20260902T112128.018504_0000/raw_search_pages/CAPABILITY-WEB-001-browser-search-8a666bf067f340d9.html`
- 原始 HTML SHA-256：`8c4a027242a0fc877af29884695d72e1107b68f1362d284678702f862690e371`

测试过程也验证了失败不被伪装：Bing 返回“请解决以下难题以继续”时未产生结果；DuckDuckGo 超时时记录为导航失败；一次 DNS 返回保留 IPv6 地址时被安全策略拒绝。最终在公网 DNS 正常时 Brave Search 成功。所有 capability artifacts 位于已忽略的 `data/capability_tests/`，不会被误提交。

### 4.3 全量测试基线

全量套件尝试在既有 Argument `candidate.review_units` 历史契约测试处失败；该失败已在 Phase 0/1 报告中记录，本批未修改 Argument、WF-4 或其 Model Schema。为避免继续进入既有长时间 Full Integration 超时，本批以 214 项 Search/WF-3 相关回归作为范围验收。

## 5. 尚未实施

- Phase 3：现有 WF-3 provider profile、required channel、搜索命中/正文读取/证据可用三级充分性合同。
- Phase 4：独立 `WF-3B_TOPIC_BACKGROUND_RESEARCH` 及其新 Prompt、Prompt JSON Schema、Model Schema、Replay 和 UI。
- Phase 5：WF-3B lineage 与 WF-4 Argument/Revision Plan/Section Contract/Writing 的确定性背景证据路由。
- Phase 6：完整 mutation/replay/lifecycle/recovery 和经用户授权后的少量 LIVE 模型闭环。

## 6. 下一批准确修改范围：Phase 3

下一批只升级现有 WF-3，不混入 WF-3B 或 WF-4：

1. 在 `prompt_pack/prompts/public_research/research_plan.md` 及对应 Prompt/Model JSON Schema 中增加 `required_channels`、`provider_execution_requirements`、`minimum_fulltext_sources_per_query`、`allow_snippet_only`、`require_web_discovery`。
2. 在 `app/skills/research_plan.py` 与 `app/skills/research_execution.py` 中规范化并锁定上述执行合同，代码确定 provider 路由和查询执行，不交给模型生成运行时 ID、Hash 或状态。
3. 在 `app/skills/verifiable_public_research.py` 中实现有序 provider profile 和 required web channel 执行记录。
4. 在 `app/skills/research_quality.py`、`app/skills/research_audit.py`、`app/skills/research_validation.py` 中分离 `search_hits`、`readable_documents`、`usable_evidence`，禁止 Academic 成功掩盖 required Web Search 失败。
5. 更新 `prompt_pack/replay/cases/public_research_plan/`、WF-3 契约/质量门禁测试和历史回放；不新增数据库表。
