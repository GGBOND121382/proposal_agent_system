# G2 S1：单章节完整链实现与验收

> 对应计划：`docs/CONCURRENT_DEVELOPMENT_PLAN.md` 的 G2 第 1 项。  
> 实现分支：`agent/integration-v06-single-section`。  
> 验收原则：编排回归可以使用确定性测试响应，但不得用脚本改写正文；正式模型语义能力仍属于 G3 LIVE 验收。

## 1. 实际生产链

`WF-4_PROPOSAL_AUTHORING` 在 `single_section_complete_chain=true` 时必须精确匹配一个章节，并按下列顺序运行：

1. `P-WRITE-BLUEPRINT`；
2. `P-WRITE-BLUEPRINT-CRITIC`；
3. Critic 返回 `REVISE` 且 Finding 可局部修复时，执行一次 `P-TARGETED-REPAIR`；
4. 使用修复对象重新执行独立的 `P-WRITE-BLUEPRINT-CRITIC`；
5. `P-WRITE-CONTENT`；
6. `P-WRITE-CRITIC`；
7. Critic 返回 `REVISE` 且 Finding 可局部修复时，执行一次 `P-TARGETED-REPAIR`；
8. 使用修复对象重新执行独立的 `P-WRITE-CRITIC`；
9. `P-EXPRESSION-POLISH`；
10. `P-EXPRESSION-CRITIC`；
11. 候选审阅、安全审批与导出审批；
12. 仅导出被后续 Expression Critic `PASS` 的润色候选。

Expression Critic 不允许通过二次自动改写或人工空确认绕过。它返回非 `PASS` 时，章节链阻断。

## 2. 修复与章节隔离

- Blueprint Critic 和 Content Critic 的自动修复额度分别为每章节最多一次；
- 修复计数使用 `section:<section_id>:<critic_prompt_id>`，不会被其他章节消耗；
- 修复对象使用 `section:<section_id>:<producer_prompt_id>` 保存，不会串入下一章节；
- 下游 Content、Polish 和复审 Prompt 读取修复对象，而不是继续读取原失败版本；
- `section_progress` 在每次 Prompt、Repair 和复审后持久化，重启后从同一阶段继续；
- Track A 的确定性调用键保证数据库事务已提交但阶段状态尚未推进时不会重复消费模型响应。

## 3. 质量与导出硬门

- Critic Finding 先进入 `QUALITY_FINDING` 生命周期；
- `P-TARGETED-REPAIR` 只登记修复证据，不能自行关闭 P0/P1；
- 后续独立 Critic 运行未再观察到相同 Finding，且修复 Run 与复审 Run 不同，才标记为 `VERIFIED`；
- 开放 P0/P1 会阻断工作流完成和导出；
- 已批准 Gate 不能覆盖开放质量 Finding；
- Exporter 只选择同一工作流中、时间上晚于润色 Run 的 Expression Critic `PASS` 结果。

## 4. 可执行验收

```bash
python scripts/run_single_section_chain_acceptance.py \
  --evidence-dir recovery_evidence/s1/local
```

该命令执行：

- 模块编译；
- Prompt Pack 一致性校验；
- 单章节顺序、两类定向修复、一次修复上限、Expression 阻断和章节隔离测试；
- 运行时恢复、质量生命周期与导出硬门测试；
- 一个真实 SQLite/Artifact/Gate/DOCX 的单章节编排端到端测试；
- JUnit、日志、JSON/Markdown 报告和 SHA-256 证据生成。

## 5. 边界

本项证明 S1 的生产编排、修复路由、状态恢复、质量门和 DOCX 导出链已经闭合。SIMULATED 测试只证明编排和确定性不变量，不代表模型生成内容质量；完整材料、真实模型、真实 Research Skill 和 PDF 页面视觉质量在 G3 验收。
