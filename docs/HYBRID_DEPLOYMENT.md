# 离线—在线协同部署

## 逻辑协同模式

同一应用可以连接内网模型与在线公共研究服务：

```text
内部材料 -> 离线模型生成脱敏任务包 -> 离线 Critic
        -> 人工外发审批 -> 在线模型与 SearXNG
        -> 公开资料快照归档 -> 离线导入 Critic
        -> 人工导入审批 -> 内部写作工作流
```

在线端点只接受 `PUBLIC` 上下文。离线模型调用失败不会自动回退到在线模型。

环境配置：

```env
MODEL_RUNTIME_MODE=LIVE

OFFLINE_LLM_ENABLED=true
OFFLINE_LLM_BASE_URL=http://offline-llm:8000/v1
OFFLINE_LLM_API_KEY=...
OFFLINE_GENERAL_MODEL=...
OFFLINE_CRITIC_MODEL=...

ONLINE_LLM_ENABLED=true
ONLINE_LLM_BASE_URL=https://public-model.example/v1
ONLINE_LLM_API_KEY=...
ONLINE_PUBLIC_MODEL=...

PUBLIC_SEARCH_PROVIDER=searxng
PUBLIC_SEARCH_BASE_URL=http://searxng:8080
PUBLIC_SEARCH_MAX_RESULTS=40
```

`deploy/docker/docker-compose.hybrid.yml` 提供应用与 SearXNG 的组合模板。生产部署应固定 SearXNG 镜像版本、限制搜索引擎和出站域名，并通过出口代理记录访问日志。

## 严格物理隔离模式

若内部环境完全禁止访问外网，应用的公开研究服务不能直接调用在线端点。此时应采用“记录集/摆渡包”方式：

1. 内网导出已审批的 Safe Online Package；
2. 外网研究节点执行搜索和网页归档；
3. 将来源快照、文本、元数据、SHA-256 和结果清单形成签名包；
4. 内网将包作为 `PUBLIC_RESEARCH_RECORD_FILE` 导入；
5. Import Critic 检查后进入人工导入审批。

当前 `recorded` provider 已支持第 4、5 步，且会把导入来源重新归档并生成 manifest。生产环境仍应增加组织级签名、病毒扫描和摆渡介质登记。

## 项目级联网许可

创建项目时必须同时允许：

- `internet_access_allowed=true`；
- `anonymized_external_processing_allowed=true`；
- `allowed_model_endpoint_ids` 包含 `online-public-primary`；
- 外发 Gate 已审批。

否则安全路由器拒绝在线调用。
