# 阶段0：运行基线与记录链路验收

## 验收结论

- 结果：**PASS**
- 工作流：`wf-d0b43c9ab12a423d`，最终状态 `COMPLETED`
- 模型节点：9个请求、9个响应，全部一一对应
- 人工确认：4次请求、4次决定、4次消费，全部闭合
- 运行记录：130个清单文件，哈希错误 0 项
- 检查点：12个，其中数据库快照 12份
- 压缩包：stage0_trace_complete_20260721.zip，SHA-256 `2f1e7a796f5a94bd829cead5e8dde81766df2c18d22695ccb58cb598e2ff9388`

## 九个模型节点

- `P-PROJECT-READINESS-CRITIC`：PASS；实际模型 `gpt-5.6-thinking`；端点 `chatgpt-conversation-file-bridge`
- `P-SCHEME-EXTRACT`：PASS；实际模型 `gpt-5.6-thinking`；端点 `chatgpt-conversation-file-bridge`
- `P-SECURITY-CLASSIFY-CRITIC`：PASS；实际模型 `gpt-5.6-thinking`；端点 `chatgpt-conversation-file-bridge`
- `P-SECURITY-CLASSIFY`：PASS；实际模型 `gpt-5.6-thinking`；端点 `chatgpt-conversation-file-bridge`
- `P-PROJECT-DEFINITION-CRITIC`：PASS；实际模型 `gpt-5.6-thinking`；端点 `chatgpt-conversation-file-bridge`
- `P-FACT-EXTRACT`：PASS；实际模型 `gpt-5.6-thinking`；端点 `chatgpt-conversation-file-bridge`
- `P-FACT-CRITIC`：PASS；实际模型 `gpt-5.6-thinking`；端点 `chatgpt-conversation-file-bridge`
- `P-SCHEME-CRITIC`：PASS；实际模型 `gpt-5.6-thinking`；端点 `chatgpt-conversation-file-bridge`
- `P-PROJECT-DEFINITION-EXTRACT`：PASS；实际模型 `gpt-5.6-thinking`；端点 `chatgpt-conversation-file-bridge`

## 四个人工确认节点

- `PROJECT_DEFINITION_CONFIRMATION`：CONFIRM，角色 `PROJECT_OWNER`，已消费：true
- `FACT_CONFIRMATION`：CONFIRM，角色 `PROJECT_OWNER`，已消费：true
- `SCHEME_CONFIRMATION`：CONFIRM，角色 `PROJECT_OWNER`，已消费：true
- `PROJECT_GAP_RESOLUTION`：CONFIRM，角色 `PROJECT_OWNER`，已消费：true

## 阶段中间结果

- 项目定义：12类核心项目对象、10条关系、1个中心设计命题、1个工程问题。
- 事实账本：25条原子事实，无冲突；团队、经费、正式申报机构信息保持未决。
- 准备度：只放行到 `READY_FOR_ARGUMENT_ARCHITECTURE`；未放行章节规划和正式正文。

## 本阶段修复

1. Markdown有序列表保持为正文。
2. 确定性校验可以追加状态与Finding，但不得改写模型业务内容。
3. 阶段号、工作流号、版本号不再被当作实质量化指标；真正数量仍须完整绑定。
4. 默认任务要求改为阶段中性，不再默认要求所有任务生成完整申请书。

## 测试

- 定向回归：9项通过。
- 完整回归：244 passed, 2 skipped, 1 warning in 238.49s (0:03:58)。

## 边界

阶段0不生成“人机协同决策优势冲刺”申请书，仅验证修复版智能体的项目受理、文件桥、独立审查、人工确认和记录链路。下一阶段才进入正式设计输入。
