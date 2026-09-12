# WF-1 手动文种与调研受理简化：交接文档

交接日期：2026-09-10。

## 1. 用户要求与停止边界

用户当前目标：让《美空军DASH系统调研分析报告》这份任务说明能通过 WF-1 受理，再由后续智能体开展检索与报告分析；不要要求用户预先写出调研答案。

用户明确允许简化：

> 我可以手动指定材料类型，这一份就是调研报告，不要再让程序自己判断了。如果太难的任务，可以先跳过。

用户最后指令：

> 不要再扩大修改范围了，要查让其他智能体查，立刻写交接文档。

因此本轮已停止代码修改、测试和调查。交接之后的实现、排查和线上运行由下一位智能体接手。**不要把本文视为 WF-1 已跑通的验收报告。**

本轮没有重启服务、修改实际项目配置、重建工作流或调用真实模型验证；没有修改用户原始输入材料。修改还留在工作区，没有提交 Git。

## 2. 环境与需要接续的实际对象

- 工作区：`D:\VSCodeWorkspace\proposal_agent_system`
- Shell：Windows PowerShell。
- Python：使用 `py -3`，不要使用可能指向 Inkscape 的 `python`。
- 中文输出：`$env:PYTHONIOENCODING='utf-8'`。PowerShell 管道中的中文字面量可能被编码替换；处理中文文件优先显式 UTF-8，或通过补丁工具写文件。
- 本轮开始时 HEAD：`4b4d716`，提交说明 `fix bugs (partial)`。
- 本轮开始时仅两个 uvicorn 日志文件已修改；之前另一个智能体的实现已经包含在 HEAD。不要回退这些既有实现。
- 数据库：`data/proposal_agents.sqlite3`。
- 当前 DASH 项目：`project-d39c11d09c7445d3`。
- 当前材料：`doc-d06185783c2e426d`，`PROJECT_BRIEF_美空军DASH系统调研分析报告.md`。
- 最近一次已审计失败流程：`wf-1ecfb26b024c4578`，`WF-1_PROJECT_INTAKE`，`BLOCKED_CONTENT`，内部 `current_step=4`。这是历史审计快照，不保证交接时仍是数据库最新实例。
- 该节点：`P-PROJECT-DEFINITION-EXTRACT`。安全分类及规则抽取/审查已经通过，不能说它停在内部第 0 个模型节点。
- 旧 v3 项目：`project-8595abee7b9c4047`。
- 旧 v3 完成的 WF-1：`wf-e5775eba63c04057`。

实际失败响应：

`data/model_calls/responses/call-provider-9f1c7e570c60dfc5db5e4891-cycle-cdff224563fcf084-attempt-1.parsed.json`

对应请求：

`data/model_calls/requests/call-provider-9f1c7e570c60dfc5db5e4891-cycle-cdff224563fcf084-attempt-1.json`

对应运行：`run-234dc6ba39ef4a2c`。

最终运行时判定保存在 `artifacts` 表，ID `artifact-06e5799111724a1e`；读取 `content_json.guard_report` 和 `decision_basis`，不要仅看 `prompt_runs.output_json.status`。

## 3. 已查实的原因：避免下一位重复误诊

### 3.1 手动类型尚未接入时，后端把调研报告误判成申请书

模型实际返回 `document_kind=SURVEY_REPORT`。

但 `app/proposal_quality.py::_document_kind_hint` 原先只扫描 `scheme_profile` 中的 `scheme_name`、`scheme_type`、`research_attribute` 及少量人工回答，不使用模型的结构化文种。

实际画像：

```text
scheme_name: 美空军DASH系统调研分析报告
scheme_type: 调研分析报告（项目内部立项任务）
research_attribute: 非指南类任务书，不涉及指南方向属性
```

函数优先匹配“申报/指南/任务书”等词；否定处理只检查关键词的前一个字。“任务书”前面是“类”，因此判成 `APPLICATION`。审计中用真实输入执行该函数，确认得到 `APPLICATION`。

已有的 `RESEARCH_REPORT` 豁免分支因此没有生效。最后仍因缺少 `INNOVATION` 被阻断，尽管用户明确不要求我方研发与创新成果。

### 3.2 最终阻断也包含真实的图谱关系错误

最终 Guard 共 4 条阻断：

