# Phase 4 实施报告：WF-3B_TOPIC_BACKGROUND_RESEARCH（2026-09-03）

对应计划：`proposal_agent_search_and_background_workflow_update_plan_20260901.md` §5、§7、§8 Phase 4、§11.3。本批只做 WF-3B 本体（契约、运行时、持久化、UI 入口、测试），不接入 WF-4（Phase 5），不做全量回归与 LIVE 验收（Phase 6）。

## 1. 交付内容

正式新增独立工作流 `WF-3B_TOPIC_BACKGROUND_RESEARCH`（topic 应用背景调研），步骤序列：

1. `P-SAFE-ONLINE-PACKAGE`（复用）→ 2. `P-SAFE-ONLINE-PACKAGE-CRITIC`（复用，OUTBOUND_SECURITY_APPROVAL Gate 自动生效）→ 3. `P-BACKGROUND-RESEARCH-PLAN`（新增，v1.0.0）→ 4. `P-BACKGROUND-RESEARCH-PLAN-CRITIC`（新增）→ 5. `PUBLIC_SEARCH`（`application_background` 质量 profile，强制 `require_web_discovery=true`）→ 6. `P-BACKGROUND-RESEARCH-SYNTHESIS`（新增）→ 7. `P-BACKGROUND-RESEARCH-CRITIC`（新增）→ 8. `P-ONLINE-RESULT-IMPORT-CRITIC`（复用，ONLINE_RESULT_IMPORT_APPROVAL Gate 自动生效）→ 持久化 artifact `TOPIC_BACKGROUND_RESULT`。

语义边界：模型只产出语义查询与标准 PUBLIC_CLAIM（标注 dimension/target_section_profiles/conflicts/limitations）；card_id、维度覆盖、background_gaps、检索合同、状态与路由全部由运行时生成或校验。

## 2. 关键设计决策

1. **claims 模式对齐**：模型综合输出为 `claims[]`（标准 PUBLIC_CLAIM + 4 个背景字段），证据卡由 `app/background_research.py:build_background_cards()` 从通过 claim—来源绑定校验的 claim 确定性构建（card_id = sha256(topic_id+claim_id+dimension) 前 16 位）。未覆盖的冻结维度产出显式 `background_gaps`，绝不用模型记忆补齐。
2. **背景维度冻结**：8 维（§5.3）。options 显式给定则校验为子集并按规范序冻结；缺省全部 8 维。topic 解析顺序：options → WF-1 project_title → problem_statement → UNRESOLVED（复用 WAITING_PREREQUISITE 阻塞语义，WF-1 完成后 advance 自动恢复）。
3. **强制网页证据**：`application_background` profile 下 `web_evidence` 维度要求 ≥1 条经 WEB_SEARCH channel 的可用证据，纯 Academic 集合恒 INSUFFICIENT（DEGRADED + gap，对应计划 §9"不能宣布背景充分"）；channel 级失败由 Phase 3 的 `require_web_discovery` 合同直接阻断（BLOCKED_PROVIDER）。
4. **完成语义**：`COMPLETED` / `COMPLETED_WITH_BACKGROUND_GAPS`；artifact 版本号沿用同类型 MAX+1；审计事件 `WF3B_BACKGROUND_RESULT_PERSISTED`。
5. **SimulatedLLM 新增 4 个 handler**（`app/simulated_llm.py`）：plan 按冻结维度生成查询并镜像检索合同；synthesis 从输入信封的真实归档来源构建 claims（修复了 replay 占位 source_id 导致的输出引用完整性失败）。
6. **critic 输入接线**：`P-BACKGROUND-RESEARCH-CRITIC` 的 `synthesis_candidate` 由运行时调用同一 `build_background_cards` 构建卡包，critic 看到的 card_id 与最终持久化 artifact 完全一致。
7. **共享 schema 最小扩展**：`P-SAFE-ONLINE-PACKAGE(-CRITIC)`、`P-ONLINE-RESULT-IMPORT-CRITIC` 三个共享输入 schema 的 `workflow_type` enum 追加 `TOPIC_BACKGROUND_RESEARCH`；import critic 的 `result_package.claims` 投影剥离 4 个背景专属字段（对 WF-3 为零影响，被剥离字段完整保留在 artifact 中）。
8. **治理清单同步**：`governance/f/test_evidence_manifest.json`（31→35 prompts、155→175 replays）、`governance/g0/interface_contract.json`（entry_count、entries_sha256、registry_git_blob_sha、workflow_state_machine 加入 WF-3B、CRITIC_PRODUCER 映射）、`governance/g0/security_freeze.json`（3 个共享 schema 的 approved_changes 条目，approval_reference 指向计划文档）。注意：G0 的 entries_sha256 在 HEAD 上已因 Phase 3 的版本号变更漂移，本次一并修正为当前真实值。

## 3. 修改文件清单

**新增（24）**：`app/background_research.py`；`tests/test_wf3b_background_research.py`；`prompt_pack/prompts/background_research/`（4 个 md）；`prompt_pack/schemas/prompts/background_research_*`（8 个）；`prompt_pack/schemas/model/background_research_*`（8 个）；`prompt_pack/schemas/common/background_evidence_card.schema.json`；`prompt_pack/replay/cases/background_research_{plan,plan_critic,synthesis,critic}/`（20 个 fixture）。

