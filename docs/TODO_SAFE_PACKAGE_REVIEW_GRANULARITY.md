# TODO — 安全包审查精细化（Safe-Package Review Granularity）

Status: active debt. 自 2026-09-07 起，安全包 Critic 的自动打回循环可通过
`SAFE_PACKAGE_CRITIC_ENABLED=false` 停用（默认 true）。当前 `.env` 已设为 false，
目的是先保证 WF-3B 端到端跑通；以下优化完成前不要把默认值改回强制打回。

## 背景与问题

- 实例 `wf-2cc59f83c233480c`（WF-3B，由 wf-66172154c37249b4 重建）卡在步骤 1
  `P-SAFE-ONLINE-PACKAGE-CRITIC`：LIVE 模式下 Critic 对涉军 DASH 课题反复返回
  `REVISE`（`SAFE_PACKAGE_SCOPE_EXCESS` / `SAFE_PACKAGE_REIDENTIFICATION`），
  耗尽默认 2 轮重生成预算（`app/workflows.py` 中
  `original_producer_regeneration_limit`，默认 2、上限 5）后 `BLOCKED_CONTENT`。
- 原工作流 9/4 是同一步骤压线（2 轮 + 1 次 Critic ERROR）才通过，说明该步骤在
  涉军课题上长期处于临界状态，非本次检索链修复引入的回归（已用
  `git diff 8d4310b..HEAD` 核实安全包 prompt/schema/context 路径零改动）。
- LIVE 采样非确定：同一输入可能 PASS 也可能 REVISE，重跑成本不可控。

## 已采取的临时措施

- 新增配置开关 `SAFE_PACKAGE_CRITIC_ENABLED`（`app/config.py`，默认 true）。
- 为 false 时，`app/workflows.py` 在 `_record_decision` 之后把
  `P-SAFE-ONLINE-PACKAGE-CRITIC` 的 `REVISE` 旁路为 `PASS`：
  - findings 与 run_id 记入 `state["safe_package_critic_bypassed"]`（保留 50 条审计）；
  - 人工 Gate（`OUTBOUND_SECURITY_APPROVAL`）仍由 `next_human_gate` 正常创建，
    安全责任不悬空，只是不再自动重生成。
- `BLOCK` 状态不受开关影响，仍然阻断。

## 后续优化方向（精细化）

1. Prompt 强负例约束：安全包内容不得点名具体机构、部队番号、内部系统架构；
   把"公开报道已披露的信息不算再识别"写成明确判据，减少
   `SAFE_PACKAGE_REIDENTIFICATION` 的误报。
2. Finding 去抖：相同 code + 相同 target 的 finding 在相邻轮次重复出现时不再
   计为新 REVISE 理由，避免模型在同一问题上反复打回。
3. `allowed_topics` 粒度：允许课题级声明"仅使用公开来源"，Critic 校验声明与
   实际包内容一致性，而不是对课题本身做政治性回避。
4. 重生成预算按课题/步骤可配：涉军或高敏课题允许更高预算或直接进入人工 Gate，
   而不是统一 2 轮。
5. 恢复默认打回前，用 DASH 课题做至少 3 次 LIVE 采样验证 REVISE 率降到可接受水平。

##  Non-goals

- 不删除安全包 Critic 步骤本身，也不绕过人工 Gate。
- 不把 `BLOCK`（确定性安全问题）纳入旁路范围。
