from __future__ import annotations
import argparse, copy, csv, hashlib, json, math, os, re, sys, zipfile
from pathlib import Path
from typing import Any
from jsonschema import Draft202012Validator
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from app.util import sha256_json, utc_now
STAGE="STAGE_7_FULL_INTEGRATION"; MODEL_ID="gpt-5.6-thinking"; ENDPOINT_ID="chatgpt-conversation-file-bridge"
SECTION_IDS=[f"SEC-{i:02d}" for i in range(1,15)]
ID_RE=re.compile(r"(?:CP|RQ|OBJ|WP|RC|M|FM|GAP|PRIOR|INNO-H|MECH|BL|EXP|MET|BOUND|FOUND|RISK|OPEN)-\d+")
SRC_RE=re.compile(r"SRC-(?:PUB|STD|UA|TRACE)-\d+")
ENGINEERING_TERMS=("Prompt","Gate","Schema","API","JSON","Trace","哈希","回归测试","文件桥","模型响应")

def read_json(p:Path)->Any:return json.loads(p.read_text(encoding='utf-8'))
def atomic_json(p:Path,v:Any)->None:
 p.parent.mkdir(parents=True,exist_ok=True); t=p.with_name(p.name+f'.tmp-{os.getpid()}'); t.write_text(json.dumps(v,ensure_ascii=False,indent=2,sort_keys=True)+'\n',encoding='utf-8'); os.replace(t,p)
def atomic_text(p:Path,s:str)->None:
 p.parent.mkdir(parents=True,exist_ok=True); t=p.with_name(p.name+f'.tmp-{os.getpid()}'); t.write_text(s,encoding='utf-8'); os.replace(t,p)
def sha256_text(s:str)->str:return hashlib.sha256(s.encode()).hexdigest()
def sha256_file(p:Path)->str:
 h=hashlib.sha256();
 with p.open('rb') as f:
  for c in iter(lambda:f.read(1048576),b''):h.update(c)
 return h.hexdigest()
def load_schema(n:str)->dict:return read_json(ROOT/'stage7_tools'/n)
def validate_schema(v:Any,n:str)->list[str]:
 return [f"{'/'.join(map(str,e.path)) or '$'}: {e.message}" for e in sorted(Draft202012Validator(load_schema(n)).iter_errors(v),key=lambda e:list(e.path))]
def paragraphs(c:dict)->list[dict]:return [p for s in c.get('subsections',[]) for p in s.get('paragraphs',[])]
def canonical_markdown(c:dict)->str:
 out=[f"# {c['section_name']}",""]
 for sub in c['subsections']:
  out += [f"## {sub['title']}",""]
  pids={p['paragraph_id'] for p in sub['paragraphs']}
  for p in sub['paragraphs']:
   out += [p['text'].strip(),""]
   for v in c.get('visual_placeholders',[]):
    if v['placement_after_paragraph_id']==p['paragraph_id'] and p['paragraph_id'] in pids:out += [f"> **{v['visual_id']}：{v['caption']}**",""]
 return '\n'.join(out).rstrip()+'\n'
def append_event(rd:Path,typ:str,**kw):
 p=rd/'events.jsonl'; idx=1+(sum(1 for x in p.read_text(encoding='utf-8').splitlines() if x.strip()) if p.exists() else 0)
 with p.open('a',encoding='utf-8') as f:f.write(json.dumps({'index':idx,'recorded_at':utc_now(),'event_type':typ,**kw},ensure_ascii=False,sort_keys=True)+'\n')
def set_state(rd:Path,status:str,phase:str,**kw):atomic_json(rd/'LATEST_STATE.json',{'schema_version':'1.0','stage':STAGE,'status':status,'phase':phase,'updated_at':utc_now(),**kw});append_event(rd,'STATE_CHANGED',status=status,phase=phase,details=kw)
def all_sections(rd:Path,repaired:bool=False)->dict[str,dict]:
 if repaired:
  p=rd/'intermediate'/'repaired_sections.json'
  if p.exists():return {x['section_id']:x['candidate'] for x in read_json(p)['sections']}
 out={}
 for fn in ['stage6a_batch_draft.json','stage6b_batch_draft.json','stage6c_batch_draft.json','stage6d_batch_draft.json']:
  d=read_json(rd/'source_snapshots'/fn)
  for x in d['sections']:out[x['section_id']]=x['candidate']
 return out
