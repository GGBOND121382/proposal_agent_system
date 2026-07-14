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

from app.logistics_application_content import SECTION_TITLES, REF_CATALOG

PROJECT_NAME = "面向复杂服务场景的后勤保障智能体关键技术研究"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def build(output_dir: Path) -> Path:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    req = output_dir / "01_upload_required"
    opt = output_dir / "02_upload_optional"
    ctl = output_dir / "03_control_and_expected"
    req.mkdir(parents=True); opt.mkdir(); ctl.mkdir()

    guide = """# 科研项目申报指南（系统测试版）

## 一、总体定位
申报项目应面向复杂服务保障与资源协同场景，研究智能体、知识增强、流程编排、优化调度和人机协同等关键技术，形成可验证的原型系统和方法体系。

## 二、申请书结构要求
申请书至少包括项目摘要、背景意义、国内外研究现状、需求分析、目标指标、研究内容、关键科学问题、关键技术、技术路线、架构设计、数据与知识工程、算法模型、接口设计、安全审计、原型实现、验证方案、创新点、成果、进度、组织、预算、基础、风险、伦理边界、结论和参考文献。可通过附录给出Prompt覆盖矩阵、数据字典、工具接口、测试用例、验收规则、日志留存规范和公开证据映射。

## 三、强制质量要求
1. 国内外研究现状应按技术分支梳理代表性工作，说明其贡献、适用条件与不足，并提出本项目切入点。
2. 关键技术、技术路线和总体设计章节必须图文结合；全文至少包括业务闭环图、逻辑结构图、技术路线图、知识图谱模式图、多智能体协同图、动态重规划闭环图、部署架构图、评估框架图和里程碑图。
3. 全文参考文献不少于30条，正文引用编号与文末条目一致；优先采用论文、标准和官方技术文档。
4. 全部模型调用必须保留完整System Prompt、任务输入、输出Schema、原始响应、解析输出、模型路由、执行耗时、错误信息和Trace；普通审计日志仅记录元数据，全文内容进入受控审计工件。
5. 必须执行方案提取、项目定义、事实抽取、公开研究、修订计划、逐章蓝图、逐章写作、逐章Critic、整合Critic、最终审查和导出门禁。至少一次Critic应触发原生产者定向修复，以验证修复分支。
6. 申请书正式版不得包含输入材料中的测试说明、模型模拟提示、未核实成果或个人联系方式。

## 四、验收要求
最终申请书不少于60页，目标80页左右；正式章节和附录结构完整；核心指标口径一致；图、表、文献和Trace均可核验；系统应输出申请书、完整审计包、输入材料包和测试报告。"""
    write(req / "01_科研项目申报指南.md", guide)

    brief = f"""# 项目任务简表

## 项目名称
{PROJECT_NAME}

## 项目定位
本项目面向大型活动保障、园区综合服务和突发保供等复杂服务场景，建设可理解任务、组织知识、调用工具、生成方案、监控执行、处理异常并持续评估的后勤保障智能体。

## 研究周期与预算
项目周期36个月，预算60万元。预算用于算力与软件环境、数据与资料、原型研发、测试验证、专家咨询和成果整理。

## 典型场景
1. 多任务并发的大型活动物资与服务保障。
2. 园区常态化补给、维修、车辆和人员协同。
3. 临时加急、供应变化、资源故障或交通延误下的滚动调整。
4. 面向管理人员的自然语言查询、方案解释与影响分析。

## 总体目标
形成任务语义理解与知识建模、多智能体协同编排、资源调度与低扰动重规划、执行监控与闭环学习等关键能力，完成原型系统、验证环境、评估体系、数据与知识资产以及完整技术文档。

## 初步指标
- 需求解析完整率不低于95%。
- 方案可执行率不低于90%。
- 初始方案生成时间不超过10分钟。
- 异常重规划成功率不低于85%。
- 关键结论Trace覆盖率不低于95%。
- Prompt、响应、工具调用和审批审计留痕完整率为100%。

## 真实信息隔离测试
虚构负责人：陈远航；虚构机构：星舟智能系统研究室；虚构邮箱：chen.yuanhang@example.test；虚构电话：138-0000-2468。上述字段只用于测试在线任务包的确定性脱敏，不得进入任何ONLINE_PUBLIC模型输入。"""
    write(req / "02_项目任务简表.md", brief)

    framework = "# 全文\n以下目录用于驱动完整申请书逐章编制。\n\n" + "\n\n".join(f"# {title}\n请基于已确认输入和公开证据编写本章。" for title in SECTION_TITLES)
    write(req / "03_申请书初始框架.md", framework)

    scenarios = """# 业务场景与流程材料

## 场景一：大型活动保障
活动期间存在多场地并发需求，涉及饮用水、餐饮、清洁、临时设备、车辆和服务人员。任务具有明确时间窗，但需求量可能滚动变化。系统需识别任务优先级、地点关系、资源兼容性和前后置条件，给出可执行计划并解释关键约束。

## 场景二：园区综合服务
园区日常存在周期性补给、设备巡检、维修派单、车辆调度和人员排班。系统需复用历史模式，同时根据实时库存、工单状态和人员可用性调整计划。

## 场景三：临时加急与异常处置
执行中可能出现临时插单、道路拥堵、车辆故障、库存不足、人员缺勤等事件。系统需评估影响范围，优先通过局部替换、时序调整或路线切换恢复计划，并控制对未受影响任务的扰动。

## 关键流程
任务受理→需求澄清→事实与规则检索→任务分解→资源候选生成→约束校核→方案优化→人工确认→执行监控→异常识别→影响分析→重规划→结果评估→经验沉淀。

## 角色与职责
需求方负责确认目标和优先级；计划人员负责方案审核；执行人员反馈状态；系统管理员管理工具和权限；安全审查人员负责公开检索、内容导出和审计检查。"""
    write(req / "04_业务场景与流程材料.md", scenarios)

    design = """# 技术设计输入

## 总体架构设想
系统采用交互层、智能体编排层、知识与数据层、决策执行层、基础设施层和治理审计层的分层架构。各层通过Schema约束的服务接口协同，所有关键动作进入统一事件流和Trace链。

## 智能体角色
- Intake Agent：解析材料、识别申报规则和任务范围。
- Project Knowledge Agent：抽取事实、项目对象和关系。
- Public Research Agent：在脱敏和审批后检索公开资料并生成证据包。
- Planning Agent：形成逐章修订计划和依赖关系。
- Writing Agent：生成段落级蓝图和正式正文。
- Critic Agent：检查来源、数字、术语、映射和格式。
- Scheduling Agent：执行资源匹配、约束求解和动态重规划。
- Gatekeeper：负责公开外发、结果导入、内容终审和导出审批。

## 关键技术输入
1. 任务语义理解：结合领域词表、结构化抽取和澄清问题，将自然语言任务转为任务对象。
2. 知识建模：构建任务、资源、地点、时间窗、规则、指标、方案和事件的知识图谱。
3. RAG与证据追踪：对公开论文、标准、内部规则和历史案例分层检索，生成来源引用和证据链。
4. 多智能体协同：通过状态机、角色契约、Critic与人工Gate组织复杂任务。
5. 优化调度：结合规则过滤、数学规划、启发式搜索和学习策略生成可执行方案。
6. 动态重规划：在状态变化时定位受影响子问题，最小化方案变更成本并快速恢复可行性。
7. 可观测与安全：完整保留Prompt、响应、工具调用、模型路由、Trace和审批记录。

## 图表要求
必须生成业务闭环、系统逻辑结构、研究现状与切入点、目标—内容—技术—成果映射、知识图谱模式、多智能体协同、总体技术路线、动态重规划闭环、部署架构、评估框架和进度里程碑图。"""
    write(req / "05_技术设计输入.md", design)

    data_spec = """# 数据与知识工程输入

## 核心对象
Task、Demand、Resource、Material、Vehicle、Person、Location、TimeWindow、Rule、Metric、Plan、PlanItem、Event、Evidence、Approval和Trace。

## 数据来源
任务通知、资源台账、库存记录、工单、人员班次、车辆状态、地点与路线、业务规则、历史方案、执行日志、用户反馈和公开资料。

## 数据质量规则
核心对象必须具备唯一标识、版本、来源、时间戳和安全标签；数字字段必须保留单位和适用条件；冲突事实不得静默覆盖；公开资料不得作为内部事实直接写入项目定义。

## 检索与记忆
按内部规则、项目事实、历史案例、公开论文和标准分别建立索引；短期工作记忆记录当前任务状态，长期记忆沉淀经人工确认的高质量方案和异常处置经验。"""
    write(req / "06_数据与知识工程输入.md", data_spec)

    optimization = """# 调度模型与算法输入

## 决策对象
任务分配、资源匹配、人员排班、车辆路径、服务顺序和异常替代方案。

## 硬约束
资源容量、技能资格、时间窗、地点可达性、任务前后置关系、互斥规则和安全规则。

## 软目标
最小化延期、运行成本、空驶距离、负载不均衡和方案变更幅度；最大化任务满足率、资源利用率和计划稳定性。

## 求解策略
先通过规则和知识图谱过滤明显不可行候选，再根据问题规模选择MILP/CP-SAT、启发式搜索、局部搜索或学习式策略；所有学习输出必须经过可行性校验器。

## 动态重规划
异常发生后建立影响子图，冻结未受影响任务，针对受影响任务重新生成候选，并以变更成本、恢复时间和方案质量进行多目标评价。"""
    write(req / "07_调度模型与算法输入.md", optimization)

    evaluation = """# 测试与评估输入

## 验证层次
单元测试、组件测试、工作流集成测试、离线场景回放、压力测试、异常注入测试和用户验收测试。

## 核心指标
需求解析完整率、事实引用准确率、方案可执行率、任务满足率、方案生成时间、重规划成功率、重规划时延、变更任务比例、工具调用成功率、人工干预率、Trace覆盖率和审计留痕完整率。

## 场景矩阵
覆盖常态任务、并发高峰、临时插单、库存短缺、车辆故障、人员缺勤、交通延误、工具失败、检索无结果、模型输出Schema错误、公开任务包隐私残留和审查退回等情况。

## 对照实验
比较人工流程、单模型直接生成、无知识增强的多智能体、完整智能体系统四种方案；报告质量、时延、人工成本和稳定性。"""
    write(req / "08_测试与评估输入.md", evaluation)

    foundation = """# 研究基础与组织保障输入

## 团队基础
团队具备自然语言处理、知识图谱、检索增强、工作流编排、优化调度、系统架构、测试工程和项目管理经验，能够覆盖从算法研究到原型实现的完整研发链路。

## 条件基础
已有受控开发环境、代码仓库、文档管理系统和基础算力；可构造虚拟园区和大型活动保障场景数据；具备开展离线回放和用户评审的条件。

## 管理机制
采用需求基线、版本控制、自动化测试、阶段评审、风险台账、数据质量检查和安全审查机制。项目负责人统筹目标与资源，技术负责人管理架构和接口，测试负责人维护验收用例和证据。"""
    write(req / "09_研究基础与组织保障输入.md", foundation)

    budget = """# 经费预算与进度输入

## 经费预算
总预算60万元：设备与软件12万元，数据与资料8万元，研发与测试24万元，场景验证8万元，专家咨询与成果整理5万元，预备费3万元。

## 进度安排
第1—6个月完成需求、架构和数据基线；第7—14个月完成知识底座、RAG和工作流引擎；第15—24个月完成调度、重规划和原型集成；第25—30个月完成场景测试与优化；第31—36个月完成试运行、验收和成果凝练。"""
    write(req / "10_经费预算与进度输入.md", budget)

    public_req = """# 公开研究任务要求

公开研究应覆盖大模型智能体规划与工具调用、多智能体协同、记忆与反思、RAG与GraphRAG、知识图谱、Agent评测与安全、组合优化、车辆路径和排程、人机协同、供应链智能化及AI治理标准。来源优先级为同行评议论文、国际标准、官方框架文档和高质量综述。研究结果只作为公开证据和方法参考，不得反推内部事实。"""
    write(req / "11_公开研究任务要求.md", public_req)

    reference = """# 参考申请书（仅用于结构和风格）

## 结构模式
采用摘要、背景与意义、研究现状、需求、目标、内容、关键问题、关键技术、技术路线、架构、验证、创新、成果、计划、预算、基础、风险和参考文献的论证顺序。

## 风格模式
章节开头说明定位，中间按问题—方法—实施—指标展开，结尾说明输出和章节衔接。技术章节应配图表，关键指标应给出定义和验收方法。

## 禁止迁移
本参考材料不包含可迁移的项目名称、团队成果、预算数字、技术指标或已有系统结论。"""
    write(req / "12_参考申请书.md", reference)

    review = """# 预置评审意见

1. 研究现状不能只列名词，应按智能体、知识增强、优化调度和治理评测等技术分支进行比较。
2. 关键技术应解释技术之间的依赖关系和工程落地方式，避免只给模块清单。
3. 技术路线必须说明各阶段输入、处理、输出和验证闭环。
4. 设计章节必须包含逻辑结构图和关键执行流图。
5. 参考文献必须真实可核验，正文编号应能定位到文末条目。
6. Prompt、日志和Trace应作为正式审计工件导出，而不是只记录调用次数。
7. 至少验证一次Critic退回和定向修复分支。"""
    write(opt / "13_预置评审意见.md", review)

    (ctl / "reference_catalog.json").write_text(json.dumps(REF_CATALOG, ensure_ascii=False, indent=2), encoding="utf-8")
    project = {
        "name": PROJECT_NAME,
        "description": "复杂服务场景后勤保障智能体科研申请书端到端测试",
        "security_level": "INTERNAL",
        "config": {
            "internet_access_allowed": True,
            "anonymized_external_processing_allowed": True,
            "allowed_public_topics": ["智能体系统", "后勤保障", "知识图谱", "RAG", "流程编排", "组合优化", "系统评估", "AI治理"],
            "prohibited_external_fields": ["人员姓名", "组织名称", "详细地址", "联系电话", "电子邮箱"],
            "recipient_scope": ["项目组内部测试人员"],
            "allowed_model_endpoint_ids": ["offline-primary", "online-public-primary"],
            "external_redaction_entities": [
                {"value": "陈远航", "entity_type": "PERSON", "placeholder": "[PERSON_1]", "field_label": "人员姓名"},
                {"value": "星舟智能系统研究室", "entity_type": "ORG", "placeholder": "[ORG_1]", "field_label": "组织名称"},
            ],
            "retention_days": 365,
            "task_instruction": {
                "goal": "生成结构完整、图文并茂、引用可核验的正式科研申请书",
                "must_include": ["研究现状", "关键技术", "技术路线", "逻辑结构图", "关键执行流流程图", "不少于30条参考文献", "Prompt日志Trace审计包"],
                "acceptance": ["不少于60页", "26个Prompt均实际触发", "至少一次定向修复", "关键正文Trace覆盖率不低于95%"],
            },
        },
    }
    (ctl / "project_create.json").write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")

    expected = {
        "formal_sections": len(SECTION_TITLES),
        "distinct_prompt_ids": 26,
        "minimum_references": 30,
        "minimum_figures": 9,
        "minimum_pages": 60,
        "target_pages": 80,
        "required_prompts": [
            "P-SECURITY-CLASSIFY", "P-SECURITY-CLASSIFY-CRITIC", "P-SAFE-ONLINE-PACKAGE", "P-SAFE-ONLINE-PACKAGE-CRITIC",
            "P-PUBLIC-RESEARCH-PLAN", "P-PUBLIC-RESEARCH-SYNTHESIS", "P-PUBLIC-RESEARCH-CRITIC", "P-ONLINE-RESULT-IMPORT-CRITIC",
            "P-SCHEME-EXTRACT", "P-SCHEME-CRITIC", "P-PROJECT-DEFINITION-EXTRACT", "P-PROJECT-DEFINITION-CRITIC",
            "P-PROJECT-READINESS-CRITIC", "P-FACT-EXTRACT", "P-FACT-CRITIC", "P-TEMPLATE-EXTRACT", "P-TEMPLATE-CRITIC",
            "P-REVISION-PLAN", "P-REVISION-PLAN-CRITIC", "P-WRITE-BLUEPRINT", "P-WRITE-BLUEPRINT-CRITIC", "P-WRITE-CONTENT",
            "P-WRITE-CRITIC", "P-INTEGRATION-CRITIC", "P-TARGETED-REPAIR", "P-FINAL-CONFIDENTIALITY-REVIEW"
        ],
        "privacy_values_not_allowed_online": ["陈远航", "星舟智能系统研究室", "chen.yuanhang@example.test", "138-0000-2468"],
    }
    (ctl / "expected_results.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest_rows = [
        ("01_科研项目申报指南.md", "APPLICATION_GUIDE", "INTERNAL", True),
        ("02_项目任务简表.md", "PROJECT_BRIEF", "INTERNAL", True),
        ("03_申请书初始框架.md", "CURRENT_PROPOSAL", "INTERNAL", True),
        ("04_业务场景与流程材料.md", "EVIDENCE_MATERIAL", "INTERNAL", True),
        ("05_技术设计输入.md", "TECHNICAL_DESIGN", "INTERNAL", True),
        ("06_数据与知识工程输入.md", "TECHNICAL_DESIGN", "INTERNAL", True),
        ("07_调度模型与算法输入.md", "TECHNICAL_DESIGN", "INTERNAL", True),
        ("08_测试与评估输入.md", "EVIDENCE_MATERIAL", "INTERNAL", True),
        ("09_研究基础与组织保障输入.md", "TEAM_PROFILE", "INTERNAL", True),
        ("10_经费预算与进度输入.md", "BUDGET_MATERIAL", "INTERNAL", True),
        ("11_公开研究任务要求.md", "EVIDENCE_MATERIAL", "INTERNAL", True),
        ("12_参考申请书.md", "REFERENCE_PROPOSAL", "INTERNAL", True),
        ("13_预置评审意见.md", "REVIEW_COMMENT", "INTERNAL", False),
    ]
    with (ctl / "upload_manifest.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w=csv.writer(f); w.writerow(["filename","role","security_level","required"]); w.writerows(manifest_rows)

    zip_path = output_dir.with_suffix(".zip")
    if zip_path.exists(): zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in output_dir.rglob("*"):
            if path.is_file(): zf.write(path, arcname=path.relative_to(output_dir).as_posix())
    return zip_path


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "logistics_agent_materials_v1"
    print(build(target.resolve()))


if __name__ == "__main__":
    main()
