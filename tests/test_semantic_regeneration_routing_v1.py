from __future__ import annotations
import copy
from app.workflow_repair import WorkflowRepairMixin
from app.workflows import WorkflowEngine
from app.workflow_status import WorkflowStatus
CRITIC="P-ARGUMENT-ARCHITECTURE-CRITIC"; PRODUCER="P-ARGUMENT-ARCHITECTURE"
class _Pack:
    def entry(self,prompt_id): return {"model_contract_mode":"SEMANTIC"} if prompt_id==CRITIC else {}
class _DB:
    def __init__(self): self.events=[]
    def audit(self,event_type,**kwargs): self.events.append((event_type,copy.deepcopy(kwargs)))
class _RegenerationHarness(WorkflowEngine):
    def __init__(self,wf): self._wf=wf; self.db=_DB(); self.pack=_Pack()
    def get(self,workflow_id):
        result=copy.deepcopy(self._wf); result["steps"]=[{"prompt_id":PRODUCER},{"prompt_id":CRITIC},{"prompt_id":"P-PROJECT-READINESS-CRITIC"}]; return result
    def _update(self,wf,**kwargs):
        if "status" in kwargs: wf["status"]=kwargs["status"]
        if "current_step" in kwargs: wf["current_step"]=kwargs["current_step"]
        if "state" in kwargs: wf["state"]=kwargs["state"]
        self._wf.update({"status":wf["status"],"current_step":wf["current_step"],"state":copy.deepcopy(wf["state"])})
    def _clear_workflow_repair_rereview(self,state,prompt_id):
        if isinstance(state.get("pending_repair_rereviews"),dict): state["pending_repair_rereviews"].pop(prompt_id,None)
class _AutoRepairFilterHarness(WorkflowRepairMixin):
    def __init__(self): self.pack=_Pack()
def _finding():
    return {"finding_instance_id":"F-STRUCT-001","code":"RESEARCH_DESIGN_INCOMPLETE","severity":"P1","category":"ARGUMENT","target_type":"ARGUMENT_SEMANTIC_COMPONENT","target_path_or_span":"/result/research_design_matrix/0","description":"缺少完成研究闭环所需的方法实体。","evidence_refs":[],"repairable":True,"repair_instruction":"重新生成该研究线程并补齐方法与评价。","suggested_route":"ORIGINAL_PRODUCER","blocking":True}
def test_structural_critic_routes_back_to_producer():
    state={"options":{"original_producer_regeneration_limit":2},"step_results":{"0":{},"1":{},"2":{}},"section_results":[{"section_id":"S1"}],"planning_revision_findings":[{"code":"OLD"}]}
    wf={"id":"wf-1","project_id":"project-1","workflow_type":"WF-4_PROPOSAL_AUTHORING","status":WorkflowStatus.RUNNING.value,"current_step":1,"state":state}; engine=_RegenerationHarness(wf)
    assert engine._prepare_original_producer_regeneration(wf,state,critic_prompt=CRITIC,output={"findings":[_finding()]})=="SCHEDULED"
    assert wf["current_step"]==0; assert state["step_results"]=={}; assert state["section_results"]==[]; assert state["producer_revision_findings"][PRODUCER]
def test_structural_regeneration_is_bounded():
    state={"options":{"original_producer_regeneration_limit":1},"producer_regeneration_rounds":{CRITIC:1},"step_results":{}}
    wf={"id":"wf-1","project_id":"project-1","workflow_type":"WF-4_PROPOSAL_AUTHORING","status":WorkflowStatus.RUNNING.value,"current_step":1,"state":state}; engine=_RegenerationHarness(wf)
    assert engine._prepare_original_producer_regeneration(wf,state,critic_prompt=CRITIC,output={"findings":[_finding()]})=="EXHAUSTED"
    assert wf["status"]==WorkflowStatus.BLOCKED_CONTENT.value

FACT_PRODUCER="P-FACT-EXTRACT"
def _quality_finding(code="QG_FACT_NOT_ATOMIC"):
    return {"code":code,"severity":"P1","category":"FACT","target_type":"FACT_PACKAGE","target_path_or_span":"fact_candidates","description":"事实FC-005包含多个可独立判真的分句。","evidence_refs":["FC-005"],"repair_instruction":"拆分为一条记录一个命题。","suggested_route":"PROJECT_KNOWLEDGE_AGENT","blocking":True}
class _ContextBuilderStub:
    def __init__(self,has_revision_findings): self.has_revision_findings=has_revision_findings; self.built=[]
    def _schema_for_path(self,prompt_id,path):
        return {} if (self.has_revision_findings and path=="payload.revision_findings") else None
    def build(self,prompt_id,project_id,**kwargs): self.built.append(prompt_id); return {}
class _SemanticRegenerationHarness(WorkflowEngine):
    def __init__(self,wf,has_revision_findings=True):
        self._wf=wf; self.db=_DB(); self.context_builder=_ContextBuilderStub(has_revision_findings)
        class _Pack2:
            def entry(self,prompt_id): return {}
            def validate_common(self,schema,obj): return []
        self.pack=_Pack2()
    def get(self,workflow_id):
        result=copy.deepcopy(self._wf); result["steps"]=[{"prompt_id":FACT_PRODUCER}]; return result
    def _update(self,wf,**kwargs):
        if "status" in kwargs: wf["status"]=kwargs["status"]
        if "state" in kwargs: wf["state"]=kwargs["state"]
        self._wf.update({"status":wf["status"],"state":copy.deepcopy(wf["state"])})
    def _clear_workflow_repair_rereview(self,state,prompt_id):
        if isinstance(state.get("pending_repair_rereviews"),dict): state["pending_repair_rereviews"].pop(prompt_id,None)
def _wf_state():
    return {"options":{},"step_results":{"0":{"run_id":"run-baseline-1"}}}
def _wf(state):
    return {"id":"wf-1","project_id":"project-1","workflow_type":"WF-1_PROJECT_INTAKE","status":WorkflowStatus.RUNNING.value,"current_step":0,"state":state}
def test_legacy_producer_with_revision_findings_contract_self_repairs_quality_codes():
    state=_wf_state(); wf=_wf(state); engine=_SemanticRegenerationHarness(wf)
    result=engine._prepare_semantic_producer_regeneration(wf,state,producer_prompt=FACT_PRODUCER,output={"result":{},"findings":[_quality_finding()]})
    assert result=="SCHEDULED"
    assert state["semantic_producer_regeneration_rounds"][FACT_PRODUCER]==1
    assert state["producer_revision_findings"][FACT_PRODUCER]
    assert engine.context_builder.built==[FACT_PRODUCER]
def test_legacy_producer_without_revision_findings_contract_stays_not_applicable():
    state=_wf_state(); wf=_wf(state); engine=_SemanticRegenerationHarness(wf,has_revision_findings=False)
    result=engine._prepare_semantic_producer_regeneration(wf,state,producer_prompt=FACT_PRODUCER,output={"result":{},"findings":[_quality_finding()]})
    assert result=="NOT_APPLICABLE"
    assert engine.context_builder.built==[]
def test_legacy_producer_ignores_non_model_repairable_codes():
    state=_wf_state(); wf=_wf(state); engine=_SemanticRegenerationHarness(wf)
    result=engine._prepare_semantic_producer_regeneration(wf,state,producer_prompt=FACT_PRODUCER,output={"result":{},"findings":[_quality_finding("QG_FACT_SOURCE_COVERAGE_INCOMPLETE")]})
    assert result=="NOT_APPLICABLE"
    assert engine.context_builder.built==[]