def citations_in(text:str)->set[int]:
 nums=set()
 for grp in re.findall(r'\[([0-9,\-]+)\]',text):
  for part in grp.split(','):
   if '-' in part:
    a,b=map(int,part.split('-',1));nums.update(range(a,b+1))
   elif part.isdigit():nums.add(int(part))
 return nums
def deterministic_report(rd:Path,sections:dict[str,dict],require_public_citations:bool)->dict:
 f=[]
 def add(code,sections_,msg):f.append({'code':code,'section_ids':sorted(set(sections_)),'message':msg})
 if list(sorted(sections))!=SECTION_IDS:add('CANDIDATE_SET_INCOMPLETE',sections.keys(),'14章集合不完整。')
 texts={};keys={};internal={};sources={};eng={};citation_missing={}; chars=0
 citation_map={x['source_id']:i+1 for i,x in enumerate([s for s in read_json(rd/'source_snapshots'/'stage4a_evidence_completion.json')['source_registry'] if s['source_id'].startswith(('SRC-PUB','SRC-STD'))])}
 for sid,c in sections.items():
  heading_values=[c.get('section_name','')]+[x.get('title','') for x in c.get('subsections',[])]+[x.get('caption','') for x in c.get('visual_placeholders',[])]
  heading_internal=sum(len(ID_RE.findall(x)) for x in heading_values)
  heading_sources=sum(len(SRC_RE.findall(x)) for x in heading_values)
  heading_eng=sum(sum(x.count(term) for term in ENGINEERING_TERMS) for x in heading_values)
  if heading_internal:internal[sid]=internal.get(sid,0)+heading_internal
  if heading_sources:sources[sid]=sources.get(sid,0)+heading_sources
  if heading_eng:eng[sid]=eng.get(sid,0)+heading_eng
  for p in paragraphs(c):
   text=re.sub(r'\s+','',p['text']);chars+=len(text)
   if text in texts:add('EXACT_PARAGRAPH_DUPLICATE',[sid,texts[text]],'存在完全重复段落。')
   texts[text]=sid; k=p['novel_content_key']
   if k in keys:add('DUPLICATE_INFORMATION_KEY',[sid,keys[k]],f'信息键{k}重复。')
   keys[k]=sid
   ni=len(ID_RE.findall(p['text'])); ns=len(SRC_RE.findall(p['text'])); ne=sum(p['text'].count(t) for t in ENGINEERING_TERMS)
   if ni:internal[sid]=internal.get(sid,0)+ni
   if ns:sources[sid]=sources.get(sid,0)+ns
   if ne:eng[sid]=eng.get(sid,0)+ne
   if any(x in p['text'] for x in ['国际首创','国内首创','国际领先','填补空白','TODO','[中段省略]','……']):add('FORBIDDEN_OR_PLACEHOLDER_TEXT',[sid],'存在占位或不受支持表述。')
   pubs=[x for x in p.get('source_ids',[]) if x in citation_map]
   if require_public_citations and pubs:
    visible=citations_in(p['text']); expected={citation_map[x] for x in pubs}
    if not expected.issubset(visible):citation_missing.setdefault(sid,[]).append(p['paragraph_id'])
 if internal:add('INTERNAL_IDENTIFIER_LEAKAGE',internal.keys(),f'正文暴露内部ID共{sum(internal.values())}处。')
 if sources:add('SOURCE_IDENTIFIER_LEAKAGE',sources.keys(),f'正文暴露来源ID共{sum(sources.values())}处。')
 if eng:add('DOCUMENT_TYPE_DRIFT',eng.keys(),f'正文含工程过程标记共{sum(eng.values())}处。')
 if citation_missing:add('VISIBLE_CITATION_INCOMPLETE',citation_missing.keys(),'公开研究段落未完整显示编号引文。')
 awkward_patterns={
  '对应目标研究目标':'目标编号替换后发生重复', '全过程全过程':'同一术语连续重复',
  '研究内容共享决策':'研究内容编号与标题粘连', '研究内容候选竞争':'研究内容编号与标题粘连',
  '研究内容人工关键':'研究内容编号与标题粘连', '研究内容全过程':'研究内容编号与标题粘连',
  '结构化输入输出结构化对象规范':'结构化术语重复', '的角色分离保持不变':'主谓成分重复',
  '版本依赖与增量失效传播机制的版本依赖':'机制名称与说明重复',
  '正式团队信息缺口团队名单':'开放事项标签与正文粘连',
  '研究基础证明材料缺口研究基础证据材料':'开放事项标签与正文粘连',
  '全过程证据记录证据':'术语与宾语重复'
 }
 awkward={}
 for sid,c in sections.items():
  for p in paragraphs(c):
   hits=[msg for pat,msg in awkward_patterns.items() if pat in p['text']]
   if hits:awkward.setdefault(sid,[]).append({'paragraph_id':p['paragraph_id'],'issues':hits})
 if awkward:add('POST_REPAIR_EXPRESSION_ARTIFACTS',awkward.keys(),f'存在{sum(len(v) for v in awkward.values())}个批量编辑遗留句式。')
 stage5=read_json(rd/'source_snapshots'/'stage5_section_plan.json'); target=sum(float(x['target_pages']) for x in stage5['sections']); maxp=sum(float(x['max_pages']) for x in stage5['sections']); est=round(target,1)
 if maxp>20.0001:add('PAGE_BUDGET_EXCEEDED',SECTION_IDS,f'最大预算{maxp}超过20页。')
 # required whole-document metadata closures
 node_union={n for c in sections.values() for p in paragraphs(c) for n in p.get('node_ids',[])}; rq_union={n for c in sections.values() for p in paragraphs(c) for n in p.get('rq_ids',[])}
 for n in ['CP-1','INNO-H1','INNO-H2','INNO-H3','OBJ-1','OBJ-2','OBJ-3','OBJ-4']:
  if n not in node_union:add('WHOLE_ARGUMENT_NODE_MISSING',['SEC-14'],f'全文未覆盖{n}。')
 for n in ['RQ-1','RQ-2','RQ-3']:
  if n not in rq_union:add('WHOLE_RESEARCH_QUESTION_MISSING',['SEC-05','SEC-14'],f'全文未覆盖{n}。')
 return {'verdict':'PASS' if not f else 'REVISE','section_count':len(sections),'total_effective_char_count':chars,'target_pages':target,'max_pages':maxp,'estimated_pages':est,'internal_identifier_occurrences':sum(internal.values()),'source_identifier_occurrences':sum(sources.values()),'engineering_term_occurrences':sum(eng.values()),'visible_citation_count':sum(len(citations_in(p['text'])) for c in sections.values() for p in paragraphs(c)),'findings':f,'candidate_set_hash':sha256_json({sid:sha256_json(c) for sid,c in sorted(sections.items())})}