**修改（33）**：
- 运行时：`app/workflow_defs.py`（WORKFLOWS+CRITIC_PRODUCER）、`app/workflows.py`（前置、options 归一化、PUBLIC_SEARCH 分支、claim 校验、持久化）、`app/workflow_repair.py`（`_run_background_search`）、`app/workflow_lifecycle.py`（文案）、`app/dependency_preflight.py`（WF-3B 分支）、`app/context_base.py`（WF-3B payload 供给、critic 卡包、import 投影剥离）、`app/runtime_factory.py`、`app/wf3_contracts.py`、`app/simulated_llm.py`、`app/skills/research_quality.py`（application_background profile + web_evidence 维度）、`app/skills/research_audit.py`、`app/skills/verifiable_public_research.py`。
- 契约/治理：`prompt_pack/config/prompt_registry.json`（+4 条目）、`prompt_pack/replay/manifest.json`（+20）、`prompt_pack/schemas/common/prompt_{input,output}_envelope.schema.json`（oneOf +4）、`prompt_pack/schemas/prompts/{safe_online_package,safe_online_package_critic,online_result_import_critic}_input.schema.json`（enum +1）、`prompt_pack/tools/validate_pack.py`（环境不变量扩展）、`prompt_pack/{SHA256SUMS.txt,MANIFEST.md,BUILD_REPORT.json}`（生成物）、`governance/f/test_evidence_manifest.json`、`governance/g0/{interface_contract.json,security_freeze.json}`。
- UI/文档：`app/static/index.html`（select 选项 + `#wf3bOptions` 表单）、`app/static/app.js`（显隐 + options.background_research 组装）、`README.md`、`DEVELOPMENT_STATUS.md`（五条→六条，其中 README L113/L122 历史脚本描述按史实保留加注）。
- 测试机械计数：`tests/test_f_agent_matrix.py`、`tests/test_prompt_contract_semantics.py`、`tests/test_runtime.py`、`tests/test_v04_complex_runtime.py`（31→35 / 155→175 等）。

## 4. 验证证据

- WF-3B 专项 + 契约 + 治理：`py -m pytest tests/test_prompt_contract_semantics.py tests/test_prompt_version_consistency.py tests/test_f_agent_matrix.py tests/test_wf3b_background_research.py -q` → **72 passed**（含 WF-3B 21 项：步骤序列、options 归一化、计划维度过滤、执行合同强制、web_evidence 充分性、证据卡确定性、gap 生成、持久化两种完成语义、版本 MAX+1、幂等、lifecycle 级联、preflight、SIMULATED 端到端落 TOPIC_BACKGROUND_RESULT）。
- 范围回归（20 个文件）：**350 passed, 2 failed**。2 个失败为 `tests/test_workflow_input_integrity.py` 的 LIVE 输入校验用例，已在干净 HEAD（`aea3a6b`）的独立 worktree 复现，属既有基线问题，与 Phase 4 无关。
- `py prompt_pack/tools/validate_pack.py` → PASS（registry 35、replay 175、errors []）；`scripts/refresh_prompt_pack_manifest.py` 后复跑仍 PASS。
- `py -m compileall app tests` 通过；`node --check app/static/app.js` 通过；`git diff --check` 仅既有 CRLF/LF 提示。
- Ruff：本批全部改动文件与 HEAD 逐文件持平，无新增告警（存量风格债：workflow_repair.py 43、simulated_llm.py 19、workflows.py/context_base.py 各 1，均为 HEAD 既有）。
- 未运行任何 LIVE LLM；全部为 REPLAY/SIMULATED 与确定性单测。

## 5. 边界声明

- **未改数据库 Schema**：复用 `workflows`/`workflow_lineage`/`prompt_runs`/`artifacts`，仅新增 artifact type `TOPIC_BACKGROUND_RESULT`。
- **改了 Prompt JSON Schema**：新增 4 个 prompt 的全套 schema + common 证据卡 schema；3 个共享输入 schema 的 workflow_type enum 扩展（已在 governance 登记）。
- **未接入 WF-4**：`_required_workflow_types` 的 WF-4 分支、`_scoped_facts()` 路由、`app/background_context.py` 均未动（Phase 5）。
- **未运行 LIVE LLM**。已知 LIVE 链路遗留：`P-BACKGROUND-RESEARCH-SYNTHESIS` 尚未纳入 `WF3_MODEL_PROMPTS` canonicalization（Phase 6 LIVE 验收前需补）。

## 6. 下一批准确范围（Phase 5）

1. WF-4 前置关系：`require_background_research=true` 时 WF-3B 强制前置；存在已获批 WF-3B 时可选冻结进 WF-4 lineage；`_validate_explicit_prerequisite_workflows` 与 `_resolve_prerequisite_workflows` 放行 WF-3B。
2. `app/background_context.py`：`background_context` 按 Section Profile 的确定性投影表（计划 §6.3）。
3. 证据进入写作链：P-ARGUMENT-ARCHITECTURE(-CRITIC) → P-REVISION-PLAN → P-WRITE-BLUEPRINT → P-WRITE-CONTENT 依次接入背景 claim/card ID。
4. 修复 `_scoped_facts()`：按冻结 Section Contract、背景卡 target profiles、research question 绑定和相关性排序，替换 `public[:6]`。
5. lifecycle rebuild 把 WF-3B 纳入 WF-4 下游图：重建背景研究后自动使对应 WF-4 分支失效并重建。
