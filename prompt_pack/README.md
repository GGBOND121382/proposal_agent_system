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

## 2.2.0 论证质量协议

- Prompt注册项由26个扩展为30个；
- 新增论证架构与表达编辑Producer/Critic；
- 新增论证图、章节合同、提案契约、前文章节摘要和质量Scorecard Schema；
- 30个Prompt均保留角色、权限、必读输入、状态、Finding、自检和严格输出协议；
- Replay清单扩展为150组：120组合法输入/输出和30组故意错误输入；
- 运行时质量校验与Prompt协议共同约束文种、中心命题、论证链、方法、创新、基础、指标、重复和篇幅。

## 2.1.0 运行时扩展

本扩展增加公开研究查询内容传递、公开来源约束和 Mermaid Skill 规则；Prompt 数量仍为26个，新增共享 Skill 规则并更新输入 Schema。
