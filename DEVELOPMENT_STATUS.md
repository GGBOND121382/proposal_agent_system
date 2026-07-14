# 开发状态与边界

## v0.5.0 已完成

- 26个Prompt、五条工作流、十三类人工Gate和Producer/Critic分离保持不变；
- 新增可审计 Skill 运行时，`mermaid.render` 与 `public_research.archive` 均保存输入/输出Hash、工件、状态、耗时和错误；
- 弱模型不承担图片二进制生成，只输出可编辑 Mermaid；代码完成安全检查、Playwright/Chromium渲染、缓存、Worker轮换、超时重启和DOCX插图；
- Public Research Plan Agent 的原始查询直接驱动检索，缺少任一查询响应即阻断；原始连接器响应、逐来源快照、文本、元数据和SHA-256全部归档；
- 支持 `searxng`、`connector` 和明确标识为回放的 `recorded` 三种公开研究模式；
- Ubuntu/Windows源码依赖离线包和Docker离线镜像包均提供构建、校验、安装/加载、启动、备份、恢复和卸载脚本；
- 大模型地址、密钥和真实模型名仍通过 `.env`、端点配置、模型映射和Prompt模型Profile四层配置；
- 完成“面向复杂物流场景的多智能体运输方案优化关键技术研究”54章复杂端到端验收。

## 已验证

- Python静态编译、Shell语法和离线包manifest往返校验：PASS；
- 本地自动测试：19项全部通过；
- 26/26 Prompt实际触发，239次Prompt Run对应239份完整Trace；
- `P-TARGETED-REPAIR` 实际经历Critic发现问题、定向修复和再次审查；
- Research Agent生成10个查询，批准连接器返回40条结果，系统去重并归档39个来源；
- 39个原始快照Hash、文本Hash及查询覆盖重新计算：PASS；
- 28次Mermaid Skill执行均保存MMD/SVG/PNG/元数据，连续20图Worker稳定性测试：PASS；
- 最终申请书182页、54章、40条参考文献、33幅嵌入图片，无实质性长段落精确重复；
- 182页逐页检查及PDF坐标检测：无空白页、坏字、越界文字或越界图片。

## 明确边界

- 本次公开资料检索与归档为真实连接器结果，模型语义调用仍采用 `SIMULATED`，因为没有用户真实LLM API密钥；不能把该结果表述为真实模型质量测评；
- 当前执行环境没有Docker守护进程和PowerShell，Docker/Windows脚本完成静态、语法和manifest级验证，但未在对应目标系统实际安装；
- `connector` 模式归档的是批准搜索连接器返回的原始记录；需要完整网页/PDF字节快照时，应使用可出网的 `searxng` 模式或让连接器返回文件内容；
- 严格物理隔离环境仍应增加组织级签名、病毒扫描、摆渡介质登记和来源包审批；
- 生产上线仍需身份权限、正式密级映射、受控数据库、密钥管理、DLP、集中审计和专用OOXML完整性验证。
