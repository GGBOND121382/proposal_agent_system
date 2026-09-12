# 模型调用配置 V2

## 必填环境变量

- `OFFLINE_LLM_BASE_URL`
- `OFFLINE_LLM_API_KEY`
- `OFFLINE_GENERAL_MODEL`
- `OFFLINE_CRITIC_MODEL`

在线能力默认关闭。启用前还需：

- `ONLINE_LLM_ENABLED=true`
- `ONLINE_LLM_BASE_URL`
- `ONLINE_LLM_API_KEY`
- `ONLINE_PUBLIC_MODEL`

`.env`只选择端点和Provider模型，不再重复配置token上限。

## Token预算分层

- `config/models.yaml`中的`provider_capabilities`：按真实`provider_model_name`登记上下文窗口、推荐输出、硬输出上限和API参数名；
- `config/prompt_model_profiles.yaml`中的`desired_output_tokens`：只描述抽取、Critic、规划、写作等任务希望获得的输出预算；
- Runtime：结合实际路由模型、任务预算、估算输入长度和上下文安全余量，计算本次`effective_output_tokens`；
- `config/model_endpoints.yaml`：只描述端点环境、安全、网络、超时和并发，不再承担模型token能力。

当前MiniMax OpenAI-compatible配置登记：
- `MiniMax-M3`：context 1,000,000；recommended output 131,072；hard max output 524,288；
- `MiniMax-M2.7` / `MiniMax-M2.7-highspeed`：context 204,800；recommended output 65,536；hard max output 204,800。

未知MiniMax模型在LIVE调用前fail-closed，必须先登记Provider能力，避免模型切换后沿用旧上限。

## 权威配置

- `config/model_endpoints.yaml`：端点环境、安全等级、数据和网络政策；
- `config/models.yaml`：逻辑模型实例与Provider模型能力；
- `config/prompt_model_profiles.yaml`：抽取、Critic、规划、写作等任务参数与期望输出预算；
- `config/prompt_registry.json`：30个Prompt到文件、Schema和Profile的映射；
- `policies/model_routing.yaml`：默认拒绝的模型路由。

离线模型失败时不得自动切换在线模型。CI应使用Mock或Replay，禁止默认真实API调用。