def write_request(rd:Path,num:int,name:str,payload:dict):
 p=rd/'requests'/f'{num:03d}_{name}.json';atomic_json(p,payload);append_event(rd,'MODEL_REQUEST_CREATED',request_file=str(p.relative_to(rd)),prompt_id=payload['prompt_id']);return p
def critic_request(rd:Path,round_no:int,sections:dict)->dict:
 report=deterministic_report(rd,sections,require_public_citations=round_no>1);atomic_json(rd/'quality'/f'deterministic_round_{round_no}.json',report)
 return {'schema_version':'1.0','call_key':f'stage7-integration-critic-r{round_no}','prompt_id':'P-STAGE7-FULL-INTEGRATION-CRITIC','prompt_version':'1.0.0','executor_role':'Whole-proposal Scientific Merit Critic','model_contract':{'independent_from_section_writers':True,'response_format':'JSON','actual_model_id_required':True,'endpoint_id_required':True},'system_prompt':'审查14章申请书全文。必须检查中心命题、六类论证链、证据状态、创新比较、研究基础、指标依据、章节独特性、文种、编号引文和20页预算。内部图谱ID、来源ID及工作流标记不得出现在面向评审人的正文。只提出最小可执行修改。','task_prompt':f'执行第{round_no}轮全文集成审查；逐章读取，不得抽样。','input_envelope':{'project_definition':read_json(rd/'source_snapshots'/'stage3_project_definition.json'),'argument_architecture':read_json(rd/'source_snapshots'/'stage4_argument_architecture.json'),'evidence_completion':read_json(rd/'source_snapshots'/'stage4a_evidence_completion.json'),'section_plan':read_json(rd/'source_snapshots'/'stage5_section_plan.json'),'candidate_sections':[{'section_id':sid,'candidate_hash':sha256_json(c),'candidate':c} for sid,c in sorted(sections.items())],'deterministic_report':report,'stage_boundary':'FULL_INTEGRATION_ONLY_NO_EXPORT'},'output_schema':load_schema('integration_critic.schema.json'),'requested_at':utc_now()}
