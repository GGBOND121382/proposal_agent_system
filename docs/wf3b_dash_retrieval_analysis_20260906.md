**WF-3B DASH 网页检索漏检分析与检索智能体选型（2026-09-06）**

结论：搜索服务曾返回 DASH 直接资料，但适配器在相关性筛选之前截断了结果，导致这些页面未进入候选池。与此同时，查询规划缺少实体核验，相关性判定把跨领域论文计为 DASH 直接证据，检索健康与充分性指标没有准确暴露目标实体的缺口。仅更换 query 生成提示词不能解决全部问题。

分析对象为 `data/exports/wf3b_retrieval_io_wf-66172154c37249b4_20260906.zip`。已逐文件核对 ZIP 与同名解压目录，260 个文件内容一致。实际检索发生于 2026-09-04，9 月 6 日是打包日期；公开资料检索时间范围为 2021-09-04 至 2026-09-04。下面的本地证据路径相对于仓库根目录。

**1. 首要原因：已经返回的相关网页在进入候选池前被截断**

`Q-APP-SC-02` 的 SearXNG `provider_runs[].raw_response.results` 包含 29 条结果，以下序号为该原始数组从 1 开始的位置：

| 原始位置 | 来源 | 内容及去向 |
| --- | --- | --- |
| 13 | Aerospace America / AIAA | 标题为 The U.S. military, human-machine teaming and decision dominance，摘要明确包含 DASH 全称；未进入候选池 |
| 15 | af.mil | Human-machine teaming in battle management: A collaborative effort across borders，DASH 3 官方报道；未进入候选池 |
| 29 | csiac.dtic.mil | Air Force DASH Sprint Pioneers Human-Machine Teaming for Faster Battle Management Decisions；未进入候选池 |

这三条均由 Brave 返回。原始响应第 1、3、5、6、8 条却是 Bing 的 Human 百科、词典、歌曲和保险页面。

直接原因位于 `app/skills/search_providers/searxng.py:112`：

```python
for index, item in enumerate(results[: min(10, per_query_limit)]):
```

本次 `per_query_limit=8`，因此第 9 条起全部无法进入后续筛选。即使只把配置提高至 30，上述代码仍最多保留 10 条，仍会漏掉第 13、15、29 条。

整个运行的网页原始响应有 259 条结果，进入标准化候选池的网页结果只有 176 条。470 条候选是截断后的混合渠道总数，不能当作搜索服务原始结果总数。470 条候选的标题、URL、摘要和正文载荷中未发现 DASH 独立词或全称；但完整原始响应存在上述 3 条线索。

证据：`data/exports/wf3b_retrieval_io_wf-66172154c37249b4_20260906/validation/04_discovery_manifest.json:488677` 为 DASH 3 URL，`:489062` 为 CSIAC URL；相同数据也保存在 `discovery/all_provider_requests_responses_470_candidates.json`。

离线验证：使用当前 `assess_candidate_relevance()`、该运行保存的 query relevance profiles，以及这三条原始标题/摘要重新评估，三条均得到 `DIRECT` 和 `qualifies_for_coverage=true`。这证明它们最先丢失于候选截断环节；不代表放开截断后一定会在所有后续排序、抓取步骤中成功归档。

**2. 查询规划：维度齐全，但没有先识别 DASH 是什么**

22 条 query 中只有 `Q-APP-SC-01` 包含 Dash：

```text
USAF Dash system operational deployment scenarios human-machine teaming aerial combat decision support public documentation 2021-2026
```

它把实体名、部署假设、使用场景和资料要求堆在同一条查询中。其余查询扩展到市场、政策、AlphaGo、VISTA、Maven、TRUSTER 等；没有 DASH 全称查询、官网定向查询、DASH 1/2/3 系列查询，也没有核实“system / operational deployment”是否为合适的对象描述。

