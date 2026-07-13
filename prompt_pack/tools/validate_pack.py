from __future__ import annotations
import json, sys, hashlib, zipfile
from pathlib import Path
import yaml
from jsonschema import Draft202012Validator, RefResolver

ROOT=Path(__file__).resolve().parents[1]
errors=[]
counts={}

def load_json(p):
    try: return json.loads(p.read_text(encoding='utf-8'))
    except Exception as e: errors.append(f'JSON_PARSE {p.relative_to(ROOT)}: {e}'); return None

def validate(instance, schema_path, label):
    schema=load_json(schema_path)
    if schema is None: return False
    try:
        Draft202012Validator.check_schema(schema)
        resolver=RefResolver(base_uri=schema_path.resolve().as_uri(), referrer=schema)
        Draft202012Validator(schema, resolver=resolver).validate(instance)
        return True
    except Exception as e:
        errors.append(f'SCHEMA_VALIDATE {label}: {type(e).__name__}: {e}')
        return False

# all json and yaml parse
json_files=list(ROOT.rglob('*.json'))
yaml_files=list(ROOT.rglob('*.yaml'))+list(ROOT.rglob('*.yml'))
for p in json_files: load_json(p)
for p in yaml_files:
    try: yaml.safe_load(p.read_text(encoding='utf-8'))
    except Exception as e: errors.append(f'YAML_PARSE {p.relative_to(ROOT)}: {e}')

reg=load_json(ROOT/'config/prompt_registry.json')
counts['registry_entries']=len(reg['prompts']) if reg else 0
if reg:
    ids=[x['prompt_id'] for x in reg['prompts']]
    if len(ids)!=26 or len(set(ids))!=26: errors.append(f'PROMPT_COUNT expected 26 unique, got {len(ids)}/{len(set(ids))}')
    profiles=yaml.safe_load((ROOT/'config/prompt_model_profiles.yaml').read_text(encoding='utf-8'))['profiles']
    for p in reg['prompts']:
        for key in ['prompt_file','input_schema','output_schema']:
            if not (ROOT/p[key]).exists(): errors.append(f'MISSING {p["prompt_id"]} {key}={p[key]}')
        if p['model_profile'] not in profiles: errors.append(f'MODEL_PROFILE_MISSING {p["prompt_id"]}: {p["model_profile"]}')
        if not p.get('executor_role'): errors.append(f'EXECUTOR_ROLE_MISSING {p["prompt_id"]}')
        text=(ROOT/p['prompt_file']).read_text(encoding='utf-8')
        if f'执行角色：`{p.get("executor_role")}`' not in text: errors.append(f'PROMPT_EXECUTOR_MISMATCH {p["prompt_id"]}')
        if len(text)<2200: errors.append(f'PROMPT_TOO_SHORT {p["prompt_id"]}: {len(text)}')
        for heading in ['## 角色与权限','## 必须读取的输入','## 执行步骤','## 状态判定','## Finding代码','## 强制自检','## 输出要求']:
            if heading not in text: errors.append(f'PROMPT_HEADING_MISSING {p["prompt_id"]}: {heading}')
        inp=load_json(ROOT/p['input_schema']); out=load_json(ROOT/p['output_schema'])
        if inp:
            try: Draft202012Validator.check_schema(inp)
            except Exception as e: errors.append(f'INPUT_SCHEMA_INVALID {p["prompt_id"]}: {e}')
        if out:
            try: Draft202012Validator.check_schema(out)
            except Exception as e: errors.append(f'OUTPUT_SCHEMA_INVALID {p["prompt_id"]}: {e}')

# input schemas and common schemas self-check
for p in ROOT.glob('schemas/**/*.json'):
    schema=load_json(p)
    if schema:
        try: Draft202012Validator.check_schema(schema)
        except Exception as e: errors.append(f'SCHEMA_SELF_INVALID {p.relative_to(ROOT)}: {e}')