- `QG_PROJECT_GRAPH_INCOMPLETE`：缺少 `INNOVATION`。
- 两条 `DECOMPOSES_TO` 使用 `OBJECTIVE → OBJECTIVE`。
- 一条 `DECOMPOSES_TO` 使用 `OBJECTIVE → DELIVERABLE`。

当前关系约束要求该关系为 `OBJECTIVE → WORK_PACKAGE`。不能直接把非法关系当正确结果放行。

三次通过格式校验的候选，非法关系数量是 11 → 2 → 3。最后一次响应有 41 个条目、53 条关系。对调研任务受理而言负担过大。

### 3.3 修复请求缺少原候选

最后一次请求中 `existing_project_definition=null`，`revision_issues` 只有两条关系错误，引用运行时 `relation-...` ID。模型自身使用 `I1..In`，请求没有提供错误关系对应的原图、节点与映射。

因此“保持其他有效内容不变”的要求缺少执行上下文；重生成会修掉旧问题并引入新问题。**本轮没有修复这条通用修复链路。**

### 3.4 错误信息掩盖了实际原因

`app/workflows.py::_prepare_semantic_producer_regeneration` 原先在预算耗尽时统一显示“证据或研究链缺口；继续自动重试不会增加新的证据来源”。关系方向错误也使用这个文案。

本轮已改为展示当前待修缺陷的 description/code，保留预算耗尽和内容阻断状态。

### 3.5 旧 v3 的完成不能作为自动全通过基线

旧流程有 15 条运行记录，5 条 ERROR；经过 6 个 APPROVED Gate 才完成。最后一次 Gate 注释：

> 按用户授权：测试占位材料继续，接受当前缺口并完成字段规范修复验证。

旧材料还预先提供了中心命题、4 个研究问题、方法、创新假设和问题—任务—实验—指标映射，天然适配申请书引擎。新材料是调研任务说明，性质不同。

### 3.6 瘦身已有成效，不应整体撤销

此前约 503 KiB 的请求记录文件，最新同类记录约 32 KiB；最新记录中的 core_request_utf8_bytes 为 22108，模型输入估计 6500 tokens。这是记录口径，不等同于实际 HTTP 报文大小。

另一位智能体已把四个 WF-1 节点迁移为语义契约，ID、Hash、版本和来源元数据由代码补齐。请保留这些工作。

## 4. 本轮已经实现的改动

### 4.1 项目级手动选择，而不是逐文件分类

为减少改动，本轮选择让用户指定“项目最终要写什么”，配置字段：

```json
{"document_type": "SURVEY_REPORT"}
```

合法值：`SURVEY_REPORT`、`RESEARCH_PROPOSAL`、`ENGINEERING_PROPOSAL`。

这不是上传材料的 `role`；指南、证据材料、参考模板仍然保留原有角色。一个调研项目可以包含多种角色的输入。

涉及文件：

- `app/api_models.py`：`ProjectCreate.document_type` 可选，兼容旧 API；新增 `ProjectDocumentTypeUpdate`，只接受三个明确枚举。
- `app/main.py`：创建项目时保存配置；新增 `PATCH /api/projects/{project_id}/document-type`，修改该字段并记录 `PROJECT_DOCUMENT_TYPE_SET` 审计，保留其他配置。
- `app/static/index.html`：新建项目表单增加报告类型；当前项目增加报告类型选择与保存按钮。
- `app/static/app.js`：提交、加载和更新该字段；提示已有工作流应重建。

新建项目页面默认选科研项目申请书。旧项目没有该字段时保持旧行为；**这次没有做自动迁移或按项目名称自动识别 DASH**。

### 4.2 传到实际模型输入和确定性校验

- `app/context_base.py::_apply_common_payload`：若配置已指定类型且该 Prompt 的输入 Schema 支持，就写入 `payload.document_type`。使用 `_set_path_if_valid`，使 LIVE 上下文记录字段已被真实填写。
- 七个 canonical 输入 Schema 新增可选 `payload.document_type`：scheme extract/critic、project definition extract/critic、fact extract/critic、project readiness critic。
- 四个 semantic 模型输入 Schema 新增可选顶层 `document_type`：scheme extract/critic、project definition extract/critic。
- `app/model_semantic_contracts.py`：新增 `_wf1_document_type_input`，四个投影函数显式带上字段。fact/readiness 当前直接通过 canonical payload 获得字段。
- 项目定义 canonical 输出装配优先采用用户指定类型；SURVEY_REPORT 映射为现有合同枚举 `TECHNICAL_REPORT`，不修改下游合同枚举。
- `app/proposal_quality.py::_document_kind_hint`：手动类型优先于所有关键词判断。旧输入没有手动字段时仍用旧启发式逻辑。
- `_audit_readiness` 原来又独立使用默认申请书图谱检查，本轮也改为传递同一文种判断。