def repair_request(rd:Path,critic:dict,repair_round:int)->dict:
 sections=all_sections(rd,repaired=repair_round>1); srcs=[x for x in read_json(rd/'source_snapshots'/'stage4a_evidence_completion.json')['source_registry'] if x['source_id'].startswith(('SRC-PUB','SRC-STD'))]
 return {'schema_version':'1.0','call_key':f'stage7-targeted-document-edit-r{repair_round}','prompt_id':'P-STAGE7-TARGETED-DOCUMENT-EDIT','prompt_version':'1.0.0','executor_role':'Proposal Expression Editor','model_contract':{'semantic_identity_immutable':True,'paragraph_metadata_immutable':True,'response_format':'JSON','actual_model_id_required':True,'endpoint_id_required':True},'system_prompt':'只把内部ID和工程工作标记转换为自然、正式的申报语言，并按给定编号补充公开来源引文。必要时可通过heading_edits规范化子节标题，通过visual_caption_edits规范化图表标题，但不得增删或移动子节、图表。不得改变段落数量、章节结构、事实状态、节点/来源/RQ绑定、信息键、图表位置、开放事项或研究结论。','task_prompt':f'执行第{repair_round}轮定向编辑。针对全文Critic指出的章节逐段编辑；未受影响段落不得列入edits。','input_envelope':{'findings':critic['findings'],'repair_round':repair_round,'sections':[{'section_id':sid,'section_name':c['section_name'],'subsection_titles':[{'subsection_id':sub['subsection_id'],'title':sub['title']} for sub in c.get('subsections',[])],'visual_placeholders':c.get('visual_placeholders',[]),'paragraphs':paragraphs(c)} for sid,c in sorted(sections.items())],'citation_registry':[{'source_id':x['source_id'],'citation_number':i+1,'title':x['title'],'authors':x['authors'],'year':x['year'],'venue':x['venue']} for i,x in enumerate(srcs)]},'output_schema':load_schema('document_repair.schema.json'),'requested_at':utc_now()}
def init_cmd(a):
 rd=Path(a.run_dir).resolve();
 if rd.exists():raise SystemExit('run dir exists')
 for d in ['source_snapshots','requests','responses','quality','intermediate','outputs','human_gate']: (rd/d).mkdir(parents=True,exist_ok=True)
 files={'stage3_project_definition.json':a.project_definition,'stage4_argument_architecture.json':a.argument_architecture,'stage4a_evidence_completion.json':a.evidence_completion,'stage5_section_plan.json':a.section_plan,'stage6a_batch_draft.json':a.stage6a,'stage6b_batch_draft.json':a.stage6b,'stage6c_batch_draft.json':a.stage6c,'stage6d_batch_draft.json':a.stage6d}
 for name,src in files.items():atomic_json(rd/'source_snapshots'/name,read_json(Path(src).resolve()))
 sections=all_sections(rd);req=critic_request(rd,1,sections);write_request(rd,1,'full_integration_critic_round1',req);set_state(rd,'WAITING_MODEL','INTEGRATION_CRITIC_ROUND_1',candidate_set_hash=req['input_envelope']['deterministic_report']['candidate_set_hash']);print(rd/'requests'/'001_full_integration_critic_round1.json')
