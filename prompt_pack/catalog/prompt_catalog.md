# 26个顶层Prompt目录 V2

| 序号 | Prompt | 执行角色 | 环境 | 模型Profile | 后续人工Gate | 文件 |
|---:|---|---|---|---|---|---|
| 1 | `P-SECURITY-CLASSIFY` | Security Review Agent | `OFFLINE_LOCAL` | `security_review` | `NONE` | `prompts/security/security_classify.md` |
| 2 | `P-SECURITY-CLASSIFY-CRITIC` | Critic Agent | `OFFLINE_LOCAL` | `security_review` | `NONE` | `prompts/security/security_classify_critic.md` |
| 3 | `P-SAFE-ONLINE-PACKAGE` | Security Review Agent | `OFFLINE_LOCAL` | `extraction` | `NONE` | `prompts/security/safe_online_package.md` |
| 4 | `P-SAFE-ONLINE-PACKAGE-CRITIC` | Critic Agent | `OFFLINE_LOCAL` | `critic` | `OUTBOUND_SECURITY_APPROVAL` | `prompts/security/safe_online_package_critic.md` |
| 5 | `P-ONLINE-RESULT-IMPORT-CRITIC` | Critic Agent | `OFFLINE_LOCAL` | `security_review` | `ONLINE_RESULT_IMPORT_APPROVAL` | `prompts/security/online_result_import_critic.md` |
| 6 | `P-FINAL-CONFIDENTIALITY-REVIEW` | Critic Agent | `OFFLINE_LOCAL` | `security_review` | `FINAL_CONTENT_SECURITY_APPROVAL` | `prompts/security/final_confidentiality_review.md` |
| 7 | `P-PUBLIC-RESEARCH-PLAN` | Public Research Agent | `ONLINE_PUBLIC` | `public_research` | `NONE` | `prompts/public_research/research_plan.md` |
| 8 | `P-PUBLIC-RESEARCH-SYNTHESIS` | Public Research Agent | `ONLINE_PUBLIC` | `public_research` | `NONE` | `prompts/public_research/research_synthesis.md` |
| 9 | `P-PUBLIC-RESEARCH-CRITIC` | Critic Agent | `ONLINE_PUBLIC` | `public_research` | `NONE` | `prompts/public_research/research_critic.md` |
| 10 | `P-SCHEME-EXTRACT` | Project Knowledge Agent | `OFFLINE_LOCAL` | `extraction` | `NONE` | `prompts/scheme/scheme_extract.md` |
| 11 | `P-SCHEME-CRITIC` | Critic Agent | `OFFLINE_LOCAL` | `critic` | `SCHEME_CONFIRMATION` | `prompts/scheme/scheme_critic.md` |
| 12 | `P-PROJECT-DEFINITION-EXTRACT` | Project Knowledge Agent | `OFFLINE_LOCAL` | `extraction` | `NONE` | `prompts/project_definition/project_definition_extract.md` |
| 13 | `P-PROJECT-DEFINITION-CRITIC` | Critic Agent | `OFFLINE_LOCAL` | `critic` | `PROJECT_DEFINITION_CONFIRMATION` | `prompts/project_definition/project_definition_critic.md` |
| 14 | `P-PROJECT-READINESS-CRITIC` | Critic Agent | `OFFLINE_LOCAL` | `critic` | `PROJECT_GAP_RESOLUTION` | `prompts/project_definition/project_readiness_critic.md` |
| 15 | `P-FACT-EXTRACT` | Project Knowledge Agent | `OFFLINE_LOCAL` | `extraction` | `NONE` | `prompts/fact/fact_extract.md` |
| 16 | `P-FACT-CRITIC` | Critic Agent | `OFFLINE_LOCAL` | `critic` | `FACT_CONFIRMATION` | `prompts/fact/fact_critic.md` |
| 17 | `P-TEMPLATE-EXTRACT` | Template Agent | `OFFLINE_LOCAL` | `extraction` | `NONE` | `prompts/template/template_extract.md` |
| 18 | `P-TEMPLATE-CRITIC` | Critic Agent | `OFFLINE_LOCAL` | `critic` | `TEMPLATE_CONFIRMATION` | `prompts/template/template_critic.md` |
| 19 | `P-REVISION-PLAN` | Planning Agent | `OFFLINE_LOCAL` | `planning` | `NONE` | `prompts/planning/revision_plan.md` |
| 20 | `P-REVISION-PLAN-CRITIC` | Critic Agent | `OFFLINE_LOCAL` | `critic` | `PLAN_CONFIRMATION` | `prompts/planning/revision_plan_critic.md` |
| 21 | `P-WRITE-BLUEPRINT` | Writing Agent | `OFFLINE_LOCAL` | `planning` | `NONE` | `prompts/writing/write_blueprint.md` |
| 22 | `P-WRITE-BLUEPRINT-CRITIC` | Critic Agent | `OFFLINE_LOCAL` | `critic` | `NONE` | `prompts/writing/write_blueprint_critic.md` |
| 23 | `P-WRITE-CONTENT` | Writing Agent | `OFFLINE_LOCAL` | `formal_writing` | `NONE` | `prompts/writing/write_content.md` |
| 24 | `P-WRITE-CRITIC` | Critic Agent | `OFFLINE_LOCAL` | `critic` | `CANDIDATE_REVIEW` | `prompts/writing/write_critic.md` |
| 25 | `P-INTEGRATION-CRITIC` | Integration Agent | `OFFLINE_LOCAL` | `critic` | `NONE` | `prompts/integration/integration_critic.md` |
| 26 | `P-TARGETED-REPAIR` | Original Producer | `SAME_AS_ORIGINAL` | `extraction` | `NONE` | `prompts/repair/targeted_repair.md` |
