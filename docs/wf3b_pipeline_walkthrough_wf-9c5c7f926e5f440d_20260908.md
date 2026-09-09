# WF-3B 调研流程实录：wf-9c5c7f926e5f440d 从输入到调研结果

> 日期：2026-09-08。本文以真实运行 `wf-9c5c7f926e5f440d`（WF-3B_TOPIC_BACKGROUND_RESEARCH）为例，说明"输入材料 → 检索方向 → 渠道执行 → claim 生成 → 下游使用"的完整链路。所有数据均可从 `data/proposal_agents.sqlite3`、`data/research_archive/project-8595abee7b9c4047/` 和 `data/model_calls/` 复核。

## 1. 输入材料与各自的作用

用户在创建 WF-3B 工作流时（前端或 `POST /api/workflows`）提供三项输入，存于 `state.options`：

| 输入 | 本次实际值 | 作用 |
|---|---|---|
| `topic`（调研主题） | 面向空军作战的人机协同决策优势冲刺关键技术研究 | 唯一直接的研究对象描述，驱动所有检索方向的生成 |
| `required_dimensions`（必需背景维度） | 8 个维度全集 | 冻结调研必须覆盖的方向；模型只能在这 8 个维度内计划，不得自创维度 |
| `allowed_public_topics`（允许调研内容） | 人机协同决策 / 美空军DASH系统 / 人在回路智能系统 / 智能化战争 / 智能体系统 / 大模型 | **范围闸门**：查询和结果审查的允许边界，见 §3.2 |

8 个冻结维度（`app/background_research.py:49` `BACKGROUND_DIMENSIONS`）：

```
APPLICATION_SCENARIO      应用场景          STAKEHOLDER_AND_PAIN     利益相关者与痛点
INDUSTRY_SCALE_AND_TREND  规模与趋势        POLICY_STANDARD_AND_PROGRAM 政策标准与项目
REPRESENTATIVE_CASE       代表性案例        CURRENT_ADOPTION         当前采用度
OPERATIONAL_CONSTRAINT    运行约束          RESEARCH_SIGNIFICANCE    研究意义
```

## 2. 工作流步骤总览

WF-3B 共 8 步（`app/workflow_defs.py:32`），本次运行状态如下：

| 步骤 | 节点 | 本次结果 |
|---|---|---|
| 1 | P-SAFE-ONLINE-PACKAGE（出境安全包） | PASS（第 2 次尝试） |
| 2 | P-SAFE-ONLINE-PACKAGE-CRITIC | 初审 REVISE → 通过后继续 |
| 3 | P-BACKGROUND-RESEARCH-PLAN（调研计划） | PASS |
| 4 | P-BACKGROUND-RESEARCH-PLAN-CRITIC（计划审查） | PASS |
| 5 | PUBLIC_SEARCH（确定性检索执行） | 两轮检索 + 归档合并 |
| 6 | P-BACKGROUND-RESEARCH-SYNTHESIS（合成 claim） | 修复后 PASS（26 条 claim） |
| 7 | P-BACKGROUND-RESEARCH-CRITIC（证据充分性审查） | PASS |
| 8 | P-ONLINE-RESULT-IMPORT-CRITIC + 导入 Gate | **当前状态：WAITING_GATE，等待人工批准导入** |

## 3. 检索方向是如何从输入产生的

### 3.1 维度 → 研究问题 → 查询

计划节点（P-BACKGROUND-RESEARCH-PLAN）把每个冻结维度实例化为一条固定模板的研究问题，再为每个问题生成约 3 条检索查询。本次的 8 条研究问题全部是同一模板：

> "核验背景维度 {DIMENSION} 的公开事实、代表性来源与适用边界"

每个维度到查询的实际映射（`query_items`，共 24 条；`binding_basis: EXPLICIT` 表示显式绑定）：

| 维度 | 查询 |
|---|---|
| APPLICATION_SCENARIO | `US Air Force DASH autonomous decision support system operational scenarios human-machine teaming`；`人机协同决策系统 空军作战 智能化指挥控制 应用场景`；`intelligentized warfare decision support human-on-the-loop applications military operations` |
| STAKEHOLDER_AND_PAIN | `Air Force operators challenges decision support AI limitations trust workload`；`human-AI decision making collaboration pain points cognitive overload situation awareness breakdown`；`military decision support system user requirements operator needs uncertainty management` |
| INDUSTRY_SCALE_AND_TREND | `human-machine teaming market size growth defense AI investment 2023 2024 2025`；`defense AI autonomous systems funding trends DoD Air Force research investment`；`多智能体系统 大语言模型 自主决策 技术成熟度 发展趋势 2024 2025` |
| POLICY_STANDARD_AND_PROGRAM | `DoD AI ethics guidelines human-autonomy teaming policy directive autonomous weapons`；`美国空军 智能化作战 人机协同 政策标准 规划 项目`；`NATO allied human-machine teaming standards interoperability requirements military AI` |
| REPRESENTATIVE_CASE | `DASH system AFRL demonstration case study autonomous decision support results`；`human-in-the-loop AI military pilot program demonstration deployment results`；`有人-无人协同作战 自主系统 试点 案例公开报道` |
| CURRENT_ADOPTION | `current adoption maturity level AI decision support military operational deployment`；`multi-agent LLM systems benchmarks evaluation frameworks current capabilities limitations`；`human-agent collaboration frameworks comparison ChatDev AutoGPT AutoGen operational readiness` |
| OPERATIONAL_CONSTRAINT | `real-time AI decision support latency constraints bandwidth military operations`；`defense AI data limitations adversarial robustness security constraints operational`；`human-machine collaborative decision uncertainty quantification explainability requirements` |
| RESEARCH_SIGNIFICANCE | `why human-AI teaming research matters air force combat effectiveness competitive advantage`；`functional decomposition state management human-agent coordination research gaps opportunities` |

