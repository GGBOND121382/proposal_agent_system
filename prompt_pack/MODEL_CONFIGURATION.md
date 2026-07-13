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

## 权威配置

- `config/model_endpoints.yaml`：端点环境、安全等级、数据和网络政策；
- `config/models.yaml`：模型实例和能力；
- `config/prompt_model_profiles.yaml`：抽取、Critic、规划、写作等参数；
- `config/prompt_registry.json`：26个Prompt到文件、Schema和Profile的映射；
- `policies/model_routing.yaml`：默认拒绝的模型路由。

离线模型失败时不得自动切换在线模型。CI应使用Mock或Replay，禁止默认真实API调用。
