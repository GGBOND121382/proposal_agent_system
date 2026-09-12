# WF-1 手动文种与调研报告链路验收记录（2026-09-10）

> 接续 `WF1_MANUAL_DOCUMENT_TYPE_HANDOFF_20260910.md`。本文档记录手动指定
> `document_type=SURVEY_REPORT` 后 WF-1 受理链路的修复、验证与遗留事项。
> 全部代码改动**未提交**（用户要求暂不 commit）。

## 1. 目标与口径

- 用户手动指定交付文种（`projects.config.document_type`），程序不再猜测文种。
- DASH 调研报告项目（`project-d39c11d09c7445d3`）按 SURVEY_REPORT 受理：
  不要求科研申请书要素（资助机构、中心命题、创新点、团队基础等）。
- 真实引用/内容错误仍必须被拒绝，不允许伪造 PASS。
- 文种映射：canonical 端 SURVEY_REPORT → TECHNICAL_REPORT 兼容映射（不扩枚举）；
  质量门端 SURVEY_REPORT → RESEARCH_REPORT 检查口径。

## 2. 本轮修复清单

### 2.1 交接批次已实现（复测确认）

- `PATCH /api/projects/{id}/document-type` 接口；`context_base._apply_common_payload`
  按项目配置向声明了 `payload.document_type` 的 4 个 WF-1 语义节点注入文种
  （schema 声明是注入前提）。
- `_document_kind_hint` 手动文种优先（`app/proposal_quality.py`）。
- RESEARCH_REPORT 文种下 GRAPH_INCOMPLETE / GRAPH_TOO_SHALLOW 等降级 advisory。
- 预算耗尽文案断言更新（`tests/test_runtime.py`）。
- 50 项复测全过；prompt pack manifest 无 diff。

### 2.2 MASQUERADE 检查修复（`app/proposal_quality.py`）

- `QG_ENGINEERING_OBJECTIVE_MASQUADES_AS_RESEARCH` 原先把所有 OBJECTIVE 文本
  join 后跑正则，跨条目拼接可误匹配（"形成…流程图"+"原型迭代效率"拼出
  `形成.*原型`）。改为逐条 OBJECTIVE 单独匹配。
- 增加 `blocking=not research_report` 文种豁免。
- 测试：`tests/test_wf1_quality_gate_adaptation.py`
  （`test_engineering_objective_check_matches_within_single_objective`、
  `test_engineering_objective_check_is_advisory_for_research_report`）。

### 2.3 FACT 质量门 REQUIREMENT 豁免 + 可修复路由（本轮新增）

**真实失败证据**：`wf-72a5417014dc4cf5` step 6（P-FACT-EXTRACT，
run `run-b1367926bc83442f`，2026-09-10T12:53）输出 25 条事实，其中 24 条
`claim_type=REQUIREMENT`（任务要求/检索指令），1 条 FACT。确定性守卫误报
`QG_FACT_NOT_ATOMIC` ×12、`QG_FACT_NUMERIC_BINDING_MISSING` ×2：

- REQUIREMENT 条目合法地包含枚举（"待核查内容包括：A；B；C"）和范围数字
  （"DASH 1、2、3"、"60%—70%"、"2026年9月9日"、"C3BM"），原子性与数值绑定
  是对"可判真命题"的约束，不适用于指令性条目。
- 这两个码不在 `MODEL_REPAIRABLE_QUALITY_CODES` 中，
  `_prepare_semantic_producer_regeneration` 返回 NOT_APPLICABLE，
  直接落入 `_has_nonconfirmable_quality_failure` → BLOCKED_CONTENT，
  连一轮自动修复机会都没有。

**修复**：

1. `app/agent_prompt_kernel.py::_audit_facts`、`app/proposal_quality.py::_audit_fact_package`：
   `QG_FACT_NOT_ATOMIC` / `QG_FACT_NUMERIC_BINDING_MISSING` 仅对非
   `REQUIREMENT` 条目触发。PLAN 不豁免——既有测试（test_b3 系列）固定了
   PLAN 条目仍需原子性与数值绑定，防止"已完成事实"伪装成计划。
2. `app/quality_guard.py::MODEL_REPAIRABLE_QUALITY_CODES` 增加上述两码：
   真正违规（如 FC-005 一条 FACT 含两个分句）路由回 P-FACT-EXTRACT 做有界
   重生成（≤2 轮），而不是首轮即硬阻断。

**离线复核**：用真实归档输出重跑守卫，14 条 QG 误报收敛为仅剩 FC-005 的
NOT_ATOMIC ×2（kernel + proposal_quality 两处一致），status=REVISE，
可进入自动修复循环。

