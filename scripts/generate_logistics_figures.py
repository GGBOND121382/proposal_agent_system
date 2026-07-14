#!/usr/bin/env python3
from __future__ import annotations

import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

FONT_PATHS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
FONT_PATH = next((p for p in FONT_PATHS if Path(p).exists()), None)


def font(size: int, bold: bool = False):
    if FONT_PATH:
        return ImageFont.truetype(FONT_PATH, size)
    return ImageFont.load_default()

TITLE = font(40)
SUB = font(27)
BODY = font(22)
SMALL = font(18)
W, H = 1800, 1150
BG = (250, 252, 255)
BLUE = (50, 104, 168)
BLUE_FILL = (230, 241, 252)
GREEN = (66, 135, 94)
GREEN_FILL = (232, 247, 237)
ORANGE = (184, 112, 42)
ORANGE_FILL = (255, 243, 226)
PURPLE = (120, 79, 161)
PURPLE_FILL = (243, 235, 252)
RED = (164, 71, 79)
RED_FILL = (253, 234, 237)
GRAY = (90, 98, 108)
GRAY_FILL = (243, 245, 247)


def canvas(title: str):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    bb = d.textbbox((0, 0), title, font=TITLE)
    d.text(((W - (bb[2]-bb[0]))/2, 35), title, font=TITLE, fill=(20, 32, 48))
    return im, d


def wrap(draw, text: str, fnt, maxw: int):
    lines=[]
    for para in text.split("\n"):
        cur=""
        for ch in para:
            test=cur+ch
            if draw.textbbox((0,0), test, font=fnt)[2] > maxw and cur:
                lines.append(cur); cur=ch
            else:
                cur=test
        lines.append(cur)
    return lines


def box(draw, xy, text, *, fill=BLUE_FILL, outline=BLUE, fnt=BODY, radius=20, width=3):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)
    x1,y1,x2,y2=xy
    lines=wrap(draw,text,fnt,x2-x1-28)
    heights=[draw.textbbox((0,0),l,font=fnt)[3] for l in lines]
    total=sum(heights)+8*(len(lines)-1)
    y=y1+(y2-y1-total)/2
    for l,h in zip(lines,heights):
        bb=draw.textbbox((0,0),l,font=fnt)
        draw.text((x1+(x2-x1-(bb[2]-bb[0]))/2,y),l,font=fnt,fill=(20,25,32))
        y+=h+8


def arrow(draw, p1, p2, color=GRAY, width=4, dashed=False):
    if dashed:
        steps=20
        for i in range(0,steps,2):
            a=(p1[0]+(p2[0]-p1[0])*i/steps,p1[1]+(p2[1]-p1[1])*i/steps)
            b=(p1[0]+(p2[0]-p1[0])*(i+1)/steps,p1[1]+(p2[1]-p1[1])*(i+1)/steps)
            draw.line([a,b],fill=color,width=width)
    else:
        draw.line([p1,p2],fill=color,width=width)
    ang=math.atan2(p2[1]-p1[1],p2[0]-p1[0])
    for s in (-1,1):
        a=ang+s*0.55
        q=(p2[0]-20*math.cos(a),p2[1]-20*math.sin(a))
        draw.line([p2,q],fill=color,width=width)


def save(im, name):
    path=OUT/name
    im.save(path, quality=95)
    return path


def fig_business_loop():
    im,d=canvas("后勤保障智能体业务闭环")
    nodes=[
        ("任务受理",(730,130,1070,230),BLUE_FILL,BLUE),
        ("需求解析",(1240,290,1580,390),GREEN_FILL,GREEN),
        ("方案生成",(1240,600,1580,700),ORANGE_FILL,ORANGE),
        ("执行监控",(730,800,1070,900),PURPLE_FILL,PURPLE),
        ("异常处置",(220,600,560,700),RED_FILL,RED),
        ("效果评估",(220,290,560,390),GRAY_FILL,GRAY),
    ]
    for t,xy,fi,ou in nodes: box(d,xy,t,fill=fi,outline=ou,fnt=SUB)
    centers=[((900,230),(1240,340)),((1410,390),(1410,600)),((1240,650),(1070,850)),((730,850),(560,650)),((390,600),(390,390)),((560,340),(730,180))]
    for a,b in centers: arrow(d,a,b)
    box(d,(650,430,1150,590),"知识图谱 + RAG\n多智能体编排 + 调度优化\n人工门禁 + Trace审计",fill=(255,255,255),outline=(45,60,80),fnt=BODY)
    return save(im,"图1_业务闭环.png")


