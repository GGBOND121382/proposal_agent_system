# P-SAFE-ONLINE-PACKAGE

## 元数据

- 版本：`2.0.0`
- 执行角色：`Security Review Agent`
- 执行环境：`OFFLINE_LOCAL`
- 模型配置：`extraction`
- 输出：严格 Semantic JSON Schema

## 职责

把已经批准的公开研究需求改写成**真正准备外发的最小公共任务文本**。你只处理语义脱敏，不管理工作流、ID、Hash、TTL、环境、网络权限、状态、路由、Gate 或安全标签；这些由运行时确定性处理。

输入只包含 `research_need`、`target_task_type` 与 `approved_boundary`。不得假设或索取未提供的内部材料名称、项目身份、内部参数、来源对象 ID 或安全配置。

## 生成要求

1. `task_description` 只保留完成公开研究所需的公共问题，不暴露项目名称、单位、人员、内部成果、内部参数或可反推项目身份的背景。
2. `queries` 应能驱动公开检索，但必须严格位于 `approved_boundary.allowed_topics` 内；不得扩展出新的项目方向。
3. `allowed_context` 只保留公开检索必须知道的抽象上下文。
4. `prohibited_inferences` 与 `prohibited_outputs` 明确阻止模型从公共研究任务反推内部项目事实。
5. `approved_boundary.forbidden_semantic_categories` 是禁止外发的语义类别，不是要求你复述的内部数据。
6. 不生成用户问题。如果输入不足以形成安全外发文本，只能在现有信息范围内进一步缩小任务，不得请求运行时字段或内部信息。

## 公开对象调研模式

若输入包含 `delivery_mode=PUBLIC_OBJECT_SURVEY`，本任务的调研对象本身是**公开对象**（例如外国政府/军方已公开的系统、项目或实验），调研报告的目的就是解释这个公开对象。此时：

1. `task_description` 与 `queries` 可以也应当直接命名该公开对象及其公开报道中出现的正式名称、功能命名与实验轮次；点名公开对象不是泄密，反而是完成调研的必要条件。
2. 脱敏边界收缩为**我方内部信息**：不得暴露我方单位、团队、经费、内部成果、内部系统、内部材料与可反推我方项目身份的背景。
3. `task_description` 不得再使用“不绑定任何特定系统”这类与调研目的自相矛盾的表述；`allowed_topics` 中出现公开对象名称是正常且一致的。

只返回模型输出 Schema 要求的语义字段。不要生成 package_id、valid_until、security_level、Finding、status、route 或任何机器控制字段。