def ingest_critic_cmd(a):
 rd=Path(a.run_dir).resolve();resp=read_json(Path(a.response_file).resolve());errs=validate_schema(resp,'integration_critic.schema.json')
 if errs:raise SystemExit('; '.join(errs))
 if set(resp['checked_section_ids'])!=set(SECTION_IDS):raise SystemExit('critic did not check all sections')
 critic_round=1+len(list((rd/'responses').glob('*full_integration_critic_round*.json')))
 num=2*critic_round-1
 atomic_json(rd/'responses'/f'{num:03d}_full_integration_critic_round{critic_round}.json',resp)
 append_event(rd,'MODEL_RESPONSE_INGESTED',response_file=f'responses/{num:03d}_full_integration_critic_round{critic_round}.json',actual_model_id=resp['actual_model_id'],endpoint_id=resp['endpoint_id'])
 if resp['verdict']=='REVISE' and resp['next_action']=='REQUEST_TARGETED_EXPRESSION_REPAIR' and critic_round<=2:
  req=repair_request(rd,resp,critic_round);write_request(rd,num+1,f'targeted_document_edit_round{critic_round}',req)
  set_state(rd,'WAITING_MODEL',f'TARGETED_DOCUMENT_EDIT_ROUND_{critic_round}',finding_codes=[x['code'] for x in resp['findings']])
  return
 sections=all_sections(rd,True);report=deterministic_report(rd,sections,True);atomic_json(rd/'quality'/'deterministic_final.json',report)
 if report['verdict']!='PASS':raise SystemExit(f"final deterministic failed: {report['findings']}")
 if resp['verdict']!='ACCEPT' or resp['next_action']!='ALLOW_STAGE_8' or any(x['result']!='PASS' for x in resp['scorecard']):raise SystemExit('final critic did not accept')
 gate={'schema_version':'1.0','gate_id':'stage7-full-integration-confirmation-001','question':'是否确认14章全文集成稿作为最终文档导出的冻结上游工件？','allowed_actions':['CONFIRM','REJECT'],'candidate_set_hash':report['candidate_set_hash'],'requested_at':utc_now()};atomic_json(rd/'human_gate'/'stage7_gate_request.json',gate);append_event(rd,'HUMAN_GATE_REQUESTED',gate_id=gate['gate_id']);set_state(rd,'WAITING_GATE','FULL_INTEGRATION_CONFIRMATION',candidate_set_hash=report['candidate_set_hash'])