**测试**：`tests/test_track_b_agent_prompt.py` 新增
`test_b3_requirement_records_are_not_truth_apt_claims`（REQUIREMENT 枚举/范围数字不触发）、
`test_b3_multi_clause_fact_remains_non_atomic`（真实多分句 FACT 仍拒绝）。
相关 61 项测试全过；test_runtime 等 147 过 + 1 已知基线失败 + 2 项 v06 陈旧失败（见 §4）。

### 2.4 模型输出 "null" 字符串清洗前置（本轮新增）

**真实失败证据**：`wf-64f274d7eeb14d8a` step 2（2026-09-10 13:25）连续 3 次
`BLOCKED_CONTRACT`：MiniMax 按 SURVEY_REPORT 提示把不适用字段写成
`"application_year": "null"`（字符串，见
`data/model_calls/responses/call-provider-4d2241107511131db6530790-*.raw.txt`），
而语义模型输出校验在 `_normalize_output` 的
`normalize_exact_null_literals` 之前执行，字符串 "null" 直接判输
（`/application_year: 'null' is not valid under any of the given schemas`）。
契约修复循环亦无法处理（repair 模型 ESCALATE：这些字段对调研报告本就不适用）。

**修复**：`app/executor.py`、`app/runtime_executor.py` 在
`apply_semantic_model_output_defaults` 之后、`validate_model` 之前，
对紧凑模型输出按模型 schema 执行 `normalize_exact_null_literals`
（schema 导向、仅精确匹配 "null" 且目标位置允许 null），并把规范化路径记入
`warnings`（`SYSTEM_EXACT_NULL_LITERAL_NORMALIZATION(model-output)`）。

**测试**：`tests/test_wf1_semantic_boundary.py` 新增
`test_null_literal_strings_are_normalized_before_semantic_output_validation`，
直接用 P-SCHEME-EXTRACT 模型 schema 复现生产报错并验证清洗后通过。
相关 303 项 + runtime 99 项回归全过（仅余已知基线失败）。

### 2.5 PD-CRITIC 调研报告口径与 extract 设计对齐（本轮新增）

**真实失败证据**：`wf-29f9d6870ee3419b` step 5（2026-09-10 13:45）
BLOCKED_CONTENT：PD-CRITIC 连续 REVISE，2 轮生产器重生成预算耗尽。

三层冲突叠加：

1. **extract 与 critic 提示词口径相反**：extract 提示词规定 SURVEY_REPORT 下
   `argument_seed` 只留一个中心问题、`gap_keys=[]`、`relations=[]`（最小骨架）；
   critic 却按科研申请书标准要求"五条并列 RQ + 一条主问 + gap_keys 锚定 +
   合法关系矩阵"，并引用 gate 回答作为依据。
2. **gate 选项制造虚假二选一**：UQ-PDC-002 的两个选项都预设了科研式 RQ 结构，
   选定后变成 critic 的"人决"依据，与最小骨架设计直接冲突。
3. **兼容映射被当成缺陷**：critic 把 `document_type=TECHNICAL_REPORT`（兼容承接）
   判为 DOC_TYPE_MISMATCH 并要求改写为 SURVEY_REPORT——该值不在合同枚举中，
   照做即破坏结构校验，是"越修越坏"的陷阱指令。

**修复**：`prompt_pack/prompts/project_definition/project_definition_critic.md`
增补 SURVEY_REPORT 专门口径：兼容承接不算文种冲突、不得要求改写枚举外取值；
最小骨架（单中心问题、空 gap_keys、空 relations）合规；RESEARCH_QUESTIONS /
CLOSEST_PRIOR_WORK / METHOD_AND_EVALUATION / FOUNDATION_EVIDENCE 维度按
"不适用（最小骨架合规）"通过；不得就兼容映射和最小骨架再提 user_question。
manifest 已同步刷新，prompt 一致性 64 项测试全过。

### 2.6 非 SEMANTIC 生产器的质量码自修复通路（本轮新增）

**真实失败证据**：`wf-395ee1e0aeb747a5` step 6（2026-09-10 14:08）
P-FACT-EXTRACT PASS 后守卫打出 QG_FACT_NOT_ATOMIC/NUMERIC_BINDING_MISSING，
尽管两码已在 `MODEL_REPAIRABLE_QUALITY_CODES` 中，仍直接 BLOCKED_CONTENT。
原因：`_prepare_semantic_producer_regeneration` 要求
`model_contract_mode=SEMANTIC`，而 P-FACT-EXTRACT 是传统全量契约
（`model_contract_mode=None`），函数返回 NOT_APPLICABLE，落入
`_has_nonconfirmable_quality_failure` 硬阻断；且其输入 schema 未声明
`payload.revision_findings`，修复反馈无处可放。

