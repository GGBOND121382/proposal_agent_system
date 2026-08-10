# 统一输出协议

- 只输出JSON对象，不得输出Markdown代码块或解释。
- `prompt_id`、`prompt_version`和`schema_version`必须与本次运行时协议身份及强制输出Schema中的`const`完全一致；不得沿用其他Prompt、Replay或历史版本中的值。
- `status`只能是PASS、REVISE、NEED_USER_INPUT、BLOCK。
- Finding必须定位到具体路径或Span，并给出证据、严重级别、修复边界和路由。
- NEED_USER_INPUT必须生成具体、可回答的问题，禁止只写“请补充信息”。
- BLOCK必须说明不可继续的确定原因，不得用重试掩盖业务或安全问题。
- 输出中的对象引用必须存在于输入Envelope或明确标记为新候选ID。