def ingest_repair_cmd(a):
 rd=Path(a.run_dir).resolve();resp=read_json(Path(a.response_file).resolve());errs=validate_schema(resp,'document_repair.schema.json')
 if errs:raise SystemExit('; '.join(errs))
 repair_round=int(resp['repair_round']);base=all_sections(rd,repaired=repair_round>1)
 index={(sid,p['paragraph_id']):(c,p) for sid,c in base.items() for p in paragraphs(c)};repaired=copy.deepcopy(base);touched=set()
 for e in resp.get('heading_edits',[]):
  sid=e['section_id']; sub=next((x for x in repaired[sid].get('subsections',[]) if x['subsection_id']==e['subsection_id']),None)
  if sub is None:raise SystemExit(f"unknown subsection {(sid,e['subsection_id'])}")
  old_title=next(x for x in base[sid].get('subsections',[]) if x['subsection_id']==e['subsection_id'])['title']
  if sha256_text(old_title)!=e['old_title_sha256']:raise SystemExit(f"old title hash mismatch {(sid,e['subsection_id'])}")
  sub['title']=e['new_title'];touched.add(sid)
 for e in resp.get('visual_caption_edits',[]):
  sid=e['section_id']; visual=next((x for x in repaired[sid].get('visual_placeholders',[]) if x['visual_id']==e['visual_id']),None)
  if visual is None:raise SystemExit(f"unknown visual {(sid,e['visual_id'])}")
  old_caption=next(x for x in base[sid].get('visual_placeholders',[]) if x['visual_id']==e['visual_id'])['caption']
  if sha256_text(old_caption)!=e['old_caption_sha256']:raise SystemExit(f"old caption hash mismatch {(sid,e['visual_id'])}")
  visual['caption']=e['new_caption'];touched.add(sid)
 for e in resp['edits']:
  key=(e['section_id'],e['paragraph_id'])
  if key not in index:raise SystemExit(f'unknown paragraph {key}')
  old=index[key][1]['text']
  if sha256_text(old)!=e['old_text_sha256']:raise SystemExit(f'old hash mismatch {key}')
  target=next(p for p in paragraphs(repaired[e['section_id']]) if p['paragraph_id']==e['paragraph_id']);target['text']=e['new_text'];touched.add(e['section_id'])
 for sid in touched:
  repaired[sid]['candidate_id']=repaired[sid]['candidate_id']+f'-stage7-r{repair_round}';repaired[sid]['markdown']=canonical_markdown(repaired[sid])
 allowed_heading_changes={(e['section_id'],e['subsection_id']) for e in resp.get('heading_edits',[])}
 allowed_visual_changes={(e['section_id'],e['visual_id']) for e in resp.get('visual_caption_edits',[])}
 for sid in SECTION_IDS:
  bsubs={x['subsection_id']:x['title'] for x in base[sid].get('subsections',[])};rsubs={x['subsection_id']:x['title'] for x in repaired[sid].get('subsections',[])}
  if set(bsubs)!=set(rsubs):raise SystemExit('subsection set changed')
  for sub_id in bsubs:
   if bsubs[sub_id]!=rsubs[sub_id] and (sid,sub_id) not in allowed_heading_changes:raise SystemExit(f'unapproved title change {sid}/{sub_id}')
  bvis={x['visual_id']:x for x in base[sid].get('visual_placeholders',[])};rvis={x['visual_id']:x for x in repaired[sid].get('visual_placeholders',[])}
  if set(bvis)!=set(rvis):raise SystemExit('visual set changed')
  for vid in bvis:
   for field in ['visual_id','placement_after_paragraph_id']:
    if bvis[vid].get(field)!=rvis[vid].get(field):raise SystemExit(f'visual metadata changed {sid}/{vid}/{field}')
   if bvis[vid].get('caption')!=rvis[vid].get('caption') and (sid,vid) not in allowed_visual_changes:raise SystemExit(f'unapproved caption change {sid}/{vid}')
  bp={p['paragraph_id']:p for p in paragraphs(base[sid])};rp={p['paragraph_id']:p for p in paragraphs(repaired[sid])}
  if set(bp)!=set(rp):raise SystemExit('paragraph set changed')
  for pid in bp:
   for k in ['role','node_ids','rq_ids','source_ids','novel_content_key','claim_status','paragraph_id']:
    if bp[pid][k]!=rp[pid][k]:raise SystemExit(f'metadata changed {sid}/{pid}/{k}')
 report=deterministic_report(rd,repaired,True);atomic_json(rd/'quality'/f'post_repair_round_{repair_round}_deterministic.json',report)
 if repair_round==2 and report['verdict']!='PASS':raise SystemExit(f"repair did not pass: {report['findings']}")
 if repair_round==1:
  allowed={'POST_REPAIR_EXPRESSION_ARTIFACTS'}
  extra={x['code'] for x in report['findings']}-allowed
  if extra:raise SystemExit(f'unexpected repair findings: {report["findings"]}')
 num=2*repair_round
 atomic_json(rd/'responses'/f'{num:03d}_targeted_document_edit_round{repair_round}.json',resp)
 record={'schema_version':'1.0','repair_round':repair_round,'touched_section_ids':sorted(touched),'sections':[{'section_id':sid,'candidate':repaired[sid]} for sid in SECTION_IDS],'citation_map':resp['citation_map'],'candidate_set_hash':report['candidate_set_hash']}
 atomic_json(rd/'intermediate'/f'repaired_sections_round_{repair_round}.json',record);atomic_json(rd/'intermediate'/'repaired_sections.json',record)
 append_event(rd,'MODEL_RESPONSE_INGESTED',response_file=f'responses/{num:03d}_targeted_document_edit_round{repair_round}.json',actual_model_id=resp['actual_model_id'],endpoint_id=resp['endpoint_id'])
 req=critic_request(rd,repair_round+1,repaired);write_request(rd,num+1,f'full_integration_critic_round{repair_round+1}',req);set_state(rd,'WAITING_MODEL',f'INTEGRATION_CRITIC_ROUND_{repair_round+1}',touched_section_ids=sorted(touched),candidate_set_hash=report['candidate_set_hash'])
