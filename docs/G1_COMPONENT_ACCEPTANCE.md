# G1：组件独立验收

> 对应计划：`docs/CONCURRENT_DEVELOPMENT_PLAN.md` 的 G1。  
> 原则：六条轨道保持独立，不在 G1 阶段提前解决跨分支集成冲突；只有每条轨道分别通过后，才允许进入 `agent/integration-v06` 和 G2 小规模集成。

## 1. 固定验收对象

G1 不跟随分支最新提交自动漂移。`governance/g1/components.json` 固定以下六个已完成工作包的 Head：

| 轨道 | 分支 | PR | 固定提交 |
|---|---|---:|---|
| A 运行时与恢复 | `agent/runtime-recovery` | #5 | `7b158f882805375fc657dc7d5fc686653d89335f` |
| B Agent 与 Prompt | `agent/prompt-agents` | #7 | `a65422d411094e9139b26b4a9311058dd64d5f1c` |
| C Research Skill | `agent/research-skill` | #6 | `8ed2d3e30b121ac2dbded4160912322b8dd16cda` |
| D Mermaid 与交付 | `agent/mermaid-export` | #8 | `a0fdeceffee6dd5999feea7d2dfdcff89ebdbf04` |
| E 质量验证 | `agent/quality-gates` | #9 | `ce7f7b59035af1faeb89be0b4a7b0288a71969be` |
| F 测试与证据 | `agent/test-evidence` | #10 | `217f69b272a1ad7aac506d40170798408200e6de` |

任何轨道继续提交后，必须显式更新固定 SHA 并重新执行整个 G1，不允许让 Gate 静默改验收对象。

## 2. 通过条件

每条轨道必须独立满足：

1. 固定提交身份与清单一致；
2. 必需代码、脚本和测试文件存在；
3. 正向、负向、边界和重启证据均在测试源码中存在；
4. 上述测试真实出现在本次 targeted JUnit 中，而不是只在文档中列名；
5. 轨道专项验收命令返回 0；
6. targeted 测试无失败；
7. 该轨道固定提交上的完整 pytest 回归无失败；
8. 专项重启或持久化探针通过。

A 轨道还必须保存并复核：

- 完整请求对象及请求 SHA-256；
- 原始模型响应文本及其 SHA-256；
- 从原始响应解析出的对象；
- Gateway 解析对象 SHA-256；
- 实际消费对象 SHA-256；
- 模型和端点标识。

原始响应对象、Gateway 解析对象和消费对象的哈希不一致时，G1 直接失败。

## 3. 重启证据

- **A**：数据库事务后故障恢复、可恢复 `BLOCKED` 原步骤继续、响应证据篡改阻断；
- **B**：在两个独立 Python 进程中重新加载 Prompt Pack 和规则，验收报告规范化哈希必须一致；
- **C**：重新加载公开研究归档并重算快照哈希，篡改内容必须被识别；
- **D**：同一 Mermaid 输入第二次执行必须命中经哈希复核的缓存，源码/SVG/PNG 哈希保持一致；
- **E**：重新创建数据库连接和 `QualityLifecycleManager` 后，P1 Finding 与阻断状态必须仍然存在；
- **F**：30 个 Agent 合同重载一致，并从各持久化工作流检查点恢复。

## 4. 自动化流程

`.github/workflows/g1.yml` 执行：

```text
固定清单校验
  → A—F 六个独立矩阵 Job 并行运行
      → 检出固定 SHA
      → 安装该组件依赖
      → 编译与 Prompt Pack 校验
      → 专项验收
      → targeted 四类测试
      → 完整分支回归
      → 重启/原始响应探针
      → 生成单轨报告
  → 下载六份证据
  → 生成 G1_ACCEPTANCE.json / G1_ACCEPTANCE.md
  → 六轨全部 PASS 才放行
```

每条轨道上传独立 Artifact；最终汇总 Artifact 保留 30 天。

## 5. 本地控制器校验

```bash
python scripts/validate_g1.py manifest \
  --manifest governance/g1/components.json \
  --report recovery_evidence/g1/G1_MANIFEST.json

python -m pytest -q tests/test_g1_gate.py
```

完整 G1 必须由 GitHub Actions 或等价的六工作目录环境执行，因为控制器需要分别检出六个固定提交。

## 6. CI 清理

通用 `CI` 和 `G0 Gate` 已调整为：

- `actions/checkout@v7`；
- `actions/setup-python@v6`；
- `actions/upload-artifact@v7`；
- 仅在 `main` push、面向 `main` 的 PR 或手工触发时运行；
- 同一 PR/分支的新运行自动取消旧运行。

这消除了 Node.js 20 弃用警告，并减少同一提交因 `push` 与 `pull_request` 同时触发产生的重复历史记录。

## 7. 能力边界

G1 验证组件代码、接口、负向规则、恢复语义和调用证据完整性。A 轨道的审计探针验证真实调用层使用的原始响应保存与哈希约束，但不把固定审计响应或 SIMULATED/REPLAY 结果表述为真实模型语义质量。真实模型、真实材料和真实 Skill 的完整申请书能力属于 G3。