官方全称为 **Decision Advantage Sprint for Human-Machine Teaming**，官方将 DASH 描述为系列实验和软件冲刺。检索应先核实这个对象，再调查其进展；不能预先把已部署系统的部署资料设为主要召回目标。参见 [2025-06-16 美空军首轮 DASH 报道](https://www.af.mil/News/Article-Display/Article/4218100/air-force-dash-pioneers-human-machine-teaming-for-faster-battle-management-deci/)。

现有 `prompt_pack/prompts/background_research/research_plan.md:28` 要求覆盖每个背景维度，却没有要求实体消歧、别名核验、短查询、试搜反馈。`research_plan_critic.md:11` 仅审查范围、禁止推断和维度匹配，未承担查询召回质量审查。

**3. 检索渠道：严重降级和明显跑题结果未影响总体健康状态**

22 次 SearXNG 执行均标记为 `DEGRADED`。360search 和 DuckDuckGo 分别失败 22 次，Brave 失败 20 次；另有 Crossref 429 错误 3 次。含 Dash 的查询只得到 Bing 的美空军首页、招聘、百科等泛化结果。

Bing 在多条查询中返回与首个词相符、与完整问题明显不符的结果，例如 `human-machine...` 返回 Human 词典和歌曲，`mission commander...` 返回 Mission Lane 信用卡。这是搜索有效性异常的证据，但不能仅凭导出包确定其内部原因是查询简化、适配问题、缓存或其他行为。应用代码 `searxng.py:76` 发送的是完整 `query.query`，原始响应也回显完整 query。

`app/skills/research_quality.py:363` 将有结果的 `DEGRADED` 视为执行成功，因此汇总显示 SearXNG 成功 22/22、retrieval health 为 PASS。这可以说明通道曾返回数据，不能说明搜索结果相关或引擎健康。建议分别报告通道可用性、引擎降级和检索有效性。

本次曾尝试访问本地 `127.0.0.1:8888` 做同引擎对照，连接被拒绝。因此没有完成当前 SearXNG/Bing 的线上复现；关于该次运行的判断来自保存的原始响应和当前代码，不把外部搜索工具的成功等同于同一后端复现。

**4. 相关性与覆盖：通用词重合被当成目标实体证据**

DASH query 的 `domain_anchors` 是 `decision / human / machine / operational`，没有 `DASH` 或 `USAF`。原因是 `research_quality.py:108` 以同研究问题下不同 query 的重复词生成 anchors，单条 query 才出现的专名容易丢失。

`research_quality.py:192` 在命中 anchor 且重合词不少于 5 个时给出 `DIRECT`。于是以下论文被标为 DASH 查询的直接证据：

- 住宅翻修决策支持：Renovation Decision Support System for Residential Buildings…
- 电网控制室：Designing for human-AI teaming in power system control room decision support。
- 卒中决策支持：From Explanation to Clinical Feasibility…Stroke Decision Support。

该查询最终被计为 5 条来源、3 条权威来源，见 `validation/07_coverage.json:5`；但这些来源没有建立 DASH 实体关联。住宅论文在 `validation/06_source_catalog.json:363`，并不是被拒绝的候选。

充分性报告总体为 `DEGRADED / INSUFFICIENT`，只列出两条其他查询的深度缺口，`may_continue=true`；没有单独识别 DASH 缺少直接证据。最终 synthesis 注意到了 DASH 缺口，但没有在此导出链路中形成对应的实体定向补查。其“Web search hit count (4)”指向最终 4 条网页渠道来源，不能替代 176 条网页候选、259 条原始网页结果这两个口径。

**5. 额外问题：全文指标不能证明已抓取全文**

80 条归档记录的 `fetch_mode` 均为 `PROVIDER_PAYLOAD`，`extractor=PROVIDER_TEXT`，`content_type=application/json`。`public_research.py:414` 的 connector 路径直接使用 provider 的 `content_text/page_text/excerpt/abstract`，未进入后面的 URL 抓取分支。

`research_audit.py:520` 以文本长度不少于 1000 字符计为 full_text_documents，得到 57；`research_quality.py:647` 的 query_fulltext_depth 直接检查 source_count。因此“57 条全文”不能据此解释为 57 个已下载并提取的原网页/论文全文。修复时应根据实际抓取来源、提取方式和文档类型区分正文、摘要和搜索片段。

**6. 已核实可用的直接网页资料**

| 发布日期 | 页面 | 用途 |
| --- | --- | --- |
| 2025-06-16 | [首轮 DASH 官方报道](https://www.af.mil/News/Article-Display/Article/4218100/air-force-dash-pioneers-human-machine-teaming-for-faster-battle-management-deci/) | 核实全称、对象性质、组织和实验方式 |
| 2025-09-19 | [DASH 2 官方报道](https://www.af.mil/News/Article-Display/Article/4310090/air-force-experiments-with-ai-boosts-battle-management-speed-accuracy/) | 系列进展与实验观察 |
| 2026-01-05 | [DASH 3 官方报道](https://www.af.mil/News/Article-Display/Article/4371071/human-machine-teaming-in-battle-management-a-collaborative-effort-across-borders/) | 第三轮实验、合作参与和局限；已存在于本次原始响应 |
| 2026-06-30 | [由前三轮 DASH 向 MASH 演进的官方报道](https://www.af.mil/News/Article-Display/Article/4530561/space-force-integrates-with-air-force-in-ai-sprint-to-ensure-mission-dominance/) | 跟踪名称变化和后续整合 |

这些页面的发布日期都落在原计划时间范围内。官方实验报道可以证明项目/实验存在及公开披露的进展，不能自动推导为已完成作战部署。

**7. 可直接使用或参考的检索智能体**

| 方案 | 已核实的机制 | 对本项目的建议 |
| --- | --- | --- |
| [GPT Researcher](https://github.com/assafelovic/gpt-researcher) | Python SDK 支持 topic/query 输入后研究与报告；`ResearchConductor.plan_research()` 先搜索，再把初搜结果送入子查询规划 | 希望使用现成完整研究能力时优先评估；只需 query 时，封装其规划部分并适配现有 schema |
| [dzhng/deep-research](https://github.com/dzhng/deep-research) | TypeScript；`generateSerpQueries()` 返回 query + researchGoal；`processSerpResult()` 生成 learnings 和后续问题，再递归搜索 | 适合借鉴轻量循环，移植到当前 Python 流程；不是现成的 Python 查询生成库 |
| [LangChain Open Deep Research](https://github.com/langchain-ai/open_deep_research) | 支持研究任务分解、工具调用、结果反思和补搜，可配置模型与搜索工具 | 适合参考架构；截至本次核查，仓库已于 2026-08-21 归档，不建议未经评估作为新增长期核心依赖 |

具体源码入口：[GPT Researcher 规划流程](https://github.com/assafelovic/gpt-researcher/blob/master/gpt_researcher/skills/researcher.py)、[generate_sub_queries / plan_research_outline](https://github.com/assafelovic/gpt-researcher/blob/master/gpt_researcher/actions/query_processing.py)、[dzhng 查询与反馈循环](https://github.com/dzhng/deep-research/blob/main/src/deep-research.ts)、[LangChain 搜索反思提示词](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/prompts.py)。

这里的推荐基于源码机制与当前项目适配成本，没有对三个项目运行统一效果基准，也没有安装或接入它们。GPT Researcher 的研究 SDK 和“只输出 queries 的稳定公共接口”应区分：上面的子查询函数属于仓库内部模块，封装时需要固定版本并做兼容验证。

**8. 建议的 topic → queries 工作流与改动顺序**

对外可以保持只输入一个 topic；内部采用“实体候选识别 → 少量试搜 → 基于来源核实名称与对象类型 → 按问题生成短 query → 搜索并阅读 → 按缺口补搜”的循环。首次规划可用模型知识提出候选全称，但正式实体绑定必须由来源核实。

本次优先级建议如下：

1. **先修候选丢失。** 保留 provider 完整原始结果到标准化候选池，在去重、目标实体相关性评估后限制昂贵的抓取数量；把原始条数、截断条数、入选条数分别记账。为目标实体来源保留容量，并对严重跑题的引擎做降权或切换。只增加最后的 80 条归档上限无效。
2. **修实体相关性与覆盖。** 独立保存目标实体、已核实别名、关联机构和来源角色。DASH 专题查询的直接证据必须实际建立实体关联；一般人机协同论文可以支撑背景，不能填充 DASH 的直接证据计数。不要只检查单个缩写词，因为 DASH 本身可能存在同名歧义。
3. **加入试搜后的查询规划与补查。** 优先借鉴 GPT Researcher 的初搜规划，或 dzhng 的 learnings/后续问题循环；复用现有 query_id、维度绑定、搜索网关和归档机制。补查应记录原 query 和新增 query 的沿革，遵守原任务范围，并复用现有范围审查。
4. **修全文与充分性判断。** 区分搜索片段、学术摘要、抓取正文和可引用证据；单独检查实体覆盖、关键问题覆盖和独立来源，避免转载多次计为多份独立证据。预算内补查，预算用尽时明确返回未解决缺口。

以下 query 是适用于本 topic 的示例，前几条用于识别对象，后几条应在前序结果核实名称后执行；本次未逐条在原 SearXNG 后端验证：

```text
"DASH" "Air Force"
"DASH" "decision advantage"
"Decision Advantage Sprint for Human-Machine Teaming"
site:af.mil "DASH" "human-machine"
site:afresearchlab.com "DASH"
site:af.mil "DASH 2"
site:af.mil "DASH 3"
"DASH" "ABMS" "ShOC-N"
"DASH" "711th Human Performance Wing"
"DASH" "Transformational Model" "battle management"
site:af.mil "DASH" "lessons"
site:af.mil "MASH" "DASH"
```

每条 query 建议同时保存 purpose、entity_ids、dimension、source_role、expected_evidence 和 parent_query_id。是否“足够完善”应根据搜索后的实体与问题覆盖判断；一次性生成更多 query 无法保证召回充分，也无法补救已经返回但被适配器丢弃的结果。

本次只新增此分析文件，未修改运行代码、检索配置或原始归档。