def bibliography(source_registry:list[dict])->str:
 pubs=[x for x in source_registry if x['source_id'].startswith(('SRC-PUB','SRC-STD'))];out=['# 参考文献','']
 for i,x in enumerate(pubs,1):
  authors=', '.join(x['authors'][:3])+(' et al' if len(x['authors'])>3 else '')
  doi=f" DOI: {x['doi']}." if x.get('doi') else ''
  out.append(f"[{i}] {authors}. {x['title']}. {x['venue']}, {x['year']}.{doi}")
 return '\n'.join(out)+'\n'
def manifest_zip(rd:Path):
 mp=rd/'TRACE_MANIFEST.json';items=[]
 for p in sorted(rd.rglob('*')):
  if p.is_file() and p!=mp:items.append({'path':str(p.relative_to(rd)),'size_bytes':p.stat().st_size,'sha256':sha256_file(p)})
 atomic_json(mp,{'schema_version':'1.0','stage':STAGE,'generated_at':utc_now(),'file_count':len(items),'files':items});zp=rd.with_suffix('.zip')
 with zipfile.ZipFile(zp,'w',zipfile.ZIP_DEFLATED) as z:
  for p in sorted(rd.rglob('*')):
   if p.is_file():z.write(p,p.relative_to(rd.parent))
 atomic_json(rd.with_suffix('.archive.json'),{'archive_path':str(zp),'size_bytes':zp.stat().st_size,'sha256':sha256_file(zp),'generated_at':utc_now()});return zp
