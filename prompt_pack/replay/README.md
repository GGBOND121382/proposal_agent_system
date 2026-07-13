# Replay回归集 V2

`replay/cases/`已包含26个Prompt各5类实际文件，共130组：

- `normal`：正常业务输入；
- `missing_input`：Schema合法但业务信息不足，应返回NEED_USER_INPUT；
- `schema_error`：故意破坏Envelope，应在模型调用前被拒绝；
- `high_risk`：安全或高风险输入，应返回BLOCK；
- `need_user_input`：关键事实未确认，应生成具体问题。

这些文件已经通过静态Schema验证。它们是开发和CI的固定回归基线，但尚未代表真实模型已经逐组运行并达到业务质量要求。真实模型接入后，应保存实际响应并与`expected_output`中的状态、Finding类别和关键字段比较。
