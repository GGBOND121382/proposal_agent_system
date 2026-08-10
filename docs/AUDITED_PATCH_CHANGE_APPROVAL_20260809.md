# 2026-08-09 审计补丁变更批准

## 范围

本批准记录覆盖 `proposal_agent_audited_relative_patch_bundle_20260809.zip` 引入的冻结接口、Prompt 输入契约与安全运行时变更，以及补丁审查后追加的 Windows 私有存储 ACL 修复。

## 接口变更

- Prompt 输入 Schema 统一收紧显式工作流上下文、候选对象身份与人工门禁关联字段。
- 集成 Critic 和共享输出协议补充统一状态、可修复性及输出边界约束。
- 数据库与执行器增加凭据脱敏、候选一致性和运行证据约束。

上述变更保持 Prompt 注册数量、工作流状态机结构和产物接口的 G0 语义不变量。

## 安全变更

- 路由、隐私、模型故障和人工门禁路径增加凭据脱敏、显式外发批准及失败关闭行为。
- POSIX 私有存储继续使用目录 `0700`、文件 `0600`。
- Windows 私有存储改用受保护 DACL，仅授予当前用户和 SYSTEM 完全访问，并对已有私有目录树进行加固。
- 私有权限无法应用时中止敏感数据写入，不再忽略权限错误。

## 验证要求

- `python scripts/validate_g0.py`
- `python prompt_pack/tools/validate_pack.py`
- `python -m pytest -q tests/test_g0_contract.py tests/test_security_audit_hardening.py`
- 完整 pytest 回归