**修复**：

1. `app/workflows.py::_prepare_semantic_producer_regeneration`：非 SEMANTIC
   生产器改为"输入契约声明了 `payload.revision_findings` 才允许自修复"，
   未声明的保持 NOT_APPLICABLE（防止反馈被静默丢弃、重试复现同一缺陷）。
2. `prompt_pack/schemas/prompts/fact_extract_input.schema.json`：
   payload 增加 `revision_findings`（ref `common/finding.schema.json`）。
3. `prompt_pack/prompts/fact/fact_extract.md`：必须读取输入补
   `revision_findings`（逐条修复后输出）；明确 REQUIREMENT 条目允许枚举、
   不强制原子拆分，与守卫口径一致。

**测试**：`tests/test_semantic_regeneration_routing_v1.py` 新增 3 项
（有契约的传统生产器可排程 / 无契约保持 NOT_APPLICABLE / 非可修复码不排程）。
回归 187 项全过（仅余已知基线失败）。

## 3. 事故记录：stash 实验打断真实运行（2026-09-10 13:10）

为确认 2 项 v06 失败是否为既有问题，在重建工作流 `wf-9db0bdb7267f4c23`
运行期间执行了 `git stash push/pop`：

- pop 因 CRLF 归一化与 uvicorn 占用日志文件失败，stash 滞留；
- 工作树在事故窗口内回退到 HEAD，`scheme_extract_input.schema.json` 的
  `payload.document_type` 声明消失 → 注入被跳过 → P-SCHEME-EXTRACT 模型看不到
  手动文种，按申报专项契约输出 `SCHEME_TYPE_MISMATCH` P0 → BLOCKED_CONTENT（step 2）。
- 恢复：`git checkout stash@{0} -- app prompt_pack tests docs` 全量恢复，
  与 stash 差异仅剩 1 行日志，随后 `git stash drop`。

**结论**：该次 SCHEME BLOCK 不是代码缺陷，是实验操作污染。**教训：真实工作流
运行期间禁止任何会改动工作树的 git 操作；基线对照一律用 `git archive` 到
`.runtime-data/` 副本。**

## 4. 遗留与治理债

| 项 | 状态 | 说明 |
|---|---|---|
| `validate_g0.py` INTERFACE_FREEZE | 未处理 | prompt 文件改动未登记治理，验收前需补齐或记录豁免 |
| `tests/test_v06_quality_redesign.py::test_quality_guard_failure_outputs_remain_schema_valid` | 陈旧测试 | v8 起 P-ARGUMENT-ARCHITECTURE 守卫改为 audit-only（`app/proposal_quality.py:398`），测试仍期待旧 REVISE 行为。已用 HEAD 版本三文件复现同样失败，与本轮改动无关 |
| `test_simulated_multisection_output_has_unique_claim_ownership_and_no_template_repetition` | 陈旧/待查 | QG_DOCUMENT_TEMPLATE_REPETITION 误触发，同样与本轮改动无关 |
| `test_runtime_recovers_safe_package_scalar_source_ref_drift_without_model_call` 等 | 已知基线失败 | 交接前已存在 |
| v7×9 / v9×1 critic envelope 失败 | 既有 | 不计入本轮回归 |
| `test_v8_prompt_pack_build_source_versions_match_registry_for_declared_prompts` | 既有 | build_v2.py 的 PROMPT_VERSIONS 与 prompt_registry.json 自 HEAD 起漂移（PD-EXTRACT 3.1.1/3.0.0、PD-CRITIC 3.1.0/3.0.0、P-PUBLIC-RESEARCH-PLAN 2.2.0/2.1.0），`git diff HEAD` 为空可证 |
| MiniMax 偶发 malformed JSON | 遗留 | 由重试吸收；本轮 12:52 一次 |
| 工程方案文种（ENGINEERING_PROPOSAL） | 未适配 | 仅 SURVEY_REPORT 链路做了门适配 |
| PD-CRITIC 对 TECHNICAL_REPORT↔SURVEY_REPORT 兼容映射报"冲突" | 观察项 | 模型对兼容映射的措辞困惑，gate 答"不修订"即可过；可考虑在 critic 提示中显式说明映射关系 |

## 5. 真实工作流验证记录

2026-09-11 重建验证（按 `WF1_REFERENCE_REPAIR_CHANGE_SPEC_20260911.md` 修复后）：

- 工作流 `wf-b943ade032da44f7`（project-d39c11d09c7445d3，DASH 调研报告），
  **WF-1 全流程 COMPLETED**（step 9）。
