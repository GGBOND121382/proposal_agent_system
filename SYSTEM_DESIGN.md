# 系统设计

## 总体架构

```mermaid
flowchart LR
    U[浏览器/接口调用方] --> API[FastAPI 控制面]
    API --> ORCH[Workflow Orchestrator]
    API --> DOC[Document Parser]
    ORCH --> CTX[Context Builder]
    CTX --> REG[Prompt Registry + JSON Schema]
    ORCH --> GATE[Human Gate Manager]
    ORCH --> EXEC[Prompt Executor]
    EXEC --> ROUTER[Security Router]
    ROUTER --> OFF[Offline Model Gateway]
    ROUTER --> ON[Online Public Model Gateway]
    ORCH --> SKILL[Skill Executor]
    SKILL --> RS[Public Research Archive]
    SKILL --> MS[Mermaid Render Worker]
    EXEC --> ART[Artifact Store]
    DOC --> DB[(SQLite / Production DB)]
    GATE --> DB
    ART --> DB
    SKILL --> DB
    ORCH --> EXPORT[DOCX/PDF/审计包]
```

## 离线与在线协同

```mermaid
sequenceDiagram
    participant User
    participant Offline as 离线系统
    participant Gate as 安全审批
    participant Agent as Research Plan Agent
    participant Search as 批准检索连接器/SearXNG
    participant Archive as Research Archive Skill

    User->>Offline: 提交公共知识缺口
    Offline->>Offline: Safe Package + Critic + 确定性扫描
    Offline->>Gate: OUTBOUND_SECURITY_APPROVAL
    Gate-->>Offline: APPROVE/RETURN/REJECT
    Offline->>Agent: 仅提供批准的PUBLIC任务包
    Agent->>Search: 原始研究查询
    Search-->>Archive: 查询响应与来源内容
    Archive->>Archive: 覆盖校验、去重、快照、文本、元数据、SHA-256
    Archive-->>Offline: SourceRef/Passage/Manifest
    Offline->>Offline: Synthesis + Critic + Import Critic
    Offline->>Gate: ONLINE_RESULT_IMPORT_APPROVAL
    Gate-->>Offline: 批准后进入候选知识
```

## 弱模型写作与图形闭环

```mermaid
flowchart TD
    A[章节任务] --> B[Write Blueprint]
    B --> C[Blueprint Critic]
    C --> D[Write Content]
    D --> E{是否需要图形}
    E -->|否| F[Write Critic]
    E -->|是| G[模型输出Mermaid源码]
    G --> H[源码安全与复杂度校验]
    H -->|通过| I[持久化Playwright Worker渲染]
    H -->|失败| J[确定性可编辑模板回退]
    I --> K[MMD/SVG/PNG/元数据]
    J --> K
    K --> L[替换为FIGURE工件并插入DOCX]
    L --> F
    F --> M[Integration Critic]
```

## 离线部署双路径

```mermaid
flowchart LR
    EXT[外网构建机] --> A[源码+Wheel+deb/Python/Chromium离线包]
    EXT --> B[应用Docker镜像+可选SearXNG镜像]
    A --> C[内网Manifest校验与一键安装]
    B --> D[内网docker load与Compose启动]
    C --> E[离线模型OpenAI-compatible API]
    D --> E
```

## 核心不变量

1. Producer 不能批准自身输出，Critic 不直接修改正式对象。
2. 所有模型输入和输出都必须通过对应 JSON Schema。
3. Skill 输入、输出、Hash、状态和错误必须可审计。
4. Public Research Agent 生成的每个原始查询必须有检索响应；缺失时流程失败。
5. 低权威来源不能覆盖高权威来源，模型记忆不能替代归档来源。
6. 离线模型失败不得自动回退在线模型。
7. 在线只接受批准且通过扫描的 PUBLIC 上下文，回传结果先进入隔离候选区。
8. Mermaid 不允许脚本、点击事件、外部CDN或动态初始化，渲染失败必须显式记录并回退。
9. 所有 Gate 决定必须匹配上下文Hash、目标版本和所需角色。
10. 最终导出必须同时具备内容审批和导出审批。
