# 项目申请书智能系统 Prompt 开发交接包 V2

本包将本次对话形成的业务、安全和模型调用设计落成可校验文件。

## 已完成

- 26个顶层Prompt，均为2.0.0详细执行版；
- 26个严格输入Schema与26个严格输出Schema；
- 6类核心输入包Schema；
- 25类项目定义对象及26类允许关系；
- 8个章节Profile；
- 离线/在线模型端点、模型、Prompt Profile和默认拒绝路由配置；
- 130组实际Replay文件；
- 构建校验脚本和报告。

## 仍需部署方填写

- OFFLINE_LLM_BASE_URL、OFFLINE_LLM_API_KEY、OFFLINE_GENERAL_MODEL、OFFLINE_CRITIC_MODEL；
- 需要在线能力时填写ONLINE_LLM_*并完成外发政策审批；
- 将PUBLIC/INTERNAL/SENSITIVE/CLASSIFIED映射为单位正式管理等级；
- 将抽象审批角色映射为真实人员与权限。

## 重要边界

V2证明文件、Schema和Replay在静态层面一致；不等于真实模型质量、真实保密审批或生产部署已经通过。真实模型上线前必须执行Prompt回归和安全红队测试。
## 2.1.0 运行时扩展

本扩展增加公开研究查询内容传递、公开来源约束和 Mermaid Skill 规则；Prompt 数量仍为26个，新增共享 Skill 规则并更新输入 Schema。