- prompt run 共 13 条：P-SECURITY-CLASSIFY / SCHEME-EXTRACT / PD-EXTRACT /
  FACT-EXTRACT / 各 Critic 全 PASS；2 次 ERROR 均一次重试即恢复
  （PD-CRITIC 的 `RESEARCH_GOALS` 枚举别名、FACT-EXTRACT 的 MiniMax 截断 JSON）。
- **P-TARGETED-REPAIR 0 次**。对照旧失败 `wf-11d5a0f0ef814cfc`：
  33 条 prompt run（20 条修复）、16 次 ERROR、约 90 次底层 HTTP 调用、
  最终 BLOCKED_CONTRACT；本次约 24 次底层 HTTP 调用（按 model_calls/requests
  文件时间窗统计，口径为近似值）。

## 6. 引用编号与局部修复（2026-09-11，WF1_REFERENCE_REPAIR_CHANGE_SPEC 交付）

### 6.1 根因（真实证据）

`run-204588b00ed34e38` 的 `output_json` 持久化的是**语义态候选**
（含 S1…S8/I 局部编号，未展开）。旧 `_validate_repaired_producer_output`
（`app/workflow_repair.py`）把它直接送进 canonical 校验链，`_normalize_output`
内的 `validate_reference_ids` 报出 **36 条** "ENTITY_REF reference ID 'S…' is not
present in its allowed input namespace" 误拒（已用持久化数据离线复现），错误进入
修复反馈循环，模型无法收敛而 ESCALATE。同次失败中修复模型曾把 `gap_keys`
新值写成字符串 `"[]"`，旧校验在 missing_field 分支跳过容器形状检查而漏检。

### 6.2 修改内容（最小范围）

| 位置 | 改动 |
|---|---|
| `app/model_semantic_contracts.py` | 新增 `apply_wf1_survey_intake_defaults`（P0-A）：仅 `P-PROJECT-DEFINITION-EXTRACT` + `payload.document_type=SURVEY_REPORT` 时，缺失的 `research_questions[*].gap_keys` 补 `[]`、`max_main_pages`/`max_core_research_questions` 补 `null`；不覆盖已有值、幂等、逐路径留痕（`WF1_SURVEY_INTAKE_DEFAULT`），在语义 schema 校验前应用 |
| 同上 | `_wf1_semantic_reference_errors` PD-EXTRACT 分支：`gap_keys` 除存在性外校验端点 `item_type ∈ {GAP, PROBLEM}`（规格用例 8） |
| 同上 | `targeted_repair_semantic_errors`：按 `original_object.object_type` 反查生产器模型输出 schema，沿 path 解析期望 JSON 类型，patch 值类型不符即拒（覆盖 missing_field 分支；`"[]"` 字符串不再能冒充数组）；schema 解析不到时放行，canonical 校验兜底 |
| `app/workflow_repair.py` | `_validate_repaired_producer_output`：语义态候选（无 `schema_version` 的语义契约生产器输出）先走原生产链——缺省 → null 清洗 → 调研缺省 → `validate_model` → `semantic_model_reference_errors`（绑定**原生产请求的** producer_input，S 编号目录一致）→ `expand_semantic_model_output`——再进原有 canonical 链；canonical 候选路径不变 |
| `app/runtime_executor.py` / `app/executor.py` | 生产链在 `validate_model` 前接入 `apply_wf1_survey_intake_defaults`，应用路径写入 output warnings（`SYSTEM_WF1_SURVEY_INTAKE_DEFAULT`） |

未做全局 `json.loads` 猜测式解码；未放宽任何 `protected_paths`。

### 6.3 验收

- 新增 `tests/test_wf1_reference_repair.py`（12 项，覆盖规格 §6 用例 1-13：
  缺省边界与幂等、RESEARCH_PROPOSAL 不适用、S99 拒绝、跨 run 编号绑定、
  I37 未声明/非 GAP 类型拒绝、缺证据目录报上下文错误、patch 原生类型、
  越权路径整体拒绝、修复后语义+canonical 两阶段校验）。
- 真实失败回放：`run-204588b00ed34e38` 持久化 input/output 离线路过重走，
  **不调用修复模型即通过**：5 处缺省由代码补齐，S1…S8 全部合法，
  canonical 校验 0 错误，装配出 9 个条目。
- 回归：相关套件 355 通过；既有基线失败不变（详见 §4，
  含 `test_runtime` safe-package 漂移、critic envelope 漂移、
  prompt 版本表漂移——均为 HEAD 既有，与本轮无关）。
- 真实 WF-1 重建：见 §5，COMPLETED。

### 6.4 留痕与审计

调研缺省应用时向 output warnings 写入
`SYSTEM_WF1_SURVEY_INTAKE_DEFAULT: <paths>`；原始模型响应仍在
prompt_runs/model_calls 中不可变保存。