def finalize_cmd(a):
 rd=Path(a.run_dir).resolve();gate=read_json(Path(a.gate_response).resolve());req=read_json(rd/'human_gate'/'stage7_gate_request.json')
 if gate.get('gate_id')!=req['gate_id'] or gate.get('action')!='CONFIRM':raise SystemExit('gate mismatch')
 atomic_json(rd/'human_gate'/'stage7_gate_response.json',gate);append_event(rd,'HUMAN_GATE_CONSUMED',gate_id=gate['gate_id'],action='CONFIRM')
 secs=all_sections(rd,True);stage5=read_json(rd/'source_snapshots'/'stage5_section_plan.json');source_registry=read_json(rd/'source_snapshots'/'stage4a_evidence_completion.json')['source_registry'];parts=['# 人机协同决策优势冲刺关键技术研究','']
 rows=[]
 for sid in SECTION_IDS:
  c=secs[sid];md=canonical_markdown(c);atomic_text(rd/'outputs'/f'{sid}_{c["section_name"]}.md',md);parts += [md.rstrip(),''];ch=sum(len(re.sub(r'\s+','',p['text'])) for p in paragraphs(c));contract=next(x for x in stage5['sections'] if x['section_id']==sid);rows.append({'section_id':sid,'section_name':c['section_name'],'effective_char_count':ch,'target_pages':contract['target_pages'],'max_pages':contract['max_pages'],'candidate_id':c['candidate_id'],'candidate_hash':sha256_json(c)})
 body='\n'.join(parts).rstrip()+'\n';refs=bibliography(source_registry);atomic_text(rd/'outputs'/'stage7_integrated_proposal.md',body+'\n'+refs);atomic_text(rd/'outputs'/'stage7_main_body.md',body);atomic_text(rd/'outputs'/'stage7_references.md',refs)
 with (rd/'outputs'/'stage7_section_summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
 cmap=read_json(rd/'intermediate'/'repaired_sections.json')['citation_map'];atomic_json(rd/'outputs'/'stage7_citation_map.json',cmap)
 final=deterministic_report(rd,secs,True);result={'schema_version':'1.0','stage':STAGE,'status':'PASS','project_title':'人机协同决策优势冲刺关键技术研究','section_count':14,'total_effective_char_count':sum(x['effective_char_count'] for x in rows),'target_pages':sum(float(x['target_pages']) for x in rows),'max_pages':sum(float(x['max_pages']) for x in rows),'estimated_main_body_pages':final['estimated_pages'],'candidate_set_hash':final['candidate_set_hash'],'reference_count':len(cmap),'open_items':read_json(rd/'source_snapshots'/'stage4a_evidence_completion.json')['open_items_remaining'],'next_stage':'STAGE_8_FINAL_EXPORT','final_submission_ready':False,'completed_at':utc_now()};atomic_json(rd/'outputs'/'stage7_integration_result.json',result);atomic_json(rd/'outputs'/'STAGE7_ACCEPTANCE_REPORT.json',result);set_state(rd,'COMPLETED','STAGE_7_COMPLETE',candidate_set_hash=final['candidate_set_hash'],next_stage='STAGE_8_FINAL_EXPORT');zp=manifest_zip(rd);print(json.dumps({'result':result,'trace_zip':str(zp)},ensure_ascii=False,indent=2))
def validate_cmd(a):
 rd=Path(a.run_dir).resolve();m=read_json(rd/'TRACE_MANIFEST.json');err=[]
 for x in m['files']:
  p=rd/x['path'];
  if not p.exists():err.append('missing:'+x['path'])
  elif p.stat().st_size!=x['size_bytes']:err.append('size:'+x['path'])
  elif sha256_file(p)!=x['sha256']:err.append('hash:'+x['path'])
 print(json.dumps({'status':'PASS' if not err else 'FAIL','errors':err,'manifest_file_count':m['file_count']},ensure_ascii=False,indent=2));
 if err:raise SystemExit(1)
def main():
 ap=argparse.ArgumentParser();s=ap.add_subparsers(dest='cmd',required=True)
 p=s.add_parser('init');p.add_argument('--run-dir',required=True);p.add_argument('--project-definition',required=True);p.add_argument('--argument-architecture',required=True);p.add_argument('--evidence-completion',required=True);p.add_argument('--section-plan',required=True);p.add_argument('--stage6a',required=True);p.add_argument('--stage6b',required=True);p.add_argument('--stage6c',required=True);p.add_argument('--stage6d',required=True);p.set_defaults(fn=init_cmd)
 p=s.add_parser('ingest-critic');p.add_argument('--run-dir',required=True);p.add_argument('--response-file',required=True);p.set_defaults(fn=ingest_critic_cmd)
 p=s.add_parser('ingest-repair');p.add_argument('--run-dir',required=True);p.add_argument('--response-file',required=True);p.set_defaults(fn=ingest_repair_cmd)
 p=s.add_parser('finalize');p.add_argument('--run-dir',required=True);p.add_argument('--gate-response',required=True);p.set_defaults(fn=finalize_cmd)
 p=s.add_parser('validate');p.add_argument('--run-dir',required=True);p.set_defaults(fn=validate_cmd)
 a=ap.parse_args();a.fn(a)
if __name__=='__main__':main()
