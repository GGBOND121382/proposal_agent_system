# 轨道 E：质量门、全文审查与责任路由验收说明

## 1. 实现范围

本实现完成 `docs/CONCURRENT_DEVELOPMENT_PLAN.md` 中 E1–E6，并遵守 G0 冻结约束：不修改 Prompt、Schema、工作流定义、数据库表结构或安全流程。质量 Finding 使用现有 `artifacts` 表以 `QUALITY_FINDING` 类型追加保存，每次状态变化生成新版本，不覆盖历史记录。

| 工作包 | 实现 | 硬性验收行为 |
|---|---|---|
| E1 确定性质量门 | `ProposalQualityGuard` 增加关系端点/类型、原子事实、来源覆盖、指标测量依据等规则 | 规则失败生成 P1 Finding，并把结果改为 `REVISE` |
| E2 章节质量门 | 保留并强化 Profile 专用职责检查 | 创新、目标、内容、路线、指标、基础等章节不能用统一模板职责替代 |
| E3 跨章一致性 | 检查术语、数字、映射与完整论证链 | 目标—任务—方法—验证—成果—指标冲突或缺失时阻断 |
| E4 Integration Critic | 对完整候选集合检查重复、冲突、遗漏、结论闭环、创新证据和质量维度 | 缺章、虚构映射、重复模板、论证链缺口均形成可路由 Finding |
| E5 责任路由 | `QualityLifecycleManager.route_delivery_finding` | 正文问题返回 WF-4；DOCX/PDF/渲染/裁切/重叠问题归 `EXPORT_ENGINEERING`，禁止改正文掩盖工程错误 |
| E6 P0/P1 关闭机制 | 追加式 Finding 生命周期和运行/导出硬门 | 只有“修复运行证据 + 不同运行的指定独立 Critic 复审”才能进入 `VERIFIED`；Gate 批准不能绕过 |

## 2. Finding 生命周期

状态流转为：

```text
OPEN -> REPAIR_RECORDED -> VERIFIED
```

`VERIFIED` 必须满足：

1. Finding 已绑定责任 Agent/工程责任方、修复阶段和独立复审 Prompt；
2. 已记录修复运行 ID 与修复证据哈希；
3. 复审运行 ID 与修复运行 ID 不同；
4. 复审者必须等于 Finding 指定的独立 Critic；
5. 复审结果中原问题代码已消失。

系统没有提供人工“直接关闭”接口。工作流结束和 DOCX 导出均调用项目质量硬门；存在未关闭 P0/P1 时进入 `BLOCKED` 或抛出 `ExportDenied`。

## 3. API 与集成点

- `GET /api/projects/{project_id}/quality-findings`：查询最新 Finding 状态；
- `GET /api/projects/{project_id}/quality-matrix`：按状态、严重级别和责任方汇总；
- `POST /api/projects/{project_id}/quality/delivery-findings`：接收 D 轨道导出后结构/视觉 Findings，并执行责任路由。

## 4. 可执行验收

```bash
python -m compileall app scripts
python scripts/quality_track_e_acceptance.py
python -m pytest -q
```

专项验收会生成：

- `recovery_evidence/track_e/acceptance.json`
- `recovery_evidence/track_e/acceptance.md`
- `recovery_evidence/track_e/pytest.log`
- `recovery_evidence/track_e/junit.xml`

这些测试证明确定性规则、路由和生命周期约束，不把 `REPLAY`、`MOCK` 或 `SIMULATED` 的正文当作真实模型语义能力证明。
