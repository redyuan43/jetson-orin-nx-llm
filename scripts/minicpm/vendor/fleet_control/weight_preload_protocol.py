"""Pinned load-only policy protocol; no model imports or lifecycle writes."""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import uuid
from urllib.parse import urlsplit

TARGETS = {
    'u24-4060ti-group': ('GPU-0befdd20-6ea9-4e7e-3378-635e20f42536', 'h3', 'u24-h3-video-v1', 'u24-turbo8-768p-v1', 'MiniMaxH3', 19488),
    'nx5-igpu-group': ('nx5-igpu', 'comfy', 'nx5-qwen21', 'qwen21-t2i-nx5-20261005', 'QwenImage21', 8188),
}
CLAIM_KEYS = frozenset(('purpose','group_id','executor_id','operation_id','profile_id','profile_version','policy_revision','graph_sha256','prompt_id','nonce'))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _need(value, reason):
    if not value:
        raise ValueError(reason)


def validate_config(config):
    _need(isinstance(config, dict), 'weight_preload_config_invalid')
    expected = {'schema_version','group_id','resource_id','mode','profile_id','profile_version','model_class','native_base',
                'central','ticket_key_file','graph_file','graph_sha256','budget'}
    _need(set(config)==expected and type(config['schema_version']) is int and config['schema_version']==1,
          'weight_preload_schema_invalid')
    target=TARGETS.get(config['group_id'])
    _need(target is not None and tuple(config[k] for k in ('resource_id','mode','profile_id','profile_version','model_class'))==target[:5],
          'weight_preload_target_invalid')
    _need(config['native_base']=='http://127.0.0.1:'+str(target[5]), 'weight_preload_native_origin_invalid')
    central=config['central']
    _need(isinstance(central,dict) and set(central)=={'base_url','token_file'},'weight_preload_central_invalid')
    url=urlsplit(central['base_url'])
    _need(url.scheme=='http' and url.hostname and url.port and not url.username and not url.password
          and not url.query and not url.fragment and url.path in ('','/'), 'weight_preload_central_origin_invalid')
    for name in ('ticket_key_file','graph_file'):
        _need(isinstance(config[name],str) and Path(config[name]).is_absolute(), 'weight_preload_absolute_path_required')
    _need(isinstance(central['token_file'],str) and Path(central['token_file']).is_absolute(),'weight_preload_absolute_path_required')
    _need(isinstance(config['graph_sha256'],str) and re.fullmatch('[a-f0-9]{64}',config['graph_sha256']), 'weight_preload_graph_digest_invalid')
    budget=config['budget']
    keys={'max_cuda_bytes','reserve_cuda_bytes','min_ram_available_bytes','min_loaded_bytes','unified_memory'}
    _need(isinstance(budget,dict) and set(budget)==keys and type(budget['unified_memory']) is bool,
          'weight_preload_budget_invalid')
    _need(all(type(budget[k]) is int and 0<budget[k]<=2**50 for k in keys-{'unified_memory'})
          and budget['min_loaded_bytes']<=budget['max_cuda_bytes'], 'weight_preload_budget_invalid')
    _need(budget['unified_memory']==(config['group_id']=='nx5-igpu-group'), 'weight_preload_memory_kind_invalid')
    return copy.deepcopy(config)


def _read(path, *, secret=False):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid(), 'weight_preload_owned_regular_required')
        limit=512 if secret else 1024*1024
        _need(0<info.st_size<=limit and (not secret or info.st_mode&0o077==0),'weight_preload_private_key_required' if secret else 'weight_preload_graph_limit')
        raw=os.read(fd,limit+1)
        _need(len(raw)<=limit,'weight_preload_file_limit')
        return raw.strip() if secret else raw
    finally:
        os.close(fd)


def private_token(path):
    token = _read(path, secret=True).decode('ascii')
    _need(re.fullmatch(r'[A-Za-z0-9._~-]{1,480}', token), 'weight_preload_private_token_invalid')
    return token


def _graph(config):
    graph=json.loads(_read(config['graph_file']))
    _check_graph(graph)
    _need(hashlib.sha256(canonical(graph)).hexdigest()==config['graph_sha256'],'weight_preload_graph_digest_mismatch')
    return graph


def _check_graph(graph):
    _need(isinstance(graph,dict) and 1<=len(graph)<=32 and all(isinstance(k,str) for k in graph), 'weight_preload_graph_invalid')
    tail=graph.get('preload')
    _need(isinstance(tail,dict) and set(tail)=={'class_type','inputs'} and tail['class_type']=='SiyuanPreloadModel'
          and set(tail['inputs'])=={'model','authorization_json'} and tail['inputs']['authorization_json']=='$AUTH',
          'weight_preload_output_invalid')
    seen=set(); visiting=set()
    def visit(node_id):
        _need(node_id in graph and node_id not in visiting,'weight_preload_graph_cycle_or_missing')
        if node_id in seen:return
        visiting.add(node_id);node=graph[node_id]
        _need(isinstance(node,dict) and set(node) in ({'class_type','inputs'}, {'class_type','inputs','_meta'}) and isinstance(node['inputs'],dict)
              and isinstance(node['class_type'],str),'weight_preload_node_invalid')
        # The signed fixed graph still must never contain inference/output work.
        _need(node['class_type'] in {'UNETLoader','LoraLoaderModelOnly','LoraLoaderBypassModelOnly',
              'H3SLAAttention','SiyuanPreloadModel'}, 'weight_preload_non_loader_node')
        for value in node['inputs'].values():
            if isinstance(value,list):
                _need(len(value)==2 and isinstance(value[0],str) and type(value[1]) is int and value[1]==0,
                      'weight_preload_model_link_invalid')
                visit(value[0])
        visiting.remove(node_id);seen.add(node_id)
    visit('preload')
    _need(seen==set(graph),'weight_preload_unconnected_node')
    link=tail['inputs']['model']
    _need(isinstance(link,list) and len(link)==2 and link[0]!='preload','weight_preload_model_parent_required')