def fig_logic():
    im,d=canvas("后勤保障智能体逻辑结构")
    layers=[
        ("交互与决策层","任务受理｜自然语言交互｜人工审批｜态势看板",160,BLUE_FILL,BLUE),
        ("智能体编排层","Planner｜Researcher｜Writer｜Critic｜Executor｜Gatekeeper",330,GREEN_FILL,GREEN),
        ("知识与数据层","项目知识图谱｜RAG检索｜规则库｜案例库｜短期/长期记忆",500,PURPLE_FILL,PURPLE),
        ("优化与工具层","资源匹配｜路径规划｜时刻与批次排程｜低扰动重规划｜外部工具",670,ORANGE_FILL,ORANGE),
        ("治理与运行层","身份权限｜安全分级｜Prompt/响应/Trace留存｜指标评估｜版本管理",840,RED_FILL,RED),
    ]
    for name,desc,y,fi,ou in layers:
        box(d,(120,y,460,y+105),name,fill=fi,outline=ou,fnt=SUB)
        box(d,(520,y,1680,y+105),desc,fill=(255,255,255),outline=ou,fnt=BODY)
        if y<840: arrow(d,(900,y+105),(900,y+160),color=ou)
    return save(im,"图2_逻辑结构.png")


def fig_research_landscape():
    im,d=canvas("研究现状与项目切入点")
    areas=[
        ("LLM智能体\n推理、规划、工具调用",(110,170,510,340),BLUE_FILL,BLUE),
        ("多智能体协同\n角色分工、辩论、反思",(700,170,1100,340),GREEN_FILL,GREEN),
        ("RAG与知识图谱\n证据增强、全局归纳",(1290,170,1690,340),PURPLE_FILL,PURPLE),
        ("运筹与调度\n路径、排程、组合优化",(310,650,710,820),ORANGE_FILL,ORANGE),
        ("数字孪生与控制塔\n状态映射、仿真、监控",(1090,650,1490,820),RED_FILL,RED),
    ]
    for t,xy,fi,ou in areas: box(d,xy,t,fill=fi,outline=ou,fnt=BODY)
    box(d,(600,410,1200,590),"本项目切入点\n知识增强的多智能体协同\n+ 多约束调度与低扰动重规划\n+ 全过程可观测与人工可控",fill=(255,255,255),outline=(20,40,70),fnt=SUB)
    for xy in [areas[0][1],areas[1][1],areas[2][1],areas[3][1],areas[4][1]]:
        cx=(xy[0]+xy[2])//2; cy=(xy[1]+xy[3])//2
        target=(900,500)
        arrow(d,(cx,cy),target,dashed=True)
    return save(im,"图3_研究现状与切入点.png")


def fig_goal_mapping():
    im,d=canvas("目标—内容—技术—成果映射")
    cols=[("研究目标",100,BLUE_FILL,BLUE),("研究内容",500,GREEN_FILL,GREEN),("关键技术",900,PURPLE_FILL,PURPLE),("预期成果",1300,ORANGE_FILL,ORANGE)]
    for title,x,fi,ou in cols: box(d,(x,140,x+300,220),title,fill=fi,outline=ou,fnt=SUB)
    rows=[
        ("统一任务与知识模型","业务关系建模\n指标与规则体系","语义抽取\n知识图谱/GraphRAG","知识模型\n数据与规则资产"),
        ("形成智能体协同框架","任务分解\n公开研究\n方案生成与审查","角色编排\n工具调用\n反思与门禁","多智能体引擎\nPrompt与Trace规范"),
        ("实现动态调度闭环","资源匹配\n执行监控\n异常重规划","组合优化\n强化学习\n低扰动更新","调度与重规划模块\n场景验证报告"),
    ]
    ys=[300,570,840]
    for row,y in zip(rows,ys):
        for i,(txt,(title,x,fi,ou)) in enumerate(zip(row,cols)):
            box(d,(x,y,x+300,y+150),txt,fill=(255,255,255),outline=ou,fnt=BODY)
            if i<3: arrow(d,(x+300,y+75),(cols[i+1][1],y+75))
    return save(im,"图4_目标内容技术成果映射.png")


