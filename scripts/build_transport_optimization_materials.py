#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.transport_optimization_application_content import SECTION_TITLES, REF_CATALOG

PROJECT_NAME = "面向复杂物流场景的多智能体运输方案优化关键技术研究"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def build(output_dir: Path) -> Path:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    required = output_dir / "01_upload_required"
    optional = output_dir / "02_upload_optional"
    control = output_dir / "03_control_and_expected"
    required.mkdir(parents=True); optional.mkdir(); control.mkdir()

    write(required / "01_科研项目申报指南.md", """
# 科研项目申报指南（复杂物流场景验证材料）

## 一、总体方向
面向城市配送、园区仓配、多仓协同、多式联运、临时订单和运行异常等复杂物流场景，研究订单理解、任务建模、资源匹配、车辆路径、时间窗、多仓联动、接续优化、动态重规划、多目标评价和人机协同等关键技术，形成可解释、可追踪、可离线部署的多智能体运输方案优化原型系统。

## 二、申请书结构
申请书应包括项目摘要、背景意义、国内外研究现状、需求与边界、科学问题、目标指标、研究内容、总体架构、订单语义、物流网络、知识图谱、多智能体协同、状态感知、需求预测、运力匹配、车辆路径、多仓协同、多式联运、装载容量、动态事件、低扰动重规划、多目标评价、关键技术、算法策略、工具接口、数据工程、执行闭环、技术路线、原型、部署、测试、创新、成果、计划、预算、组织、基础、风险、应用边界、结论、参考文献和完整附录。

## 三、强制要求
1. 公开研究必须由系统内 Public Research Agent 生成查询计划并调用 Research Skill；不得在系统外人工整理后直接塞给写作智能体。
2. 检索连接器必须覆盖智能体生成的全部查询，系统保存原始连接器响应、每条来源快照、标题、作者、日期、URL、访问时间、网页引用标识、摘录和 SHA-256。
3. Research Synthesis 只能使用本次归档的 source_id 形成 PUBLIC_CLAIM，正文引用必须能够回溯到 source_id、检索任务和本地快照。
4. 关键章节由弱模型输出短小 Mermaid 源码，代码完成危险指令拦截、语法清理、Chromium 渲染、PNG/SVG 生成和文档插入；模型漏图或错误时触发确定性模板回退。
5. Mermaid 必须保留 .mmd、.svg、.png 和元数据 JSON，便于用户修改和重新渲染。
6. 所有 Prompt、Skill、工具调用、人工 Gate、模型原始响应和导出过程必须留痕。
7. 必须覆盖 26 个 Prompt、五条主工作流和至少一次 Critic→Targeted Repair→Critic 闭环。
8. 最终文档不少于 60 页，目标与上一复杂申请书相近；参考文献不少于 35 条，图形不少于 10 幅。
9. 系统必须提供 Ubuntu 与 Windows 离线安装包构建/安装脚本，以及 Docker 镜像 save/load 的离线部署流程。
""")

    write(required / "02_项目任务简表.md", f"""
# 项目任务简表

## 项目名称
{PROJECT_NAME}

## 项目定位
面向多来源订单、多类型车辆、多仓库、多站点、时间窗和动态事件，构建能够理解业务输入、调用优化工具、生成候选方案、解释取舍、监控执行并在变化后低扰动调整的多智能体系统。

## 周期与预算
周期 36 个月，预算 80 万元。成果包括项目申请书、业务与数据模型、公开资料归档、优化算法组件、智能体与 Skill、原型系统、场景库、测试报告、离线部署包和完整审计资料。

## 主要目标
- 支持城市配送、园区物流、多仓补货和多式联运四类主场景。
- 支持订单、货物、车辆、仓库、站点、时间窗、库存和网络状态统一建模。
- 支持精确算法、约束规划、启发式搜索和学习型策略的组合求解。
- 支持临时订单、交通变化、车辆不可用和接续延误后的局部重规划。
- 支持成本、时效、服务水平、资源利用率、排放和方案稳定性多目标评价。
- 支持弱模型任务拆分、Mermaid 源码渲染、真实公开检索归档和全过程 Trace。

## 初步指标
硬约束满足率 100%；中等规模初始可行方案时间不超过 60 秒；异常场景重规划成功率不低于 90%；关键结论与方案变更 Trace 覆盖率不低于 95%；公开来源归档完整率 100%；Mermaid 图源留存率 100%。

## 隔离测试用虚构信息
项目联系人：林知行；机构：澄川物流技术研究中心；地址：临江市新港区云桥路 28 号；电话：139-0000-6735；邮箱：lin.zhixing@example.test。上述信息只用于验证公开任务包脱敏，不得进入 ONLINE_PUBLIC 输入。
""")

    framework = "# 全文\n以下目录驱动逐章编制。\n\n" + "\n\n".join(f"# {title}\n请基于确认材料和本次归档的公开证据完成本章。" for title in SECTION_TITLES)
    write(required / "03_申请书初始框架.md", framework)

    write(required / "04_典型场景与业务流程输入.md", """
# 典型场景与业务流程输入

## 场景一：城市多时间窗配送
订单持续到达，车辆类型、装载能力和工作时段不同，客户具有预约时间窗和服务时长。系统需要完成订单聚合、车辆匹配、路径与顺序优化，并在拥堵和临时订单出现后局部调整。

## 场景二：园区仓配协同
园区内存在中心仓、前置仓、生产点和收货点。运输计划需要同时考虑库存可用量、装卸能力、车辆循环、站点排队和班次衔接。

## 场景三：多仓补货与库存运输联动
多个仓库为一组需求点补货。系统需要在缺货风险、库存持有、车辆成本和服务水平之间权衡，决定由哪个仓、何时、以何种批量和路线完成补货。

## 场景四：公铁水多式联运
运输任务可能经过公路、铁路和水运段，涉及接续节点、时刻表、换装时间、班次容量和全程可靠性。系统需要形成多套可解释的联运候选方案。

## 场景五：动态事件与低扰动恢复
事件包括临时订单、车辆不可用、交通时间变化、站点拥堵、库存变化和接续延误。系统先识别影响范围，锁定不受影响的已承诺部分，再对局部对象重规划。

## 业务闭环
需求接入→语义解析→数据与知识校验→候选任务模型→规划 Agent 分解→优化 Skill 求解→Critic 检查→人工 Gate→方案发布→执行监控→事件识别→影响分析→低扰动重规划→版本归档。
""")

    write(required / "05_数据资源与约束输入.md", """
# 数据资源与约束输入

## 主要数据对象
订单、货物、车辆、司机、仓库、库存、站点、装卸资源、路网、铁路班次、水运班次、换装节点、历史运输记录、实时事件、方案版本和评价结果。

## 约束类型
容量、重量、体积、车型、温控、兼容性、时间窗、服务时长、工作时段、仓库库存、装卸能力、道路限制、班次容量、接续时间、最大里程、优先级、拆单规则和人工锁定。

## 数据状态
每个字段记录来源、更新时间、版本、单位、置信度、确认状态和适用范围。缺失的决定性字段不能由模型自行补造，必须生成澄清问题或采用明确标注的场景假设。

## 评价数据
成本、总里程、最大路线时长、准时率、未服务订单、车辆利用率、仓库吞吐、接续等待、排放估算、方案变更量和人工调整次数。
""")

    write(required / "06_优化模型与求解输入.md", """
# 优化模型与求解输入

## 基础问题
车辆路径与时间窗、容量车辆路径、取送一体、异构车队、多仓车辆路径、库存路径、多式联运路径与时刻表、装载配载和动态重规划。

## 求解策略
小规模或关键子问题采用 MILP、CP-SAT、最小费用流和分支定价等精确方法；中大规模采用贪心构造、局部搜索、ALNS、禁忌搜索和多启动策略；学习型模型用于候选生成、排序、预测和启发式引导，不直接绕过硬约束校验。

## 低扰动目标
在恢复可行性的同时，限制车辆更换、顺序变化、时间偏移、仓库切换和已通知订单变化。对已开始执行、临近服务和人工锁定对象设置冻结或高变更成本。

## 输出要求
每次求解保存问题版本、约束摘要、求解器、参数、随机种子、时间限制、状态、目标值、可行性检查、候选方案和解释。
""")

    write(required / "07_多智能体与Skill设计输入.md", """
# 多智能体与 Skill 设计输入

## 智能体角色
需求解析 Agent、项目定义 Agent、事实 Agent、公开研究 Agent、规划 Agent、优化 Agent、Critic Agent、Repair Agent、执行监控 Agent、重规划 Agent、文档编制 Agent 和终审 Agent。

## 弱模型适配原则
一个 Prompt 只完成一个边界清晰的结构化任务；复杂章节拆成蓝图、证据选择、正文、表格、Mermaid 源码和 Critic；模型不负责直接生成图片、下载网页、调用求解器或修改文件系统。

## 核心 Skill
- public_research.archive：接收 Agent 查询计划，调用批准的检索连接器，归档响应、来源快照、文本和哈希。
- mermaid.render：接收短小 Mermaid 源码，由持久化 Playwright Worker 复用 Chromium 渲染 SVG/PNG。
- optimization.solve：后续接入 OR-Tools、MILP 和启发式求解器，保存模型、参数与结果。
- document.export：按结构标记生成 DOCX，并插入经过验证的图形。

## Mermaid 运行方式
LLM 只输出 flowchart 等受限类型源码；代码拒绝 click、javascript、script 和外部资源，清理语法后送入单独 Worker。Worker 连续复用浏览器，达到请求阈值或超时后自动重启。相同源码按哈希缓存。
""")

    write(required / "08_公开研究与真实性输入.md", """
# 公开研究与真实性输入

Public Research Agent 必须先根据课题和章节形成研究问题，再生成检索查询。检索连接器执行 Agent 的原始查询，不允许用预置参考文献替代检索。Research Skill 校验查询覆盖，保存完整 connector_response.json，并为每条来源保存原始结果 JSON、提取文本、metadata.json、URL、访问时间、网页引用标识、SHA-256 和 source_index.csv。

本项目研究范围包括车辆路径与时间窗、动态车辆路径、多仓与库存路径、多式联运、强化学习与学习型组合优化、数字孪生、绿色运输、LLM 工具调用和 OR-Tools 官方能力。写作智能体只能引用本次归档并经 Critic 接受的 PUBLIC_CLAIM。
""")

    write(required / "09_测试评估输入.md", """
# 测试与评估输入

## 分层测试
Schema 与单元测试、Skill 测试、智能体工作流测试、公开研究归档测试、Mermaid 连续渲染测试、优化算法基准测试、场景回放、异常注入、文档与 Trace 验收、离线部署验收。

## 重点用例
字段缺失、单位冲突、无可行车辆、时间窗冲突、库存不足、班次容量不足、车辆不可用、交通时间突变、临时订单、站点延误、模型漏图、Mermaid 语法错误、浏览器进程异常、检索结果缺少某一查询、来源重复、网页不可获取和 API 超时。

## 证据要求
每项测试保存环境、版本、输入、配置、日志、结果、哈希和缺陷编号。长文档测试要求同一 Mermaid Worker 连续处理不少于 20 幅图，并验证自动轮换和失败重启。
""")

    write(required / "10_部署与模型API输入.md", """
# 部署与模型 API 输入

## 离线安装包方案
外网机器运行构建脚本，下载 Python wheelhouse、Ubuntu deb 依赖或 Windows Python 安装器、浏览器与 Mermaid 本地运行时，生成 manifest 和 SHA-256。内网机器运行 install_offline 脚本，先验签/验哈希，再执行 no-index/no-network 安装并注册服务。

## Docker 离线方案
外网构建包含应用、Python 依赖、Chromium、字体、LibreOffice、Mermaid 运行时的镜像，使用 docker save 输出 tar 和校验清单；内网 docker load 后使用离线 compose 一键启动。混合部署可额外连接受控 SearXNG 和在线公开模型。

## 模型 API
系统使用 OpenAI-compatible /chat/completions。离线通用模型、离线 Critic 和在线公开研究模型通过 .env 指定地址、API Key 和真实模型名；端点的安全级别与任务范围由 model_endpoints.yaml、models.yaml 和 prompt_model_profiles.yaml 控制。
""")

    write(required / "11_组织预算与进度输入.md", """
# 组织、预算与进度输入

## 团队
团队覆盖物流业务、运筹优化、智能体、数据工程、后端、前端、测试、部署和项目管理。项目负责人负责目标与资源，算法负责人负责模型与求解器，智能体负责人负责 Prompt 与 Skill，工程负责人负责接口与部署，测试负责人负责场景和证据。

## 预算
总预算 80 万元：计算与离线环境 24 万元，软件与数据 10 万元，算法与系统研发 30 万元，场景验证 11 万元，咨询与成果 5 万元。

## 进度
1—6 月完成需求、场景、数据模型和真实公开研究归档；7—12 月完成任务语义、知识图谱和基础优化；13—20 月完成多智能体规划、车辆路径和多仓协同；21—26 月完成联运、动态事件和低扰动重规划；27—32 月完成原型集成、离线部署和分层测试；33—36 月完成试运行、评估和成果固化。
""")

    write(optional / "12_参考结构与预置评审意见.md", """
# 参考结构与预置评审意见

参考材料仅用于章节顺序、论证模式和格式，不得把其他项目事实、指标和成果迁移到本项目。

评审关注：研究现状是否来自系统内真实检索；每条引用是否可追溯；复杂任务是否拆分以适配弱模型；Mermaid 是否保存源码并由代码渲染；动态重规划是否兼顾可行性与稳定性；部署包是否能在无网络环境安装；Prompt、Skill、资料和文档是否均可审计。
""")

    connector_src = ROOT / "data" / "research_catalog" / "transport_optimization_connector_response.json"
    if not connector_src.exists():
        raise FileNotFoundError(connector_src)
    shutil.copy2(connector_src, control / connector_src.name)
    (control / "transport_optimization_verified_sources.json").write_text(
        json.dumps({"schema_version":"1.0","sources":REF_CATALOG}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    project = {
        "name": PROJECT_NAME,
        "description": "复杂物流运输方案优化、多智能体、真实公开资料归档、Mermaid Skill 与离线部署综合验证",
        "security_level": "INTERNAL",
        "config": {
            "internet_access_allowed": True,
            "anonymized_external_processing_allowed": True,
            "allowed_public_topics": ["车辆路径","动态调度","多仓协同","库存路径","多式联运","物流数字孪生","绿色运输","运筹优化工具"],
            "prohibited_external_fields": ["人员姓名","组织名称","详细地址","联系电话","电子邮箱","真实项目名称"],
            "recipient_scope": ["项目组内部测试人员"],
            "allowed_model_endpoint_ids": ["offline-primary","online-public-primary"],
            "external_redaction_entities": [
                {"value":"林知行","entity_type":"PERSON","placeholder":"[PERSON_1]","field_label":"人员姓名"},
                {"value":"澄川物流技术研究中心","entity_type":"ORG","placeholder":"[ORG_1]","field_label":"组织名称"},
                {"value":PROJECT_NAME,"entity_type":"PROJECT","placeholder":"[PROJECT_1]","field_label":"真实项目名称"},
            ],
            "retention_days": 365,
            "task_instruction": {
                "goal":"生成复杂、可核验、适配弱模型、图源可编辑的物流运输方案优化申请书",
                "must_include":["系统内真实检索归档","国内外研究现状","关键技术","技术路线","Mermaid 图源","离线部署","参考文献","Prompt 和 Skill Trace"],
                "acceptance":["不少于60页","26个Prompt覆盖","至少一次定向修复","至少10幅Mermaid图","不少于35条参考文献"],
            },
        },
    }
    (control / "project_create.json").write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")

    required_prompts = [
        "P-SECURITY-CLASSIFY","P-SECURITY-CLASSIFY-CRITIC","P-SAFE-ONLINE-PACKAGE","P-SAFE-ONLINE-PACKAGE-CRITIC",
        "P-PUBLIC-RESEARCH-PLAN","P-PUBLIC-RESEARCH-SYNTHESIS","P-PUBLIC-RESEARCH-CRITIC","P-ONLINE-RESULT-IMPORT-CRITIC",
        "P-SCHEME-EXTRACT","P-SCHEME-CRITIC","P-PROJECT-DEFINITION-EXTRACT","P-PROJECT-DEFINITION-CRITIC",
        "P-PROJECT-READINESS-CRITIC","P-FACT-EXTRACT","P-FACT-CRITIC","P-TEMPLATE-EXTRACT","P-TEMPLATE-CRITIC",
        "P-REVISION-PLAN","P-REVISION-PLAN-CRITIC","P-WRITE-BLUEPRINT","P-WRITE-BLUEPRINT-CRITIC","P-WRITE-CONTENT",
        "P-WRITE-CRITIC","P-INTEGRATION-CRITIC","P-TARGETED-REPAIR","P-FINAL-CONFIDENTIALITY-REVIEW",
    ]
    expected = {
        "formal_sections": len(SECTION_TITLES), "minimum_pages": 60, "target_pages": 120, "minimum_references": 35,
        "minimum_figures": 10, "minimum_mermaid_sources": 10, "minimum_research_sources": 35,
        "required_prompts": required_prompts,
        "privacy_values_not_allowed_online": ["林知行","澄川物流技术研究中心","临江市新港区云桥路 28 号","139-0000-6735","lin.zhixing@example.test",PROJECT_NAME],
        "required_phrases": ["国内外研究现状","车辆路径与时间窗优化","低扰动重规划","多式联运路径与时刻表优化","技术路线","离线部署","参考文献","Mermaid图形源码"],
    }
    (control / "expected_results.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = [
        ("01_科研项目申报指南.md","APPLICATION_GUIDE","INTERNAL",True), ("02_项目任务简表.md","PROJECT_BRIEF","INTERNAL",True),
        ("03_申请书初始框架.md","CURRENT_PROPOSAL","INTERNAL",True), ("04_典型场景与业务流程输入.md","EVIDENCE_MATERIAL","INTERNAL",True),
        ("05_数据资源与约束输入.md","TECHNICAL_DESIGN","INTERNAL",True), ("06_优化模型与求解输入.md","TECHNICAL_DESIGN","INTERNAL",True),
        ("07_多智能体与Skill设计输入.md","TECHNICAL_DESIGN","INTERNAL",True), ("08_公开研究与真实性输入.md","EVIDENCE_MATERIAL","INTERNAL",True),
        ("09_测试评估输入.md","EVIDENCE_MATERIAL","INTERNAL",True), ("10_部署与模型API输入.md","TECHNICAL_DESIGN","INTERNAL",True),
        ("11_组织预算与进度输入.md","BUDGET_MATERIAL","INTERNAL",True), ("12_参考结构与预置评审意见.md","REFERENCE_PROPOSAL","INTERNAL",False),
    ]
    with (control / "upload_manifest.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(["filename","role","security_level","required"]); w.writerows(rows)

    zip_path = output_dir.with_suffix(".zip")
    if zip_path.exists(): zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in output_dir.rglob("*"):
            if p.is_file(): zf.write(p, arcname=p.relative_to(output_dir).as_posix())
    return zip_path


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/mnt/data/transport_optimization_materials_v1")
    print(build(out.resolve()))


if __name__ == "__main__":
    main()