语义映射：

```text
projects.config.document_type = SURVEY_REPORT
  → WF-1 canonical payload.document_type
  → 四个语义节点的模型输入 document_type
  → Guard 使用 RESEARCH_REPORT 策略
  → proposal_contract.document_type = TECHNICAL_REPORT
```

**没有把所有后续工作流的 Schema 都加上字段。** WF-2/3/4/5 的完整文种贯通仍需后续审计。

### 4.3 调研受理简化：当前主要由 Prompt 约束

修改七个 Prompt：

- scheme extract/critic：指定为调研报告后，只整理范围、结构、篇幅和证据纪律；申报机构、立项评分等不适用，不重复追问用户。
- project definition extract：只整理调研对象、范围、要求和交付物，建议 3—8 个条目；`relations=[]`；保留一个概括性调研问题及必要的兼容字段，不构造创新实验论证链。
- project definition critic：允许小规模条目和空关系图；待检索技术结论不是受理缺项。
- fact extract/critic：任务要求用 REQUIREMENT/PLAN，不把“要求核查某技术”当作“已确认采用该技术”。
- readiness critic：本轮判断能否开始后续调研；允许待查事实，不能要求用户先给出答案；尚未完成研究时不宣称可交付正文。

**重要限制：还没有实现独立的轻量报告输出 Schema，也没有在运行时强制模型一定输出空关系图。** 模型不遵守简化提示时仍可能生成大图并触发真实关系错误。这里只是利用已有报告豁免和更明确的受理指令，不是伪造模型 PASS。

没有删除来源校验、真实事实约束、非法关系校验，也没有自动批准人工 Gate。

### 4.4 错误消息与版本

- `app/workflows.py`：语义重生成预算耗尽时，展示当前 gap/quality finding 的实际 description/code（最多 5 条），不再一律归因于缺少证据。没有扩大重试次数或改变阻断状态。
- `app/executor.py` 最后一批改动把 `OUTPUT_NORMALIZER_VERSION` 更新为 `2026-09-10.v60-manual-document-type`，避免变化后的 canonical 装配逻辑沿用旧归一化版本。

## 5. 验证结果及准确边界

新增测试文件：`tests/test_manual_document_type.py`。

已经完成并通过的命令：

```powershell
$env:PYTHONIOENCODING='utf-8'
py -3 -m pytest tests/test_manual_document_type.py tests/test_wf1_semantic_boundary.py tests/test_wf1_quality_gate_adaptation.py tests/test_current_project_context_regressions.py -q --disable-warnings --maxfail=3
```

当时结果：**48 passed in 27.05s**。

已覆盖：

- 手动类型覆盖真实案例中的“非指南类任务书”歧义。
- 四个模型投影带上类型，并符合输入 Schema。
- 用户指定调研报告时，模型误报申请书不能改变 canonical 合同类型。
- 小规模、空关系图的科研完整性检查只是非阻断观察。
- 去除真实来源后，`QG_CONFIRMED_ITEM_WITHOUT_EVIDENCE` 仍然阻断。
- 准备度检查使用同一文种；申请书仍保持原有图谱要求。
- 项目配置经过真实 ContextBuilder 进入输入（当时测试为 SIMULATED 模式）。
- HTTP API 创建、修改、读取、非法枚举 422、不存在项目 404，以及保留其他项目配置。

另外 `node --check app/static/app.js` 已执行成功；没有进行浏览器交互验收。

首次新增测试曾因使用 replay 中的占位来源 Hash 而失败，已改用临时数据库中的实际解析材料，再运行通过。没有为此放宽来源校验。

**48 项通过之后，还有以下最后一批调整尚未复测，随后用户要求立刻停止：**

1. ContextBuilder 检查字段是否存在从展开整份 Schema 改为 `_schema_for_path`，语义目标不变。
2. 测试增加 LIVE 模式 ContextBuilder 分支，并增加“旧项目不能凭 scaffold 生成手动类型”的检查。
3. 更新 `OUTPUT_NORMALIZER_VERSION` 到 v60。
4. 修改 `_document_kind_hint` 的说明文字。

