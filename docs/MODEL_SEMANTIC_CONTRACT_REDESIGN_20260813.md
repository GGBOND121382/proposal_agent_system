# 模型语义契约重构设计

## 核心原则

LLM 只负责无法由现有事实和确定规则唯一推导的语义判断。运行时、数据库和确定性代码负责：Prompt/Schema/Workflow 身份，Hash、版本、Span、来源绑定与审计回执，机器 ID，能从业务语义唯一派生的 Graph/Matrix，能从 blocker/route/Gate 唯一派生的状态，changed_paths/protected hash/resolved receipts，以及无歧义的协议表示归一化。

同一业务语义只允许模型表达一次。

## P-ARGUMENT-ARCHITECTURE

LLM 只生成中心命题、研究线程（Gap→RQ→Objective→Work Package→Method→Evaluation→Innovation）、最近工作比较、研究基础、证伪/比较规则、证据缺口和必须由用户补充的问题。

模型只读参考：项目任务、硬约束、Evidence Cards、已有论证语义 seed、revision issues、human resolutions。current_sections、template_context、Hash、版本、机器 ID、完整 source_ref、Graph/Matrix 机器表示不进入模型输入。

Runtime 派生 node/edge/graph ID、Graph、Matrix、source binding、status/readiness、scope decision、canonical container。

## Critic

LLM 只审查 CENTRAL_THESIS、ARGUMENT_CHAIN、EVIDENCE_SUPPORT、METHOD_SUBSTANCE、INNOVATION_BASELINE、FEASIBILITY_FOUNDATION、METRIC_JUSTIFICATION 七类语义质量。Finding ID、JSON Pointer、route、repairable、blocking、verdict、结构检查回执由 Runtime 生成。

## Targeted Repair

先分为：DETERMINISTIC（规则唯一确定，不调用 LLM）、LOCAL_SEMANTIC（局部语义判断，调用 Targeted Repair）、STRUCTURAL_SEMANTIC（需新增/删除实体、重构线程或改变范围，回 Original Producer）。

Repair 模型输入严格拆成 repair_targets（WRITE）和 reference_context（READ ONLY），重试时只增加 previous_attempt_feedback。模型只输出最小 changes；Runtime 在副本上机械应用、计算 diff/receipts，然后执行原完整 Validator 链。

禁止把整个 canonical object 发给局部 Repair；禁止模型重抄 repaired_object；禁止模型同时维护 Graph/Matrix 两套 ID；禁止通过增加 Prompt 规则解决 Runtime 能消除的确定性矛盾；禁止放松 canonical Validator。
