# 大模型 API 配置

## 配置层次

### 1. `.env`

填写实际端点、密钥和 provider 模型名：

```env
MODEL_RUNTIME_MODE=LIVE

OFFLINE_LLM_ENABLED=true
OFFLINE_LLM_BASE_URL=http://127.0.0.1:8000/v1
OFFLINE_LLM_API_KEY=...
OFFLINE_GENERAL_MODEL=general-model-name
OFFLINE_CRITIC_MODEL=critic-model-name

ONLINE_LLM_ENABLED=false
ONLINE_LLM_BASE_URL=
ONLINE_LLM_API_KEY=
ONLINE_PUBLIC_MODEL=
```

### 2. `prompt_pack/config/model_endpoints.yaml`

定义安全域、允许的数据等级、任务类型、网络策略、超时与并发限制。`api_key_secret` 保存的是环境变量名称，不是密钥值。

### 3. `prompt_pack/config/models.yaml`

把系统内部逻辑模型 ID 映射到模型服务里的真实模型名。

### 4. `prompt_pack/config/prompt_model_profiles.yaml`

决定抽取、规划、写作、Critic、公共研究和安全审查分别使用哪个模型，以及温度和最大输出长度。

## 弱模型适配

系统不要求单个模型一次完成整份申请书。运行时将任务拆成：

- 项目规则和事实抽取；
- 章节修订计划；
- 单章蓝图；
- 单章正文；
- 单章 Critic；
- 跨章节 Integration Critic；
- 图形源码生成和代码渲染；
- 公开研究检索、归档与综合。

模型只负责生成 Mermaid 文本，代码负责校验与渲染。模型漏图或 Mermaid 无法渲染时，关键章节采用确定性可编辑模板回退。

## 接口兼容性检查

```bash
python scripts/check_config.py --probe
```

模型 API 应提供：

```text
GET  /v1/models                 # 建议提供
POST /v1/chat/completions       # 必须提供
```

响应应包含：

```json
{"choices":[{"message":{"content":"{...JSON...}"}}]}
```

模型返回值仍会经过 JSON Schema 校验；格式不合法不会直接写入项目事实或正文。