另有 1 条**反馈补查**查询（见 §4.3）：`multi-agent systems large language models autonomous decision technology readiness trends 2024 2025`（绑定到规模与趋势维度）。

语言策略：每个维度中英查询混合（24 条中 4 条中文），中文查询由 searxng 路由到中文引擎（`PUBLIC_SEARCH_ENGINES_ZH/EN` 配置，`app/skills/search_providers/searxng.py`）。

### 3.2 用户手填的"允许调研内容"起什么作用

`allowed_public_topics` 是**范围约束**，贯穿三个环节：

1. **出境安全包**：步骤 1 构造对外请求的 `safe_online_package` 时，`allowed_topics` 被替换为该列表（`app/context_base.py:3360-3372`），模型只能围绕这些公开主题构造请求；
2. **计划审查**：计划 critic（步骤 4）对照允许主题检查每条查询不越界；范围外的查询被拒绝；
3. **补查闸门**：反馈轮新增的查询必须通过同样的范围审查才会追加执行（计划锁机制，见 `state.background_research_plan_lock`）——本次补查的 1 条查询即通过审查后加入。

它还是隐私防线的一部分：检索内容被限定在用户明确批准的公开主题内，不得携带项目内部信息出境。

## 4. 检索执行：渠道与结果构成

### 4.1 执行渠道

检索由运行时确定性执行（非模型自由发挥），启用的提供方（`retrieval_health.enabled_providers`）：**openalex、crossref、searxng、browser_search**；academic-multi-source 与 semantic_scholar 处于禁用状态。

合并归档共 **93 条来源记录**，按发现渠道：

| 渠道 | 记录数 | 说明 |
|---|---|---|
| connector（学术 API：OpenAlex/CrossRef） | 66 | 论文、报告元数据为主 |
| browser_search（Playwright 浏览器检索） | 26 | 网页结果，含 DASH 官方报道 |
| searxng（自建元搜索） | 1 | 本次仅 1 条中文综述存活到归档 |

按抓取方式（`fetch_mode`）：

| fetch_mode | 数量 | 含义 |
|---|---|---|
| PROVIDER_PAYLOAD | 51 | 仅有提供方返回的元数据/摘要 |
| HTTP | 20 | 直接 HTTP 抓取到正文 |
| SNIPPET_ONLY | 17 | 只有搜索摘要片段 |
| PLAYWRIGHT_RENDERED | 5 | 浏览器渲染兜底（用于 403/反爬页面，如 AIAA） |

其中 **25 条取得全文**（`full_text_available`），全文以哈希钉住的快照文件存于归档（`text/` 目录，`text_sha256` 校验）。

### 4.2 关键来源举例（DASH 直接相关）

| 来源 | 渠道 | URL |
|---|---|---|
| Air Force DASH pioneers human-machine teaming…（美空军官网首轮报道） | 归档命中 | af.mil/News/…/4218100 |
| Air Force experiments with AI, boosts battle management speed…（DASH 2） | 归档命中 | af.mil/News/…/4310090 |
| Air Force DASH Sprint Pioneers Human-Machine Teaming…（CSIAC 专题） | 归档命中 | csiac.dtic.mil/articles/… |
| AFRL 转载的 DASH 报道 | browser_search | afresearchlab.com/… |

### 4.3 两轮检索与归档合并

本次并非一次检索完成：

1. **首轮**：24 条查询执行后产生归档 `research-2f849806d0a5410d`（81 条来源）；
2. **反馈轮**：运行时根据首轮覆盖缺口生成反馈（`state.background_search_feedback.round = 1`），计划节点追加 1 条补查查询（经范围审查），执行第二轮；
3. **合并**：`merge_research_archives`（`app/skills/research_merge.py`）把两轮归档合并为独立的累积归档 `research-merged-69f06940beba4461`（93 条），原始两轮归档保持不可变，合并报告记录来源谱系（`merge_provenance`）。

## 5. 覆盖度与缺口（诚实结论）

合并归档的覆盖判定并**不是全绿**，这是设计使然——缺口必须显式呈现而非被掩盖：

