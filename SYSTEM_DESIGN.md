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
    ORCH --> SEARCH[Approved Public Search]
    EXEC --> ART[Artifact Store]
    DOC --> DB[(SQLite / Production DB)]
    GATE --> DB
    ART --> DB
    ORCH --> EXPORT[DOCX Patch/Export Package]
```

## 离线与在线协同

```mermaid
sequenceDiagram
    participant User
    participant Offline as 离线系统
    participant Gate as 安全审批
    participant Online as 在线公共系统

    User->>Offline: 提交公共知识缺口
    Offline->>Offline: P-SAFE-ONLINE-PACKAGE
    Offline->>Offline: Safe Package Critic + 确定性扫描
    Offline->>Gate: OUTBOUND_SECURITY_APPROVAL
    Gate-->>Offline: APPROVE/RETURN/REJECT
    Offline->>Online: 仅发送批准的 PUBLIC Safe Package
    Online->>Online: 研究计划、检索、综合、在线 Critic
    Online-->>Offline: 回传到隔离候选区
    Offline->>Offline: P-ONLINE-RESULT-IMPORT-CRITIC
    Offline->>Gate: ONLINE_RESULT_IMPORT_APPROVAL
    Gate-->>Offline: 批准后才进入项目候选知识
```

## 申请书编写闭环

```mermaid
flowchart TD
    A[准备度检查] --> B[Revision Plan]
    B --> C[Plan Critic]
    C -->|PASS| D[PLAN_CONFIRMATION]
    C -->|REVISE且可局部修复| R[Targeted Repair 一次]
    R --> C
    D --> E[Write Blueprint]
    E --> F[Blueprint Critic]
    F --> G[Write Content]
    G --> H[Write Critic]
    H -->|PASS| I[CANDIDATE_REVIEW]
    H -->|REVISE| R2[Targeted Repair 一次]
    R2 --> H
    I --> J[Integration Critic]
    J --> K[Final Confidentiality Review]
    K --> L[FINAL_CONTENT_SECURITY_APPROVAL]
    L --> M[FINAL_EXPORT_APPROVAL]
    M --> N[DOCX + Manifest + Integrity Report]
```

## 核心不变量

1. Producer 不能批准自身输出，Critic 不直接修改正式对象。
2. 所有模型输入和输出都必须通过对应 JSON Schema。
3. 低权威来源不能覆盖高权威来源，模型推断不能成为正式事实。
4. UNKNOWN、CONFLICTED、TO_BE_SELECTED 不得被语言流畅性掩盖。
5. 离线模型失败不得自动回退在线模型。
6. 在线结果不能直接成为确认事实或正式正文。
7. 所有 Gate 决定必须匹配上下文 Hash 和所需角色。
8. 最终导出必须同时具备内容保密审批和导出审批。