def validate_policy_barrier(config, activity, operation, revision):
    config=validate_config(config)
    _need(re.fullmatch('op_[a-f0-9]{32}',operation or '') and type(revision) is int and revision>=1,'weight_preload_policy_identity_invalid')
    _need(isinstance(activity,dict) and activity.get('group_id')==config['group_id']
          and activity.get('resources')==[config['resource_id']] and activity.get('mode')==config['mode']
          and activity.get('state') in {'ready','loading'} and activity.get('fresh') is True
          and type(activity.get('active')) is int and activity['active']>=0
          and activity.get('leases')==[] and activity.get('pending')==[], 'weight_preload_activity_not_exclusive')
    transition,demand=activity.get('transition'),activity.get('policy_demand')
    executor='device-policy:'+config['group_id']
    _need(isinstance(transition,dict) and transition.get('operation_id')==operation
          and transition.get('executor_id')==executor and transition.get('target_mode')==config['mode'],
          'weight_preload_original_barrier_required')
    _need(isinstance(demand,dict) and demand.get('operation_id')==operation and demand.get('executor_id')==executor
          and demand.get('profile_id')==config['profile_id'] and demand.get('profile_version')==config['profile_version']
          and demand.get('policy_revision')==revision and type(demand.get('policy_revision')) is int
          and demand.get('mode')==config['mode'],'weight_preload_original_demand_required')
    return True


def build_preload_request(config, demand, nonce, prompt_id):
    config=validate_config(config)
    _need(isinstance(demand,dict),'weight_preload_demand_invalid')
    _need(isinstance(nonce,str) and re.fullmatch('[a-f0-9]{32,128}',nonce),'weight_preload_nonce_invalid')
    try:_need(str(uuid.UUID(prompt_id))==prompt_id,'weight_preload_prompt_id_invalid')
    except (ValueError,TypeError,AttributeError):raise ValueError('weight_preload_prompt_id_invalid') from None
    claims=dict(purpose='weight-preload',group_id=config['group_id'],executor_id='device-policy:'+config['group_id'],
        operation_id=demand.get('operation_id'),profile_id=config['profile_id'],profile_version=config['profile_version'],
        policy_revision=demand.get('policy_revision'),graph_sha256=config['graph_sha256'],prompt_id=prompt_id,nonce=nonce)
    _need(re.fullmatch('op_[a-f0-9]{32}',claims['operation_id'] or '') and type(claims['policy_revision']) is int
          and claims['policy_revision']>=1 and all(demand.get(k)==claims[k] for k in ('executor_id','profile_id','profile_version'))
          and demand.get('mode')==config['mode'],'weight_preload_demand_identity_invalid')
    graph=_graph(config)
    signature=hmac.new(_read(config['ticket_key_file'],secret=True),canonical(claims),hashlib.sha256).hexdigest()
    graph['preload']['inputs']['authorization_json']=canonical(dict(claims=claims,signature=signature)).decode()
    return dict(prompt=graph,prompt_id=prompt_id,extra_data={'siyuan_weight_preload':1})


def validate_preload_request(config, body, headers, activity):
    config=validate_config(config)
    _need(isinstance(body,dict) and set(body)=={'prompt','prompt_id','extra_data'}
          and body['extra_data']=={'siyuan_weight_preload':1},'weight_preload_body_invalid')
    graph=copy.deepcopy(body['prompt'])
    try:
        authorization=json.loads(graph['preload']['inputs']['authorization_json'])
        graph['preload']['inputs']['authorization_json']='$AUTH'
    except (KeyError,TypeError,ValueError):raise ValueError('weight_preload_authorization_invalid') from None
    _check_graph(graph)
    _need(graph==_graph(config),'weight_preload_graph_not_pinned')
    _need(isinstance(authorization,dict) and set(authorization)=={'claims','signature'},'weight_preload_authorization_invalid')
    claims=authorization['claims']
    _need(isinstance(claims,dict) and set(claims)==CLAIM_KEYS,'weight_preload_claims_invalid')
    expected=build_preload_request(config,{**claims,'mode':config['mode']},claims['nonce'],body['prompt_id'])
    expected_auth=json.loads(expected['prompt']['preload']['inputs']['authorization_json'])
    _need(claims==expected_auth['claims'] and isinstance(authorization['signature'],str)
          and hmac.compare_digest(authorization['signature'],expected_auth['signature']),'weight_preload_signature_invalid')
    _need(headers.get('X-Fleet-Operation')==claims['operation_id'] and headers.get('X-Fleet-Executor')==claims['executor_id'],
          'weight_preload_headers_invalid')
    validate_policy_barrier(config,activity,claims['operation_id'],claims['policy_revision'])
    return copy.deepcopy(claims)
