from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from .skills.executor import SkillExecutionError, SkillExecutor
from .util import new_id, sha256_json, utc_now


class DiagramEnrichmentService:
    """Turns small Mermaid tasks into rendered document figures.

    Writing models only need to emit a short Mermaid source block. Rendering,
    source archiving, validation and deterministic fallback are handled in code.
    """

    KEY_SECTION_HINTS = {
        "总体架构": ("总体架构逻辑结构图", "architecture"),
        "系统架构": ("系统总体架构图", "architecture"),
        "技术路线": ("总体技术路线图", "route"),
        "关键技术": ("关键技术关系图", "technology"),
        "执行流程": ("关键执行流程图", "workflow"),
        "业务流程": ("业务闭环流程图", "workflow"),
        "研究内容": ("研究内容关系图", "research"),
        "测试验证": ("测试验证流程图", "validation"),
        "评估": ("评估闭环图", "validation"),
    }

    def __init__(self, db, pack, skill_executor: SkillExecutor):
        self.db = db
        self.pack = pack
        self.skill_executor = skill_executor
        # Mermaid rendering owns one persistent browser process.  A dedicated
        # thread keeps all pipe I/O and SkillExecutor database writes on a
        # stable thread instead of asyncio's rotating default thread pool.
        self._render_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mermaid-render")

    async def enrich(
        self,
        *,
        project_id: str,
        workflow_id: str | None,
        run_id: str,
        section: dict[str, Any],
        output: dict[str, Any],
        security_level: str,
    ) -> dict[str, Any]:
        result = output.get("result") or {}
        paragraphs = result.get("paragraphs") or []
        rendered = 0
        for paragraph in paragraphs:
            text = str(paragraph.get("text") or "")
            if not text.startswith("[[MERMAID]]"):
                continue
            caption, width_cm, source = self._parse_marker(text)
            try:
                skill_result = await self._execute_mermaid(
                    {
                        "section_id": section.get("section_id") or section.get("title") or "section",
                        "caption": caption,
                        "width_cm": width_cm,
                        "mermaid_source": source,
                    },
                    project_id=project_id, workflow_id=workflow_id, security_level=security_level,
                )
            except SkillExecutionError as exc:
                fallback = self._fallback_for_section(str(section.get("title") or ""), project_id)
                if not fallback:
                    output.setdefault("warnings", []).append(
                        f"章节《{section.get('title')}》的Mermaid源码渲染失败，已保留调用输入和错误日志：{exc}"
                    )
                    continue
                fallback_caption, fallback_source = fallback
                skill_result = await self._execute_mermaid(
                    {
                        "section_id": section.get("section_id") or section.get("title") or "section",
                        "caption": fallback_caption,
                        "width_cm": width_cm,
                        "mermaid_source": fallback_source,
                        "fallback_reason": "invalid_model_mermaid_source",
                    },
                    project_id=project_id, workflow_id=workflow_id, security_level=security_level,
                )
                output.setdefault("warnings", []).append(
                    f"章节《{section.get('title')}》的模型Mermaid源码未通过校验，已切换为可编辑模板：{exc}"
                )
            paragraph["text"] = skill_result.output["figure_marker"]
            paragraph["paragraph_role"] = "图示"
            rendered += 1

        # Weak-model fallback: key design chapters must still receive a simple,
        # editable diagram even when the model omitted the Mermaid block.
        if rendered == 0:
            fallback = self._fallback_for_section(str(section.get("title") or ""), project_id)
            if fallback:
                caption, source = fallback
                skill_result = await self._execute_mermaid(
                    {
                        "section_id": section.get("section_id") or section.get("title") or "section",
                        "caption": caption,
                        "width_cm": 15.5,
                        "mermaid_source": source,
                        "fallback_reason": "writing_model_omitted_required_diagram",
                    },
                    project_id=project_id, workflow_id=workflow_id, security_level=security_level,
                )
                sequence = max([int(p.get("sequence") or 0) for p in paragraphs] or [0]) + 1
                paragraph_id = new_id("diagram-paragraph")
                paragraphs.append(
                    {
                        "paragraph_id": paragraph_id,
                        "sequence": sequence,
                        "paragraph_role": "图示",
                        "text": skill_result.output["figure_marker"],
                        "blueprint_paragraph_id": (paragraphs[-1].get("blueprint_paragraph_id") if paragraphs else "bp-diagram"),
                        "trace_link_ids": [],
                        "preserved_source_span": None,
                        "contains_unresolved_placeholder": False,
                    }
                )
                rendered = 1
                output.setdefault("warnings", []).append(
                    f"章节《{section.get('title')}》未提供Mermaid源码，系统使用可编辑的确定性模板补充图示。"
                )

        if rendered:
            result["paragraphs"] = sorted(paragraphs, key=lambda item: int(item.get("sequence") or 0))
            result["candidate_text"] = "\n\n".join(str(p.get("text") or "") for p in result["paragraphs"])
            self._persist_enriched_output(project_id, workflow_id, run_id, output, security_level)
        return output

    async def _execute_mermaid(
        self, payload: dict[str, Any], *, project_id: str, workflow_id: str | None, security_level: str
    ):
        loop = asyncio.get_running_loop()
        call = partial(
            self.skill_executor.execute,
            "mermaid.render",
            payload,
            project_id=project_id,
            workflow_id=workflow_id,
            security_level=security_level,
        )
        return await loop.run_in_executor(self._render_executor, call)

    def _persist_enriched_output(
        self,
        project_id: str,
        workflow_id: str | None,
        run_id: str,
        output: dict[str, Any],
        security_level: str,
    ) -> None:
        output_json = json.dumps(output, ensure_ascii=False)
        output_hash = sha256_json(output)
        self.db.execute(
            "UPDATE prompt_runs SET output_json=?,output_hash=? WHERE id=?",
            (output_json, output_hash, run_id),
        )
        row = self.db.fetchone(
            "SELECT COALESCE(MAX(version),0) AS v FROM artifacts WHERE project_id=? AND prompt_id='P-WRITE-CONTENT'",
            (project_id,),
        )
        version = int(row["v"]) + 1 if row else 1
        self.db.execute(
            """INSERT INTO artifacts(
                   id,project_id,workflow_id,artifact_type,prompt_id,version,status,
                   security_level,context_hash,content_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("artifact"),
                project_id,
                workflow_id,
                "SKILL_ENRICHED_PROMPT_OUTPUT",
                "P-WRITE-CONTENT",
                version,
                output.get("status", "PASS"),
                security_level,
                output_hash,
                output_json,
                utc_now(),
            ),
        )
        self.db.audit(
            "PROMPT_OUTPUT_ENRICHED",
            project_id=project_id,
            object_id=run_id,
            metadata={"prompt_id": "P-WRITE-CONTENT", "output_hash": output_hash},
        )

    @staticmethod
    def _parse_marker(text: str) -> tuple[str, float, str]:
        lines = text.splitlines()
        header = lines[0].removeprefix("[[MERMAID]]").strip()
        parts = [item.strip() for item in header.split("|")]
        caption = parts[0] if parts and parts[0] else "结构图"
        try:
            width_cm = float(parts[1]) if len(parts) > 1 else 15.0
        except ValueError:
            width_cm = 15.0
        source = "\n".join(lines[1:]).strip()
        if not source:
            raise ValueError("Mermaid marker has no source body")
        return caption, width_cm, source

    def _fallback_for_section(self, title: str, project_id: str) -> tuple[str, str] | None:
        project = self.db.fetchone("SELECT name FROM projects WHERE id=?", (project_id,)) or {}
        project_name = str(project.get("name") or "")
        domain = "transport" if any(token in project_name for token in ["物流", "运输方案", "车辆路径", "多式联运"]) else "generic"
        for keyword, (caption, template) in self.KEY_SECTION_HINTS.items():
            if keyword in title:
                return caption, self._template(template, domain)
        return None

    @staticmethod
    def _template(template: str, domain: str = "generic") -> str:
        if domain == "transport":
            transport_templates = {
                "architecture": """flowchart TB
U[业务用户与外部系统] --> A[任务接入与语义解析]
A --> K[物流知识与状态底座]
K --> P[多智能体规划与协同]
P --> S[优化求解与方案生成]
S --> E[执行监控与事件检测]
E --> R[低扰动重规划]
R --> S
P --> G[人工Gate与权限控制]
S --> O[方案发布与解释]""",
                "route": """flowchart LR
A[设计输入与场景分析] --> B[订单/资源/网络建模]
B --> C[公开研究与知识底座]
C --> D[多智能体规划]
D --> E[混合优化求解]
E --> F[执行监控与重规划]
F --> G[场景验证与部署]
G --> H[成果固化]""",
                "technology": """flowchart TB
A[任务语义理解] --> D[统一优化问题]
B[物流知识图谱] --> D
C[实时状态感知] --> D
D --> E[路径/分仓/联运求解]
E --> F[多目标评价]
F --> G[执行监控]
G --> H[低扰动重规划]
H --> E""",
                "workflow": """flowchart LR
A[订单进入] --> B[字段与约束校验]
B --> C[资源和网络快照]
C --> D[初始方案求解]
D --> E[Critic与人工确认]
E --> F[执行监控]
F --> G{发生事件?}
G -- 否 --> H[完成与复盘]
G -- 是 --> I[影响分析与局部重规划]
I --> E""",
                "research": """flowchart TB
O[总体目标] --> R1[知识与任务建模]
O --> R2[多智能体协同]
O --> R3[混合优化求解]
O --> R4[动态重规划]
O --> R5[检索归档与证据追踪]
R1 --> V[原型与场景验证]
R2 --> V
R3 --> V
R4 --> V
R5 --> V""",
                "validation": """flowchart LR
A[Schema与单元测试] --> B[Skill和工具测试]
B --> C[算法基准测试]
C --> D[工作流端到端测试]
D --> E[异常场景回放]
E --> F[文档与Trace验收]
F --> G[发布与回归]""",
            }
            return transport_templates[template]
        templates = {
            "architecture": """flowchart TB
U[用户与业务系统] --> G[接入与身份校验]
G --> O[流程编排与策略控制]
O --> K[密钥与可信服务]
O --> C[通信会话与协议适配]
K --> A[审计、监测与生命周期管理]
C --> A""",
            "route": """flowchart LR
R[需求与场景分析] --> M[威胁建模与指标设计]
M --> P[协议与密码能力设计]
P --> I[原型实现与系统集成]
I --> T[分层测试与场景验证]
T --> E[评估优化与成果固化]""",
            "technology": """flowchart TB
A[身份可信] --> D[会话建立]
B[密钥管理] --> D
C[协议适配] --> D
D --> E[端到端保护]
E --> F[动态更新与撤销]
F --> G[审计与评估]""",
            "workflow": """flowchart LR
A[任务受理] --> B[身份与设备校验]
B --> C[策略匹配]
C --> D[建立安全会话]
D --> E[消息传输与状态监测]
E --> F{异常?}
F -- 否 --> G[结束与审计]
F -- 是 --> H[重协商/切换/撤销]
H --> D""",
            "research": """flowchart TB
O[总体目标] --> R1[协议与体系架构]
O --> R2[密钥生命周期管理]
O --> R3[异构终端适配]
O --> R4[验证评估与治理]
R1 --> V[原型系统]
R2 --> V
R3 --> V
R4 --> V""",
            "validation": """flowchart LR
A[单元与接口测试] --> B[协议一致性测试]
B --> C[功能与性能测试]
C --> D[安全性与鲁棒性测试]
D --> E[典型场景回放]
E --> F[问题闭环与回归验证]""",
        }
        return templates[template]