因此不能说当前最终工作树已全部测试通过；应说“上一批 48 项通过，最后小批改动待复测”。

**尚未做：**

- 整套测试、Prompt Pack 全量校验及清单/校验和刷新。
- LIVE 模型端到端 WF-1。
- 服务重启和浏览器验证。
- 为真实 DASH 项目设置 document_type。
- 重建/推进真实工作流。
- 后续 WF-3 检索与 WF-4 报告交付验收。

## 6. 下一位智能体建议按此顺序接续

### 第一步：确认交接工作树，不重做已查实的问题

先查看本轮 diff，确认其他智能体是否继续写入。两个 uvicorn 日志是运行环境变化，不要纳入功能提交。优先复测上面的 48 项对应命令（新增测试使最终数量可能变化），以及新增 LIVE 上下文测试。

Prompt/schema 文件尚未升级各自 prompt_version，也未刷新 pack 的生成清单。请按仓库既有机制完成一致性验证；相关脚本 `scripts/refresh_prompt_pack_manifest.py`，不要仅生成一个写着 PASS 的清单就声称所有契约通过。可参考 `tests/test_runtime.py::test_prompt_pack_and_all_normal_replays`。

### 第二步：让真实项目采用用户明确指定的类型

服务加载新代码后，通过新页面或 API 设置：

```http
PATCH /api/projects/project-d39c11d09c7445d3/document-type
Content-Type: application/json

{"document_type":"SURVEY_REPORT"}
```

当前本轮没有代为执行。新设置影响后续上下文构造，不追溯改写已保存的模型响应与 Guard 记录。使用已有重建流程重跑，避免混用老规则画像和新配置。

实际运行前确认当前服务和工作流状态；不要直接复制未经核验的 PID 或杀死其他智能体正在使用的进程。

### 第三步：只验证受理，先别同时改完整报告写作引擎

检查真实 request 中出现 `document_type=SURVEY_REPORT`。检查 Guard 不再因 INNOVATION、EXPERIMENT 或节点数量不足阻断本报告。核对模型是否遵守小条目、空关系图约定，是否仍把公开检索任务当成必须人工补充的问题。

验收标准：材料已经说明研究对象、范围、交付要求和证据纪律，就能形成受理结果并进入后续研究；不要求先补我方研发方案、团队基础或创新假设。对真实引用错误仍须拒绝，不能用一键强制 PASS 获得“完成”。

若模型仍被科研骨架 Schema 拖累，可继续采用用户允许的简化：为报告受理提供更小的专用语义输出，只让模型产出主题、范围、待查问题、交付结构和约束，再由代码装配兼容对象。不要急着扩展通用知识图谱自动修复。

### 第四步：区分可跳过的难任务与不可伪造的事实

- 可以先跳过：完整科研论证图、我方创新链、多层研究任务与实验映射。
- 必须留给后续真实检索：DASH 模块、流程、技术实现、实验效果与其来源。
- 不能自动伪造：事实、引用、模型审查通过、人工批准。

通用修复链路（原候选、局部编号映射、最小 patch）属于后续独立任务。本轮未实现，不要以为错误文案改好就等于修复闭环已完成。

## 7. 其他注意事项

- `document_type` 当前只在七个 WF-1 canonical 输入中可用，安全分类节点不需要此字段。
- `SURVEY_REPORT → TECHNICAL_REPORT` 是兼容现有合同枚举的映射；后续节点若再次按文字猜测，仍可能丢失报告模式。
- 工程方案手动类型目前仍走既有严格申请书式质量策略，本轮没有承诺工程方案全链路适配。
- 新 API 没有冻结运行中工作流的类型快照；页面明确提示重建。运行中更改配置的语义隔离可后续单独处理，不要在本轮功能之外盲目扩展。
- Prompt 对“中心问题”“argument_seed”等兼容字段仍有残留负担；这是尚未推出专用报告 Schema 的已知边界。
- 准备度的 `ready_for_argument_architecture` 暂时沿用既有流程字段表达“可以进入后续规划”，不表示调研成果已充分。其更准确的阶段命名和下游兼容仍待处理。
- 用户已明确纯调研、公开资料、未知需标注；不要反复让用户确认这些已经给出的约束。

本次交接状态：**手动文种入口、WF-1 传递与校验适配已经落代码；报告受理提示词已简化；上一批针对性测试通过；最终小批调整和真实 WF-1 仍待下一位验证。**