def fig_knowledge_graph():
    im,d=canvas("任务—资源—规则—指标知识图谱模式")
    center=(900,550)
    box(d,(700,465,1100,635),"保障任务\nTask",fill=BLUE_FILL,outline=BLUE,fnt=SUB)
    nodes=[
        ("需求项\nDemand",(150,170,470,300),GREEN_FILL,GREEN),
        ("物资\nMaterial",(740,130,1060,260),ORANGE_FILL,ORANGE),
        ("人员/班组\nTeam",(1330,170,1650,300),PURPLE_FILL,PURPLE),
        ("地点/节点\nLocation",(130,760,450,890),RED_FILL,RED),
        ("车辆/设备\nResource",(740,850,1060,980),GREEN_FILL,GREEN),
        ("规则/约束\nRule",(1350,760,1670,890),GRAY_FILL,GRAY),
        ("指标/事件\nMetric/Event",(1300,465,1650,635),BLUE_FILL,BLUE),
        ("方案/批次\nPlan/Batch",(150,465,500,635),ORANGE_FILL,ORANGE),
    ]
    for t,xy,fi,ou in nodes: box(d,xy,t,fill=fi,outline=ou,fnt=BODY)
    for _,xy,_,ou in nodes:
        c=((xy[0]+xy[2])//2,(xy[1]+xy[3])//2)
        arrow(d,c,center,color=ou,dashed=True)
    return save(im,"图5_知识图谱模式.png")


def fig_agents():
    im,d=canvas("多智能体角色与协同机制")
    roles=[
        ("Planner\n任务分解与流程编排",(100,190,430,330),BLUE_FILL,BLUE),
        ("Researcher\n公开检索与证据综合",(560,160,910,300),GREEN_FILL,GREEN),
        ("Writer\n蓝图与正文生成",(1030,160,1380,300),ORANGE_FILL,ORANGE),
        ("Critic\n规则、一致性与质量审查",(1370,440,1710,590),PURPLE_FILL,PURPLE),
        ("Executor\n调度求解与工具调用",(1030,760,1380,910),RED_FILL,RED),
        ("Gatekeeper\n安全分类与人工门禁",(560,790,910,940),GRAY_FILL,GRAY),
        ("Memory/Trace\n事实、版本、运行轨迹",(100,600,430,750),GREEN_FILL,GREEN),
    ]
    for t,xy,fi,ou in roles: box(d,xy,t,fill=fi,outline=ou,fnt=BODY)
    path=[((430,260),(560,230)),((910,230),(1030,230)),((1380,230),(1540,440)),((1370,515),(1205,760)),((1030,835),(910,865)),((560,865),(430,675)),((265,600),(265,330))]
    for a,b in path: arrow(d,a,b)
    box(d,(630,450,1170,620),"共享状态与契约\nSchema｜对象版本｜来源引用\n问题清单｜Gate决策｜指标反馈",fill=(255,255,255),outline=(30,45,60),fnt=BODY)
    for xy in [roles[0][1],roles[1][1],roles[2][1],roles[3][1],roles[4][1],roles[5][1],roles[6][1]]:
        c=((xy[0]+xy[2])//2,(xy[1]+xy[3])//2)
        arrow(d,c,(900,535),dashed=True)
    return save(im,"图6_多智能体协同.png")


def fig_route():
    im,d=canvas("项目总体技术路线")
    phases=[
        ("阶段1\n需求、指标与场景建模",(80,230,390,390),BLUE_FILL,BLUE),
        ("阶段2\n数据治理与知识底座",(430,230,740,390),GREEN_FILL,GREEN),
        ("阶段3\n多智能体编排与工具接入",(780,230,1090,390),PURPLE_FILL,PURPLE),
        ("阶段4\n调度优化与动态重规划",(1130,230,1440,390),ORANGE_FILL,ORANGE),
        ("阶段5\n集成验证与迭代优化",(1480,230,1750,390),RED_FILL,RED),
    ]
    for t,xy,fi,ou in phases: box(d,xy,t,fill=fi,outline=ou,fnt=BODY)
    for a,b in zip(phases[:-1],phases[1:]): arrow(d,(a[1][2],310),(b[1][0],310))
    outputs=[
        ("需求基线\n场景集\n指标体系",(90,560,380,740)),
        ("知识图谱\n事实库\n检索索引",(440,560,730,740)),
        ("工作流引擎\n角色契约\nPrompt/Trace",(790,560,1080,740)),
        ("资源匹配\n路径排程\n重规划策略",(1140,560,1430,740)),
        ("原型系统\n评测报告\n应用建议",(1490,560,1740,740)),
    ]
    for (t,xy),(_,pxy,_,ou) in zip(outputs,phases):
        box(d,xy,t,fill=(255,255,255),outline=ou,fnt=BODY)
        arrow(d,((pxy[0]+pxy[2])//2,pxy[3]),((xy[0]+xy[2])//2,xy[1]),color=ou)
    box(d,(390,900,1410,1015),"贯穿机制：来源可信度｜安全分级｜人工门禁｜版本控制｜自动化测试｜场景回放｜持续评估",fill=GRAY_FILL,outline=GRAY,fnt=SUB)
    return save(im,"图7_总体技术路线.png")


def fig_replanning():
    im,d=canvas("动态调度与低扰动重规划闭环")
    nodes=[
        ("当前任务与资源状态",(650,120,1150,220),BLUE_FILL,BLUE),
        ("约束检查与冲突检测",(1260,330,1690,430),RED_FILL,RED),
        ("影响范围定位",(1260,650,1690,750),ORANGE_FILL,ORANGE),
        ("局部候选生成",(650,860,1150,960),GREEN_FILL,GREEN),
        ("多目标评价与选择",(110,650,540,750),PURPLE_FILL,PURPLE),
        ("人工确认/自动发布",(110,330,540,430),GRAY_FILL,GRAY),
    ]
    for t,xy,fi,ou in nodes: box(d,xy,t,fill=fi,outline=ou,fnt=BODY)
    centers=[((1150,170),(1260,380)),((1475,430),(1475,650)),((1260,700),(1150,910)),((650,910),(540,700)),((325,650),(325,430)),((540,380),(650,170))]
    for a,b in centers: arrow(d,a,b)
    box(d,(650,450,1150,650),"优化目标\n时效｜成本｜满足率｜稳定性\n变更范围｜风险｜可解释性",fill=(255,255,255),outline=(30,45,60),fnt=BODY)
    return save(im,"图8_动态重规划闭环.png")


def fig_deployment():
    im,d=canvas("后勤保障智能体部署与系统集成架构")
    zones=[
        ("用户与业务系统",(70,150,430,950),BLUE_FILL,BLUE),
        ("智能体应用服务",(500,150,920,950),GREEN_FILL,GREEN),
        ("模型与工具服务",(990,150,1350,950),PURPLE_FILL,PURPLE),
        ("数据与治理基础设施",(1420,150,1750,950),ORANGE_FILL,ORANGE),
    ]
    for t,xy,fi,ou in zones:
        d.rounded_rectangle(xy,radius=25,fill=fi,outline=ou,width=4)
        bb=d.textbbox((0,0),t,font=SUB); d.text((xy[0]+(xy[2]-xy[0]-(bb[2]-bb[0]))/2,xy[1]+25),t,font=SUB,fill=(20,25,32))
    items=[
        ("任务受理门户\n人工审批台\n态势与评估看板",(110,300,390,600),BLUE),
        ("API网关\n工作流引擎\n上下文构建\nPrompt执行\nTrace服务",(550,270,870,650),GREEN),
        ("离线/专有模型\n公开研究模型\n优化求解器\n地图/库存/日历工具",(1030,270,1310,680),PURPLE),
        ("项目数据库\n向量库\n知识图谱\n对象存储\n日志/监控/密钥",(1460,270,1710,700),ORANGE),
    ]
    for t,xy,ou in items: box(d,xy,t,fill=(255,255,255),outline=ou,fnt=BODY)
    arrow(d,(390,450),(550,450)); arrow(d,(870,450),(1030,450)); arrow(d,(1310,450),(1460,450))
    arrow(d,(1460,760),(870,760),dashed=True); arrow(d,(870,760),(390,760),dashed=True)
    d.text((650,995),"部署原则：业务隔离、最小权限、可替换模型、可审计调用、数据不越界",font=SUB,fill=(20,32,48))
    return save(im,"图9_部署架构.png")


def fig_evaluation():
    im,d=canvas("评估指标与分层验证框架")
    layers=[
        ("L1 单元级", "抽取准确率｜Schema合法率｜工具调用正确率｜检索召回率", 170, BLUE_FILL, BLUE),
        ("L2 智能体级", "任务完成率｜反思修复率｜角色协同开销｜Trace覆盖率", 350, GREEN_FILL, GREEN),
        ("L3 工作流级", "端到端时延｜人工门禁等待｜错误恢复率｜版本一致性", 530, PURPLE_FILL, PURPLE),
        ("L4 业务场景级", "方案可执行率｜资源满足率｜成本与时效｜异常重规划质量", 710, ORANGE_FILL, ORANGE),
        ("L5 试运行级", "用户接纳度｜稳定运行时间｜审计完备性｜持续改进效果", 890, RED_FILL, RED),
    ]
    for name,desc,y,fi,ou in layers:
        box(d,(150,y,500,y+105),name,fill=fi,outline=ou,fnt=SUB)
        box(d,(570,y,1650,y+105),desc,fill=(255,255,255),outline=ou,fnt=BODY)
        if y<890: arrow(d,(900,y+105),(900,y+175),color=ou)
    return save(im,"图10_评估框架.png")


def fig_milestones():
    im,d=canvas("三年实施进度与里程碑")
    quarters=[f"Q{i}" for i in range(1,13)]
    x0=250; cw=115
    for i,q in enumerate(quarters):
        x=x0+i*cw
        d.text((x+35,135),q,font=SMALL,fill=(20,32,48))
        d.line([(x,175),(x,1000)],fill=(210,215,220),width=2)
    d.line([(x0+12*cw,175),(x0+12*cw,1000)],fill=(210,215,220),width=2)
    tracks=[
        ("需求与知识建模",1,3,BLUE_FILL,BLUE),
        ("RAG与数据治理",2,5,GREEN_FILL,GREEN),
        ("多智能体编排",3,7,PURPLE_FILL,PURPLE),
        ("调度与重规划",5,9,ORANGE_FILL,ORANGE),
        ("系统集成与场景验证",7,11,RED_FILL,RED),
        ("试运行与成果凝练",10,12,GRAY_FILL,GRAY),
    ]
    y=230
    for name,s,e,fi,ou in tracks:
        d.text((40,y+24),name,font=BODY,fill=(20,25,32))
        box(d,(x0+(s-1)*cw,y,x0+e*cw-15,y+80),f"{name}\nM{s}-M{e}",fill=fi,outline=ou,fnt=SMALL,radius=12)
        y+=125
    return save(im,"图11_进度里程碑.png")


def main():
    funcs=[fig_business_loop,fig_logic,fig_research_landscape,fig_goal_mapping,fig_knowledge_graph,fig_agents,fig_route,fig_replanning,fig_deployment,fig_evaluation,fig_milestones]
    paths=[f() for f in funcs]
    print("\n".join(str(p) for p in paths))


if __name__ == "__main__":
    main()
