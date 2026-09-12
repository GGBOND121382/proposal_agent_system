# 运行依赖预检与配置等待状态

日期：2026-07-30

## 目标

模型端点、公开搜索、证据目录、Stage 输入或导出工具缺失时，工作流不再消耗技术重试次数并写成普通 `BLOCKED`。系统改为保存当前步骤和依赖问题，进入 `WAITING_CONFIGURATION`。运维人员修正 `.env`、文件或项目权限并重启服务后，可从原步骤继续。

## 检查层级

1. 应用静态检查：数据目录、模型证据目录、导出证据目录、Prompt Pack、运行策略、超时、Mermaid 运行时和浏览器。
2. 工作流启动检查：所需离线/在线模型、项目联网与匿名外发权限、公开搜索 Provider、Stage 运行目录。
3. 步骤前检查：当前 Prompt 的真实路由、公开搜索计划与 Connector 覆盖、Stage 4A/5 输入、Stage 8 的 LibreOffice 和中文字体。
4. 运行时分类：401/403/404/429/5xx、连接失败、搜索服务失败、只读目录、磁盘空间和渲染/导出依赖错误会转换为可恢复配置等待。

网络探测不会在状态页面隐式执行。使用下列接口或脚本显式探测：

```text
POST /api/config/probe?timeout_seconds=10
GET  /api/projects/{project_id}/dependency-preflight?workflow_type=WF-3_HYBRID_ONLINE_ASSIST
```

```bash
python scripts/check_config.py --env-file .env --probe --render-mermaid
```

## 恢复语义

`configuration_wait` 保存：

- 依赖名称和错误代码；
- 需要检查的配置项；
- 原工作流 ID；
- `resume_step` 或 `resume_stage`；
- 首次发现和最近复检时间。

修复配置并重启后，对原工作流执行一次 `advance`。已通过的 Prompt、人工 Gate、公开研究计划和模型调用证据不会重做。Stage 工作流只复检待进入的下一阶段，不会因已有非空 `run_root` 永久等待。

## 安全边界

预检不放宽模型路由、密级、隐私扫描、联网权限或人工审批。缺失配置只能使工作流等待，不能让不合格输入绕过原安全控制。

## 启动与导出边界

缺少 Mermaid 运行时不再使 FastAPI 在导入阶段直接崩溃。技能仍可注册，配置状态接口会报告 `MERMAID_RUNTIME_NOT_FOUND`；只有真正执行图示渲染时才会在技能边界产生可分类、可恢复的依赖错误。

直接 DOCX 导出和审计包导出会先复检数据与证据目录。PDF 转换、页数验收和 Stage 8 额外检查 LibreOffice、浏览器与中文字体。配置缺失时接口返回结构化 HTTP 409，不会在导出过程中留下半成品后才报错。