manifest=load_json(ROOT/'replay/manifest.json')
counts['replay_manifest_entries']=len(manifest['cases']) if manifest else 0
valid_in=invalid_in=valid_out=0
unified_in=unified_out=0
if reg and manifest:
    by_id={x['prompt_id']:x for x in reg['prompts']}
    if len(manifest['cases'])!=130: errors.append(f'REPLAY_COUNT expected 130 got {len(manifest["cases"])}')
    for entry in manifest['cases']:
        path=ROOT/entry['fixture_path']
        if not path.exists(): errors.append(f'REPLAY_MISSING {entry["fixture_path"]}'); continue
        case=load_json(path)
        if not case: continue
        p=by_id[entry['prompt_id']]
        inp_schema=ROOT/p['input_schema']; out_schema=ROOT/p['output_schema']
        # validate without recording expected failure as error
        schema=load_json(inp_schema)
        try:
            resolver=RefResolver(base_uri=inp_schema.resolve().as_uri(), referrer=schema)
            Draft202012Validator(schema, resolver=resolver).validate(case['input'])
            actual_in=True
        except Exception:
            actual_in=False
        expected_in=case['expected_validation']['input_schema_valid']
        if actual_in!=expected_in: errors.append(f'REPLAY_INPUT_EXPECTATION {entry["fixture_path"]}: expected {expected_in}, got {actual_in}')
        if actual_in:
            valid_in+=1
            unified_schema=load_json(ROOT/'schemas/common/prompt_input_envelope.schema.json')
            try:
                resolver=RefResolver(base_uri=(ROOT/'schemas/common/prompt_input_envelope.schema.json').resolve().as_uri(), referrer=unified_schema)
                Draft202012Validator(unified_schema, resolver=resolver).validate(case['input'])
                unified_in+=1
            except Exception as e:
                errors.append(f'UNIFIED_INPUT {entry["fixture_path"]}: {e}')
        else: invalid_in+=1
        if case.get('expected_output') is not None:
            if validate(case['expected_output'],out_schema,f'{entry["fixture_path"]} output'):
                valid_out+=1
                unified_schema=load_json(ROOT/'schemas/common/prompt_output_envelope.schema.json')
                try:
                    resolver=RefResolver(base_uri=(ROOT/'schemas/common/prompt_output_envelope.schema.json').resolve().as_uri(), referrer=unified_schema)
                    Draft202012Validator(unified_schema, resolver=resolver).validate(case['expected_output'])
                    unified_out+=1
                except Exception as e:
                    errors.append(f'UNIFIED_OUTPUT {entry["fixture_path"]}: {e}')
            expected_status=case['expected_validation']['expected_status']
            if case['expected_output']['status']!=expected_status: errors.append(f'REPLAY_STATUS {entry["fixture_path"]}')

counts.update({'json_files':len(json_files),'yaml_files':len(yaml_files),'valid_replay_inputs':valid_in,'intentional_invalid_replay_inputs':invalid_in,'valid_replay_outputs':valid_out,'unified_envelope_valid_inputs':unified_in,'unified_envelope_valid_outputs':unified_out})
# profiles
profiles=list((ROOT/'profiles').glob('*.yaml')); counts['profiles']=len(profiles)
if len(profiles)!=8: errors.append(f'PROFILE_COUNT expected 8 got {len(profiles)}')
for p in profiles:
    d=yaml.safe_load(p.read_text(encoding='utf-8'))
    for k in ['profile_id','version','purpose','must_answer','required_item_types','recommended_chain','paragraph_roles','forbidden','readiness','critic_checks']:
        if k not in d: errors.append(f'PROFILE_FIELD {p.name}: {k}')

# model config
endpoints=yaml.safe_load((ROOT/'config/model_endpoints.yaml').read_text(encoding='utf-8'))
models=yaml.safe_load((ROOT/'config/models.yaml').read_text(encoding='utf-8'))
endpoint_ids={x['endpoint_id'] for x in endpoints['endpoints']}
for m in models['models']:
    if m['endpoint_id'] not in endpoint_ids: errors.append(f'MODEL_ENDPOINT {m["model_id"]}')
counts['model_endpoints']=len(endpoint_ids); counts['models']=len(models['models'])
# Routing and environment invariants
for p in reg['prompts']:
    pid=p['prompt_id']; env=p['required_environment']
    if pid.startswith('P-PUBLIC-RESEARCH-') and env!='ONLINE_PUBLIC': errors.append(f'PUBLIC_PROMPT_ENV {pid}: {env}')
    if not pid.startswith('P-PUBLIC-RESEARCH-') and pid!='P-TARGETED-REPAIR' and env!='OFFLINE_LOCAL': errors.append(f'OFFLINE_PROMPT_ENV {pid}: {env}')
    if pid=='P-TARGETED-REPAIR' and env!='SAME_AS_ORIGINAL': errors.append(f'REPAIR_ENV {env}')
routing=yaml.safe_load((ROOT/'policies/model_routing.yaml').read_text(encoding='utf-8'))
if routing.get('default',{}).get('deny') is not True: errors.append('MODEL_ROUTING_DEFAULT_NOT_DENY')
if not any('不得从OFFLINE_LOCAL自动Fallback到ONLINE_PUBLIC' in x for x in routing.get('prohibitions',[])): errors.append('MODEL_ROUTING_OFFLINE_FALLBACK_PROHIBITION_MISSING')

report={'version':'2.0','status':'PASS' if not errors else 'FAIL','counts':counts,'errors':errors}
(ROOT/'BUILD_REPORT.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
sys.exit(0 if not errors else 1)
