# 开发状态与边界

## G0 基线与接口冻结

- 唯一代码基线固定为 `ac7e3032a51c682c6bee6e2461d3393cc14835d7`，产品版本固定为 `0.6.0`；
- `pyproject.toml`、FastAPI 版本和 Docker Compose 镜像标签由自动 Gate 校验，已消除 `0.5.0-offline` 镜像标签漂移；
- 30 个 Prompt/Schema、Agent 责任、五条工作流、Gate、Critic/Producer 映射、SQLite 表结构和 Artifact/Trace 字段已形成机器可校验契约；
- 安全分类、密级集合、外发保护、模型路由、审批 Gate 与 WF-1/WF-3/WF-5 安全步骤已按代码基线冻结；
- Git、SQLite、Trace、材料与恢复包目录规范已固定；
- G0 恢复包包含源码归档、依赖声明、材料清单、SQLite 一致性快照、Trace JSONL、冻结契约和逐文件 SHA-256；
- `.github/workflows/g0.yml` 在全新 Python 3.12 虚拟环境中恢复依赖、源码、材料并重新执行基线测试；
- 详细说明见 `docs/G0_BASELINE_AND_INTERFACE_FREEZE.md`。

## v0.6.0 已完成

- 基于物流申请书239份Prompt Trace完成智能体责任链审计；
- Prompt由26个扩展为30个，新增论证架构与表达编辑两组Producer/Critic；
- 项目知识图谱与申请书论证图谱分离，新增Section Contract、Proposal Contract、Prior Section Digest和12维质量Scorecard；
- Producer与Critic输入契约统一，Context Builder不再以Replay样例静默替代真实生产结果；
- 章节写作携带唯一命题、证据ID、新增信息键和章节合同，跨章上下文使用结构化语义摘要；
- 全篇缺陷按最早责任阶段路由：论证缺陷回到论证架构，规划缺陷回到章节规划，纯表达重复只重写受影响章节；
- 全篇模型输入与完整质量上下文分离，实际模型输入受限，完整正文继续用于确定性质量计算和审计；
- 历史167份质量相关Trace全部被新版规则判定为需要修订，解析错误0；
- 新14章正向端到端五条工作流全部完成，全文12维评价全部通过；
- 自动测试45项全部通过，Prompt Pack 30个Prompt、150组Replay静态验证通过。

## 已验证

- 历史Trace负向重放：167/167识别旧缺陷；
- 正向端到端：108次Prompt调用、14章、完整候选14/14、重复与信息键冲突均为0；
- 弱模型上下文：单章蓝图/正文/表达输入最高约33/37/38 KB，全篇实际模型输入约88 KB，完整159 KB质量上下文独立保留；
- Python静态编译、Prompt Pack校验和45项pytest：PASS；
- GitHub Actions在Ubuntu 24.04与Python 3.12环境中完成依赖安装、应用编译、Prompt Pack校验和完整pytest：PASS。

## 明确边界

- 本次公开资料检索与归档为真实连接器结果，模型语义调用仍采用 `SIMULATED`，因为没有用户真实LLM API密钥；不能把该结果表述为真实模型质量测评；
- 当前执行环境没有Docker守护进程和PowerShell，Docker/Windows脚本完成静态、语法和manifest级验证，但未在对应目标系统实际安装；
- `connector` 模式归档的是批准搜索连接器返回的原始记录；需要完整网页/PDF字节快照时，应使用可出网的 `searxng` 模式或让连接器返回文件内容；
- 严格物理隔离环境仍应增加组织级签名、病毒扫描、摆渡介质登记和来源包审批；
- 生产上线仍需身份权限、正式密级映射、受控数据库、密钥管理、DLP、集中审计和专用OOXML完整性验证。