- `coverage.status: INSUFFICIENT`；`research_sufficiency.status: DEGRADED`；
- 未覆盖查询（5 条）：含 3 条中文查询中的 3 条（`人机协同决策系统…`、`美国空军 智能化作战…`、`有人-无人协同作战…` 均无存活结果）及 2 条英文查询；
- 研究缺口示例：`research-gap-001`（Q-APP-SCENARIO-001 仅 2 条来源、要求 3 条，深度与全文不足）；
- 工作流完成语义为 **`COMPLETED_WITH_BACKGROUND_GAPS`**：协议走完 ≠ 每个维度都有充分证据。

## 6. Claim 的生成逻辑

步骤 6（P-BACKGROUND-RESEARCH-SYNTHESIS）的规则（`prompt_pack/prompts/background_research/research_synthesis.md`）：

1. **只能根据输入的 `evidence_passages`** 产出 `PUBLIC_CLAIM`，不得使用模型记忆补造统计数据、政策条文、案例细节、年份或来源；
2. 每条 claim 标注：`dimension`（8 维之一）、`target_section_profiles`（适用的申请书章节）、`conflicts`/`limitations`；未被覆盖的必需维度写入 `background_gaps`；
3. **证据卡（background_cards）由运行时确定性构建**（`build_background_cards`，`app/background_research.py:428`），模型不生成卡片；只有通过来源绑定校验的 claim 才会变成卡片；
4. 确定性校验（`validate_public_claims`，`app/skills/research_claims.py:103`）逐条检查：claim_type 必须为 PUBLIC_CLAIM、`source_id` 必须在来源目录中、`source_hash` 必须与归档快照哈希一致、`quoted_text` 必须能在来源摘要或**哈希钉住的归档全文**（经与模型输入一致的隐私脱敏后）中找到、创新类断言还需额外的证据维度门禁。

本次产物：**26 条 claim**，全部为 `DOCUMENT_EXTRACTED`，维度分布：应用场景 3、利益相关者 4、规模趋势 4、政策标准 3、代表案例 2、当前采用 3、运行约束 4、研究意义 3。26 条全部通过绑定校验且均为 `DIRECT_SOURCE_SUPPORTED`（引文在归档全文中可核验）。

示例（`C-APP-SCENARIO-001`）：claim_text 陈述 2025 年 4 月美空军首轮 DASH 实验的时间、地点、参与方与目标，绑定来源 `public-src-aa74a5c91f324f21`（AFRL 报道），引文可核验。

> 修复注记：本次运行曾卡在步骤 6。原因是模型把 `subject_id` 输出为带空格的自由文本、把 19 条 claim 误标为 `FACT`，以及引文核验只查摘要不查全文。已通过确定性规范化（slug 化、类型归一）和全文引文核验修复，详见 `docs/` 相关修复记录与 `app/executor.py` `_normalize_wf3b_synthesis_representation`。

## 7. 调研材料计划如何使用

1. **导入审批（当前位置）**：步骤 8 的 `ONLINE_RESULT_IMPORT_APPROVAL` Gate 由安全审查角色人工决定 APPROVE / RETURN / REJECT / CANCEL。批准后调研结果才正式进入项目知识。
2. **持久化为背景成果**：`_persist_wf3b_background_result`（`app/workflows.py:278`）把卡片包、缺口、覆盖总结、来源目录、归档清单指针固化为 `TOPIC_BACKGROUND_RESULT` 工件（`state.wf3b_background_result_artifact_id`）。
3. **支撑后续申请书写作（WF-4）**：背景卡片按 `target_section_profiles` 路由到对应章节（如 APPLICATION_SCENARIO→PROJECT_OVERVIEW、STAKEHOLDER_AND_PAIN→NEED_ANALYSIS 等，映射见 `app/executor.py` 的 `profile_aliases`），为"项目概述/需求分析/背景与意义/文献综述"等章节提供带来源的事实基底；写作链路的证据卡与引用均可回溯到归档快照哈希。
4. **缺口显式传递**：未覆盖维度与不足证据作为 `background_gaps` 一并进入下游，下游不得用模型记忆填补——要么补查，要么在成稿中保持空白/提示。
5. **可审计与可展示**：全部 93 条来源的原始响应、全文快照、哈希与合并谱系存于 `data/research_archive/project-8595abee7b9c4047/`；前端"调研结果"面板可按会话浏览该清单（`GET /api/projects/{id}/research-archives/{session_id}/detail`）。

## 8. 一键速查

| 问题 | 答案 |
|---|---|
| 调研主题 | 面向空军作战的人机协同决策优势冲刺关键技术研究 |
| 检索方向来源 | 8 个冻结背景维度 → 8 条研究问题 → 24 条查询（4 条中文） |
| 允许调研内容的作用 | 出境安全包范围 + 计划/补查的范围审查闸门 |
| 渠道 | OpenAlex/CrossRef（connector）66 条、browser_search 26 条、searxng 1 条；25 条全文 |
| 检索轮次 | 2 轮（首轮 81 条 + 反馈补查），合并为 93 条累积归档 |
| claim | 26 条，全部 DOCUMENT_EXTRACTED 且 DIRECT_SOURCE_SUPPORTED |
| 覆盖结论 | INSUFFICIENT/DEGRADED，5 条查询未覆盖，缺口显式保留 |
| 当前状态 | 步骤 8 WAITING_GATE，等待导入审批 |
