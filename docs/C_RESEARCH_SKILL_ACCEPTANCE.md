# 轨道 C：Research Skill 与可核验公开资料验收说明

> 对应计划：`docs/CONCURRENT_DEVELOPMENT_PLAN.md` 的 C1—C6。  
> 所有者：Research Skill。  
> 安全边界：本轨道不修改安全分类、外发审批、模型路由、WF-1/WF-3/WF-5 安全步骤，也不修改写作 Agent 的正文生成逻辑。

## 1. 交付范围

| 工作包 | 实现 | 确定性验收 |
|---|---|---|
| C1 研究计划 | 将研究问题、查询、来源优先级、时间范围和证据要求规范化；LIVE 能力运行启用严格计划校验 | 无查询、宽泛查询、未绑定研究问题、缺失来源策略或时间范围时在检索前阻断 |
| C2 真实公开检索 | 支持 SearXNG、批准连接器和明确标识的受控记录集；保存原始响应、来源快照、提取文本、元数据、访问时间和 SHA-256 | 归档生成后立即重算哈希；文件缺失或哈希不一致时失败 |
| C3 去重与权威排序 | 依次使用规范化 DOI、去跟踪参数后的规范 URL、标题/发布者和正文哈希去重；按来源类型、权威分和年份排序 | 同源重复不会进入来源库；同一 DOI 的题名/年份冲突作为结构化问题保留 |
| C4 Claim 绑定 | 在 `P-PUBLIC-RESEARCH-SYNTHESIS` 后、导入审查前，确定性校验每个 `PUBLIC_CLAIM` 的来源 ID、来源类型、快照哈希和引文 | 未知来源、伪造哈希、找不到的引文或未绑定证据直接阻断；报告区分“原文直接支持”和“模型综合” |
| C5 最近工作与基线覆盖 | 对每个查询统计来源覆盖，并独立检查最近五年工作、可比较基线和局限机制 | 创新类主张缺少任一维度时阻断，不允许只靠形容词声明创新 |
| C6 异常处理 | 结构化记录抓取失败、查询未覆盖、证据不足、重复与来源冲突 | 不补写不存在的 DOI、题名或来源；来源冲突必须在综合结果中显式保留 |

## 2. 归档结构

```text
data/research_archive/<project_id>/<session_id>/
├── manifest.json
├── source_index.csv
├── connector/
│   └── connector_response.json
├── raw/
│   └── <source_id>.*
├── text/
│   └── <source_id>.txt
├── metadata/
│   └── <source_id>.json
└── claim_bindings/
    └── claim-binding-<synthesis-hash>.json
```

`manifest.json` 保存规范化研究计划、计划校验结果、来源记录、重复项、异常、查询覆盖、最近工作/基线/局限覆盖和归档路径。每个来源同时保存：

- 原始 URL、最终 URL和规范 URL；
- DOI、作者、发布者、发布日期和来源类型；
- 检索词、访问时间、连接器验证信息；
- 原始快照、提取文本、元数据路径；
- 原始快照 SHA-256 与文本 SHA-256；
- 权威等级、最近工作标记、基线与局限证据标记。

## 3. Claim—证据绑定

运行时不改写模型输出。`PublicResearchService.validate_synthesis()` 只生成确定性校验报告：

1. `claim_id` 必须唯一，且类型必须为 `PUBLIC_CLAIM`；
2. 每项 Claim 至少绑定一个实际归档的 `PUBLIC_SOURCE`；
3. `source_hash` 必须与归档快照哈希一致；
4. 声称为原文引文的文本必须能在归档题名或摘录中找到；
5. 来源比较只能引用归档来源；
6. 归档存在来源冲突时，综合结果不得省略冲突；
7. 创新类主张必须同时具有最近工作、可比较基线和局限机制证据。

校验失败时，工作流在公开研究综合步骤后进入 `BLOCKED`，不会进入导入 Gate，也不会把无效 Claim 注入写作上下文。

## 4. 验证命令

```bash
python -m pytest -q \
  tests/test_research_skill_track_c.py \
  tests/test_v05_skills_and_privacy.py

python scripts/verify_research_archive.py \
  data/research_archive/<project_id>/<session_id>/manifest.json
```

完整回归仍执行：

```bash
python scripts/validate_g0.py
python -m compileall app scripts
python prompt_pack/tools/validate_pack.py
python -m pytest -q
```

## 5. 兼容性说明

- `sources` 与 `passages` 保持原有 Prompt Schema 形状，新增审计字段放在 Research Skill 输出和归档 Manifest 中；
- REPLAY、MOCK 和 `SIMULATED_EMPTY` 仅做编排回归，不冒充真实公开研究能力；
- LIVE 运行强制执行完整 C1 计划契约；旧版直接组件测试仍可使用兼容模式，但会记录计划警告；
- Prompt、Schema 与安全冻结路径未因本轨道修改。
