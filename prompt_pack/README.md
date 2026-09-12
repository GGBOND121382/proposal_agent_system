# 项目申请书智能系统 Prompt 开发交接包 V2

本包将本次对话形成的业务、安全和模型调用设计落成可校验文件。

## 已完成

- 30个顶层Prompt，均具备完整角色、权限、输入、状态、Finding、自检与输出协议；
- 30个严格输入Schema与30个严格输出Schema；
- 6类核心输入包Schema，以及论证图、章节合同、提案契约、前文章节摘要和质量Scorecard等公共Schema；
- 项目事实图谱包含25类项目定义对象及26类允许关系，并与申请书论证图谱分离；
- 8个章节Profile；
- 离线/在线模型端点、模型、Prompt Profile和默认拒绝路由配置；
- 150组Replay文件，其中120组为合法输入/输出，30组为故意错误输入；
- 构建校验脚本和报告。

## 仍需部署方填写

- OFFLINE_LLM_BASE_URL、OFFLINE_LLM_API_KEY、OFFLINE_GENERAL_MODEL、OFFLINE_CRITIC_MODEL；
- 需要在线能力时填写ONLINE_LLM_*并完成外发政策审批；
- 将PUBLIC/INTERNAL/SENSITIVE/CLASSIFIED映射为单位正式管理等级；
- 将抽象审批角色映射为真实人员与权限。

## 重要边界

V2证明文件、Schema和Replay在静态层面一致；不等于真实模型质量、真实保密审批或生产部署已经通过。真实模型上线前必须执行Prompt回归和安全红队测试。

## 2.3.0 语义模型边界

- `P-ARGUMENT-ARCHITECTURE`、其 Critic 和 `P-TARGETED-REPAIR` 使用独立 Model Schema；
- LLM 只生成不能由规则唯一推出的科研语义；ID、Graph/Matrix、状态、Hash、引用回执由 Runtime 派生；
- Producer 只接收项目任务、约束、Evidence Cards、设计语义 seed、revision issues 和 human resolutions；
- Critic 只判断七类论证质量维度与语义问题，结构检查回执由 Runtime 生成；
- Targeted Repair 明确拆分 WRITE 目标与 READ 上下文，只返回最小修改操作；
- 确定性协议错误优先由 Runtime 在副本上按唯一规则修复并重新走完整 Validator；
- 需要新增/删除业务实体或重构研究线程时，不进入 Targeted Repair，回 Original Producer；
- MiniMax 语义任务直接使用 function arguments 承载语义对象，不再嵌套 `output_json` 字符串。

## 2.2.1 Provider模型Token预算

- `.env`只选择模型，不重复维护token上限；
- Provider模型上下文与输出能力由`config/models.yaml/provider_capabilities`统一登记；
- Prompt Profile改用`desired_output_tokens`表达任务期望预算；
- Runtime按模型硬上限、实际输入估算和上下文安全余量计算本次有效输出上限；
- MiniMax OpenAI-compatible请求使用`max_completion_tokens`；
- 模型或模型能力变化进入Provider Request Spec Hash，避免复用旧模型生成结果。

## 2.2.0 论证质量协议

- Prompt注册项由26个扩展为30个；
- 新增论证架构与表达编辑Producer/Critic；
- 新增论证图、章节合同、提案契约、前文章节摘要和质量Scorecard Schema；
- 30个Prompt均保留角色、权限、必读输入、状态、Finding、自检和严格输出协议；
- Replay清单扩展为150组：120组合法输入/输出和30组故意错误输入；
- 运行时质量校验与Prompt协议共同约束文种、中心命题、论证链、方法、创新、基础、指标、重复和篇幅。

## 2.1.0 运行时扩展

本扩展增加公开研究查询内容传递、公开来源约束和 Mermaid Skill 规则；Prompt 数量仍为26个，新增共享 Skill 规则并更新输入 Schema。
