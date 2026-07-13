# 统一输出协议 V2

- 只输出JSON对象，不得输出Markdown代码块或解释。
- `schema_version`固定为`2.0`，`prompt_version`固定为`2.0.0`。
- `status`只能是PASS、REVISE、NEED_USER_INPUT、BLOCK。
- Finding必须定位到具体路径或Span，并给出证据、严重级别、修复边界和路由。
- NEED_USER_INPUT必须生成具体、可回答的问题，禁止只写“请补充信息”。
- BLOCK必须说明不可继续的确定原因，不得用重试掩盖业务或安全问题。
- 输出中的对象引用必须存在于输入Envelope或明确标记为新候选ID。
