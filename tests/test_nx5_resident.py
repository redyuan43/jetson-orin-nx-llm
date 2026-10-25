import io
import hashlib
import json
import os
from pathlib import Path
import queue
import sys
import shutil
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from test_nx5_service import module
from resident import ResidentModel, CentralLease, profile_digest


class ResidentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.model = ResidentModel(self.root, {'state': str(self.root/'jobs')})

    def tearDown(self):
        self.tmp.cleanup()

    def test_verified_off_observation_is_ready_for_reservation(self):
        central = object.__new__(CentralLease)
        central.group = '/group'
        central.api = Mock()
        central.observe('off', 0)
        self.assertEqual(central.api.call_args.args[2],
                         {'mode':'off','state':'ready','active':0,'operation_id':None})

    def comfy_owner(self):
        owner = object.__new__(module.ManagedComfy)
        owner.policy_profiles = {'comfy': {
            'profile_id': 'nx5-qwen21', 'version': 'qwen21-t2i-nx5-20261005'}}
        owner.policy_state = {}
        owner.service = type('ServiceFixture', (), {'resident': self.model, 'closing': False, 'jobs': {}})()
        return owner

    @staticmethod
    def comfy_policy():
        return {'operation_id': 'op_' + 'a' * 32, 'profile_id': 'nx5-qwen21',
                'profile_version': 'qwen21-t2i-nx5-20261005', 'mode': 'comfy',
                'activation': 'controller', 'policy_revision': 16,
                'executor_id': 'device-policy:nx5-igpu-group', 'state': 'transitioning',
                'weight_preload_required': True}

    def preload_owner(self, state='running', loaded=False):
        owner = self.comfy_owner()
        owner.policy_profiles['comfy']['weight_preload'] = {'schema_version': 1}
        owner.policy_comfy_transition = Mock(return_value=True)
        owner.check_policy_barrier = Mock()
        owner.report = Mock()
        owner.checked_activity = Mock(return_value={
            'group_id': 'nx5-igpu-group', 'resources': ['nx5-igpu'], 'fresh': True,
            'state': 'ready', 'active': 0, 'leases': [], 'pending': []})
        owner.unit = Mock(return_value='active')
        owner.flint_unit = Mock(return_value='inactive')
        owner.queue = Mock(return_value=0)
        owner.queue_state = Mock(return_value={'queue_running': [], 'queue_pending': []})
        client = Mock()
        client.ensure.return_value = {'state': state, 'prompt_id': '11111111-1111-4111-8111-111111111111'}
        owner.weight_preload_client = Mock(return_value=client)
        owner.profile_observation = Mock(return_value={'mode': 'comfy', 'profile_id': 'nx5-qwen21',
                                                       'profile_version': 'qwen21-t2i-nx5-20261005',
                                                       'model_loaded': loaded})
        owner.policy_state = {}
        owner.save_policy = Mock(side_effect=lambda value: setattr(owner, 'policy_state', value))
        return owner, client

    def test_comfy_weight_preload_runs_only_inside_original_idle_policy_transition(self):
        owner, client = self.preload_owner()
        policy = self.comfy_policy()
        transition = {'operation_id': policy['operation_id'], 'executor_id': policy['executor_id'],
                      'target_mode': 'comfy'}

        result = owner._ensure_weight_preload(policy, transition)

        self.assertEqual(result['state'], 'running')
        owner.weight_preload_client.assert_called_once_with(policy)
        client.ensure.assert_called_once_with(policy)
        self.assertEqual(owner.checked_activity.call_args_list,
                         [unittest.mock.call(require_ready=False), unittest.mock.call(require_ready=False)])
        owner.report.assert_called_once_with({'mode': 'comfy', 'state': 'loading', 'active': 1}, transition)

    def test_preload_unknown_stays_pending_and_reuses_the_same_operation(self):
        owner, client = self.preload_owner(state='unknown')
        policy = self.comfy_policy()
        transition = {'operation_id': policy['operation_id'], 'executor_id': policy['executor_id'],
                      'target_mode': 'comfy'}

        first = owner._ensure_weight_preload(policy, transition)
        second = owner._ensure_weight_preload(policy, transition)

        self.assertEqual((first['state'], second['state']), ('unknown', 'unknown'))
        self.assertEqual(owner.weight_preload_client.call_count, 2)
        self.assertEqual(client.ensure.call_args_list, [unittest.mock.call(policy), unittest.mock.call(policy)])
        self.assertEqual(owner.report.call_count, 2)
        self.assertTrue(all(call.args[0]['state'] == 'loading' for call in owner.report.call_args_list))

    def test_real_running_preload_journal_resumes_own_busy_queue_to_complete(self):
        import shutil
        import time
        import urllib.parse
        from unittest.mock import patch
        source_vendor = Path(module.__file__).resolve().parents[1]/'minicpm/vendor/fleet_control'
        vendor = self.root/'repo/scripts/minicpm/vendor/fleet_control'
        vendor.mkdir(parents=True)
        for name in ('__init__.py', 'comfy_preload.py', 'weight_preload_protocol.py'):
            shutil.copyfile(source_vendor/name, vendor/name)
        graph = {'1': {'class_type':'UNETLoader','inputs':{
                    'unet_name':'qwen_image_2.1_int8_convrot.safetensors','weight_dtype':'default'}},
                 'preload': {'class_type':'SiyuanPreloadModel','inputs':{
                    'model':['1',0],'authorization_json':'$AUTH'}}}
        graph_raw = json.dumps(graph,sort_keys=True,separators=(',',':')).encode()
        graph_path = self.root/'graph.json'; graph_path.write_bytes(graph_raw)
        token = self.root/'central.token'; token.write_text('opaque-token'); token.chmod(0o600)
        key = self.root/'ticket.key'; key.write_text('opaque-ticket-key'); key.chmod(0o600)
        config = {'schema_version':1,'group_id':'nx5-igpu-group','resource_id':'nx5-igpu',
            'mode':'comfy','profile_id':'nx5-qwen21','profile_version':'qwen21-t2i-nx5-20261005',
            'model_class':'QwenImage21','native_base':'http://127.0.0.1:8188',
            'central':{'base_url':'http://100.91.42.28:18292','token_file':str(token)},
            'ticket_key_file':str(key),'graph_file':str(graph_path),
            'graph_sha256':hashlib.sha256(graph_raw).hexdigest(),
            'budget':{'max_cuda_bytes':3*1024**3,'reserve_cuda_bytes':4*1024**3,
                'min_ram_available_bytes':9*1024**3,'min_loaded_bytes':256*1024**2,
                'unified_memory':True}}
        owner, _unused = self.preload_owner()
        del owner.weight_preload_client
        del owner.queue
        owner.policy_profiles['comfy']['weight_preload'] = config
        owner.service.repo = self.root/'repo'; owner.service.state = self.root/'state/jobs'
        owner.service.jobs = {}; owner.service.closing = False
        owner.service.lock = threading.RLock()
        owner.policy_state = {}
        owner.policy_path = self.root/'state/policy-demand.json'
        owner.policy_path.parent.mkdir(parents=True, exist_ok=True)
        owner.intent = {}
        owner.path = self.root/'state/managed-comfy.json'
        owner.observed = {'mode':'unknown','state':'unknown','active':1}
        del owner.checked_activity
        del owner.policy_comfy_transition
        del owner.check_policy_barrier
        del owner.report
        prompt_id = '11111111-1111-4111-8111-111111111111'
        request_prompt = {}
        dispatches = []
        history_complete = {'value':False}
        def queue_item():
            return [] if history_complete['value'] else [[0,dispatches[-1] if dispatches else prompt_id,{},{}]]
        hook = {'schema_version':1,'source':'comfy.current_loaded_models',
            'observed_at':time.time(),'models':[{'model_class':'QwenImage21',
                'device_kind':'cuda','loaded_bytes':512*1024**2}],
            'count':1,'loaded_bytes':512*1024**2,'model_loaded':True,'busy':False}
        def owner_request(path):
            if path == '/queue':
                return {'queue_running':queue_item(),'queue_pending':[]} if dispatches else {
                    'queue_running':[],'queue_pending':[]}
            if path == '/siyuan/loaded-models': return hook
            raise AssertionError(path)
        owner.request = owner_request
        owner.profile_observation = module.ManagedComfy.profile_observation.__get__(owner)
        owner.unit = Mock(return_value='active'); owner.flint_unit = Mock(return_value='inactive')
        owner.action = Mock(); owner.flint_action = Mock()
        owner.demand_for_mode = Mock(return_value=None)
        class Response:
            def __init__(self, value): self.raw=json.dumps(value).encode()
            def __enter__(self): return self
            def __exit__(self,*_args): return False
            def read(self,_limit=-1): return self.raw
        class Opener:
            history_reads = 0
            def open(self, request, timeout=0):
                path=urllib.parse.urlsplit(request.full_url).path
                if path == '/prompt':
                    body=json.loads(request.data); request_prompt.update(body['prompt'])
                    dispatches.append(body['prompt_id'])
                    return Response({'prompt_id':body['prompt_id']})
                if path.startswith('/history/'):
                    self.history_reads += 1
                    if self.history_reads == 1: return Response({})
                    actual_prompt=path.rsplit('/',1)[-1]
                    history_complete['value']=True
                    receipt={'operation_id':policy['operation_id'],'prompt_id':actual_prompt,
                        'graph_sha256':config['graph_sha256'],'model_class':'QwenImage21',
                        'scope':'main_model_partial_cuda','inference_executed':False,
                        'loaded_bytes':512*1024**2}
                    saved={'prompt':['',actual_prompt,request_prompt], 'status':{
                        'status_str':'success','completed':True},
                        'outputs':{'preload':{'siyuan_weight_preload':[receipt]}}}
                    return Response({actual_prompt:saved})
                if path == '/siyuan/loaded-models':
                    return Response({'models':[{'model_class':'QwenImage21','device_kind':'cuda',
                        'loaded_bytes':512*1024**2}],'busy':False})
                if path == '/queue': return Response({'queue_running':queue_item(),'queue_pending':[]})
                raise AssertionError(path)
        fake=Opener()
        policy=self.comfy_policy()
        transition={'group_id':'nx5-igpu-group','resources':['nx5-igpu'],
            'lease_barrier':True,'operation_id':policy['operation_id'],
            'executor_id':policy['executor_id'],'target_mode':'comfy'}
        central_row={'group_id':'nx5-igpu-group','resources':['nx5-igpu'],'fresh':True,
            'state':'ready','active':0,'leases':[],'pending':[],'policy_demand':dict(policy)}
        class Central:
            group='/internal/v1/resource-groups/nx5-igpu-group'
            observations=[]
            def api(self,method,path,body=None,controller=False):
                if method=='GET' and path.endswith('/activity'): return dict(central_row)
                if method=='GET' and path=='/internal/v1/mode-transitions':
                    return {'transitions':[transition]}
                if method=='POST' and path.endswith('/observation'):
                    self.observations.append(dict(body))
                    central_row.update(state=body['state'],active=body['active'])
                    return {'ok':True}
                raise AssertionError((method,path))
        owner.central=Central()
        owner.wire=lambda:owner.central
        owner.service.lock=threading.RLock()
        with patch('urllib.request.build_opener',return_value=fake):
            with patch('resident.foreign_inference_count',return_value=0):
                owner.tick()
            journal=owner.service.state.parent/'comfy-weight-preload'/(policy['operation_id']+'.json')
            self.assertEqual(json.loads(journal.read_text())['state'],'running')
            self.assertEqual(central_row['state'],'loading',
                {'phase':owner.policy_state,'intent':owner.intent,'observation':owner.observed,
                 'journal':json.loads(journal.read_text()),'activity':central_row})
            self.assertEqual(owner.central.observations[-1]['state'],'loading')
            central_row.update(state='unknown',active=1)
            central_row['policy_demand']=dict(policy,state='unknown')
            # Startup reconciliation may mark an interrupted local policy phase
            # unknown; only this exact dispatched preload may resume read-only.
            owner.policy_state['phase']='unknown'
            with patch('resident.foreign_inference_count',return_value=0):
                owner.tick()
        self.assertEqual(json.loads(journal.read_text())['state'],'complete',
            {'journal':json.loads(journal.read_text()),'history_reads':fake.history_reads,
             'observations':owner.central.observations})
        self.assertEqual(len(dispatches),1)
        self.assertEqual(fake.history_reads,2)
        self.assertEqual(len(owner.central.observations),2)
        self.assertEqual([row['state'] for row in owner.central.observations],['loading','ready'])
        self.assertIsNone(owner.central.observations[0].get('model_loaded'))
        self.assertIs(owner.central.observations[1].get('model_loaded'),True)
        self.assertEqual(owner.central.observations[1].get('profile_id'),'nx5-qwen21')
        self.assertEqual(owner.policy_state['phase'],'applied')
        owner.action.assert_not_called()
        owner.flint_action.assert_not_called()

    def test_preload_completion_requires_live_target_weights_and_exact_transition(self):
        owner, client = self.preload_owner(state='complete', loaded=False)
        policy = self.comfy_policy()
        transition = {'operation_id': policy['operation_id'], 'executor_id': policy['executor_id'],
                      'target_mode': 'comfy'}

        result = owner._ensure_weight_preload(policy, transition)

        self.assertEqual(result['state'], 'unknown')
        owner.profile_observation.assert_called_once_with({'mode': 'comfy', 'state': 'ready', 'active': 0})
        owner.policy_comfy_transition.return_value = False
        rejected = owner._ensure_weight_preload(policy, transition)
        self.assertEqual(rejected['state'], 'failed')
        self.assertEqual(client.ensure.call_count, 1)
        self.assertEqual(owner.report.call_args.args[0]['state'], 'loading')

    def test_preload_never_submits_while_the_physical_group_is_busy(self):
        owner, client = self.preload_owner()
        policy = self.comfy_policy()
        transition = {'operation_id': policy['operation_id'], 'executor_id': policy['executor_id'],
                      'target_mode': 'comfy'}
        owner.checked_activity.return_value['leases'] = [{'operation_id': 'other'}]

        result = owner._ensure_weight_preload(policy, transition)

        self.assertEqual(result['state'], 'unknown')
        client.ensure.assert_not_called()
        self.assertEqual(owner.report.call_args.args[0]['state'], 'loading')

    def test_preload_flag_is_part_of_demand_identity_and_requires_a_local_contract(self):
        owner = self.comfy_owner()
        owner.policy_profiles['comfy']['weight_preload'] = {'schema_version': 1}
        policy = self.comfy_policy()
        row = {'fresh': True, 'policy_demand': policy}
        self.assertEqual(owner.policy_demand(row), policy)
        changed = dict(policy, weight_preload_required=False)
        owner.policy_state = {'demand': policy, 'phase': 'transitioning'}
        with self.assertRaisesRegex(RuntimeError, 'identity_conflict'):
            owner.policy_demand({'fresh': True, 'policy_demand': changed})
        owner.policy_profiles['comfy'].pop('weight_preload')
        with self.assertRaisesRegex(RuntimeError, 'config_missing'):
            owner.policy_demand({'fresh': True, 'policy_demand': policy})

    def test_policy_without_required_preload_keeps_existing_comfy_transition(self):
        owner = self.comfy_owner()
        owner.policy_profiles['comfy']['weight_preload'] = {'schema_version': 1}
        policy = dict(self.comfy_policy(), weight_preload_required=False)
        owner.policy_comfy_transition = Mock(return_value=True)
        owner.checked_activity = Mock(return_value={'fresh': True, 'policy_demand': policy})
        self.assertTrue(owner.policy_demand({'fresh': True, 'policy_demand': policy}))
        self.assertFalse(owner._ensure_weight_preload(policy, {
            'operation_id': policy['operation_id'], 'executor_id': policy['executor_id'],
            'target_mode': 'comfy'})['state'] == 'complete')

    def test_real_preload_client_loads_isolated_vendored_package_without_path_or_pyc(self):
        import shutil
        source_vendor = Path(module.__file__).resolve().parents[1]/'minicpm/vendor/fleet_control'
        vendor = self.root/'repo/scripts/minicpm/vendor/fleet_control'
        vendor.mkdir(parents=True)
        for name in ('__init__.py', 'comfy_preload.py', 'weight_preload_protocol.py'):
            shutil.copyfile(source_vendor/name, vendor/name)
        graph = {'1': {'class_type': 'UNETLoader', 'inputs': {
                    'unet_name': 'qwen_image_2.1_int8_convrot.safetensors', 'weight_dtype': 'default'}},
                 'preload': {'class_type': 'SiyuanPreloadModel', 'inputs': {
                    'model': ['1', 0], 'authorization_json': '$AUTH'}}}
        graph_path = self.root/'preload-graph.json'
        graph_bytes = json.dumps(graph, sort_keys=True, separators=(',', ':')).encode()
        graph_path.write_bytes(graph_bytes)
        token_path = self.root/'central.token'
        key_path = self.root/'ticket.key'
        token_path.write_text('placeholder')
        key_path.write_text('placeholder')
        config = {'schema_version': 1, 'group_id': 'nx5-igpu-group', 'resource_id': 'nx5-igpu',
            'mode': 'comfy', 'profile_id': 'nx5-qwen21',
            'profile_version': 'qwen21-t2i-nx5-20261005', 'model_class': 'QwenImage21',
            'native_base': 'http://127.0.0.1:8188',
            'central': {'base_url': 'http://100.91.42.28:18292', 'token_file': str(token_path)},
            'ticket_key_file': str(key_path), 'graph_file': str(graph_path),
            'graph_sha256': hashlib.sha256(graph_bytes).hexdigest(),
            'budget': {'max_cuda_bytes': 3*1024**3, 'reserve_cuda_bytes': 4*1024**3,
                'min_ram_available_bytes': 9*1024**3, 'min_loaded_bytes': 256*1024**2,
                'unified_memory': True}}
        owner = self.comfy_owner()
        owner.service.repo = self.root/'repo'
        owner.service.state = self.root/'state/jobs'
        owner.policy_profiles['comfy']['weight_preload'] = config
        policy = dict(self.comfy_policy(), weight_preload_required=True)
        original_path = list(sys.path)
        client = owner.weight_preload_client(policy)
        self.assertEqual(client.config, config)
        self.assertEqual(client.journal_path, self.root/'state/comfy-weight-preload'/
                         (policy['operation_id']+'.json'))
        self.assertEqual(sys.path, original_path)
        self.assertFalse(list(vendor.rglob('__pycache__')))

    @staticmethod
    def comfy_payload(models=(), *, busy=False, observed_at=None):
        import time
        rows = [{'model_class': name, 'device_kind': device, 'loaded_bytes': size}
                for name, device, size in models]
        return {'schema_version': 1, 'source': 'comfy.current_loaded_models',
                'observed_at': time.time() if observed_at is None else observed_at,
                'models': rows, 'count': len(rows), 'loaded_bytes': sum(row['loaded_bytes'] for row in rows),
                'model_loaded': any(row['loaded_bytes'] > 0 for row in rows), 'busy': busy}

    def test_comfy_observation_uses_real_cuda_qwen_weights_and_local_endpoint(self):
        owner = self.comfy_owner()
        payload = self.comfy_payload([('QwenImage21', 'cuda', 1234)])
        paths = []
        owner.request = lambda path: paths.append(path) or payload

        result = owner.profile_observation({'mode': 'comfy', 'state': 'ready', 'active': 0})

        self.assertEqual(result['model_loaded'], True)
        self.assertEqual(result['profile_id'], 'nx5-qwen21')
        self.assertEqual(result['profile_version'], 'qwen21-t2i-nx5-20261005')
        self.assertEqual(paths, ['/siyuan/loaded-models'])

    def test_comfy_empty_idle_is_false_but_busy_empty_is_unknown(self):
        owner = self.comfy_owner()
        payloads = [self.comfy_payload(), self.comfy_payload(busy=True)]
        owner.request = lambda _path: payloads.pop(0)

        idle = owner.profile_observation({'mode': 'comfy', 'state': 'ready', 'active': 0})
        busy = owner.profile_observation({'mode': 'comfy', 'state': 'ready', 'active': 1})

        self.assertIs(idle['model_loaded'], False)
        self.assertIsNone(busy['model_loaded'])

    def test_comfy_cpu_only_or_unrelated_cuda_weights_do_not_prove_target_loaded(self):
        owner = self.comfy_owner()
        payloads = [
            self.comfy_payload([('QwenImage21', 'cpu', 1234)]),
            self.comfy_payload([('AutoencoderKL', 'cuda', 5678)]),
        ]
        owner.request = lambda _path: payloads.pop(0)

        cpu = owner.profile_observation({'mode': 'comfy', 'state': 'ready', 'active': 0})
        unrelated = owner.profile_observation({'mode': 'comfy', 'state': 'ready', 'active': 0})

        self.assertIs(cpu['model_loaded'], False)
        self.assertIs(unrelated['model_loaded'], False)

    def test_comfy_malformed_stale_or_unreachable_weight_evidence_stays_unknown(self):
        owner = self.comfy_owner()
        owner.request = lambda _path: {'schema_version': 1, 'source': 'wrong'}
        malformed = owner.profile_observation({'mode': 'comfy', 'state': 'ready', 'active': 0})
        owner.request = lambda _path: self.comfy_payload(
            [('QwenImage21', 'cuda', 1234)], observed_at=0)
        stale = owner.profile_observation({'mode': 'comfy', 'state': 'ready', 'active': 0})
        owner.request = Mock(side_effect=TimeoutError('do not expose endpoint details'))
        unavailable = owner.profile_observation({'mode': 'comfy', 'state': 'ready', 'active': 0})

        self.assertIsNone(malformed['model_loaded'])
        self.assertIsNone(stale['model_loaded'])
        self.assertIsNone(unavailable['model_loaded'])

    def test_llm_and_flint_observations_keep_their_existing_loaded_semantics(self):
        owner = self.comfy_owner()
        self.model.model_revision = 'llm-version'
        self.model.health = Mock(return_value={'model_loaded': True})
        owner.policy_profiles['llm'] = {'profile_id': 'llm-profile', 'version': 'llm-version'}
        owner.policy_profiles['flint'] = {'profile_id': 'flint-profile', 'version': 'flint-version'}
        owner.flint_runtime = Mock(return_value={'ready': True})

        llm = owner.profile_observation({'mode': 'llm', 'state': 'ready', 'active': 0})
        flint = owner.profile_observation({'mode': 'flint', 'state': 'ready', 'active': 0})

        self.assertIs(llm['model_loaded'], True)
        self.assertIs(flint['model_loaded'], True)

    def test_vendored_shared_preload_helpers_match_pinned_source_lock(self):
        vendor = Path(__file__).resolve().parents[1] / 'scripts/minicpm/vendor'
        lock = json.loads((vendor/'source-lock.json').read_text())
        self.assertEqual(lock['repository'], 'redyuan43/siyuan-media-gateway')
        self.assertEqual(lock['commit'], '00420e8ada805f35a91d31c796b11b2c9cfe4284')
        self.assertTrue({'scripts/minicpm/vendor/fleet_control/comfy_preload.py',
                         'scripts/minicpm/vendor/fleet_control/weight_preload_protocol.py',
                         'scripts/minicpm/vendor/weight_preload_node/__init__.py'}.issubset(lock['files']))
        for path, metadata in lock['files'].items():
            self.assertEqual(hashlib.sha256((Path(__file__).resolve().parents[1]/path).read_bytes()).hexdigest(),
                             metadata['sha256'])

    def test_launch_keeps_target_q8_and_sets_explicit_draft_q8_cache(self):
        root = self.root/'runtime'
        model_dir = root/'models'
        model_dir.mkdir(parents=True)
        binary = root/'llama-nx5-chat'
        binary.write_bytes(b'cpu fixture')
        template = self.root/'template.jinja'
        template.write_text('template')
        (model_dir/'stripped.gguf').write_bytes(b'main model')
        (model_dir/'dspark-Q4_K_M.gguf').write_bytes(b'draft model')
        cfg = {
            'model_dir':'models', 'chat_template':str(template),
            'chat_template_sha256':hashlib.sha256(b'template').hexdigest(),
            'library_dir':'libs', 'library_sha256':{},
            'model_file_sizes':{'stripped.gguf':10, 'dspark-Q4_K_M.gguf':11},
            'parameters':{'ctx':65536, 'window':176, 'bank':64, 'io_threads':8,
                'rows':8, 'offload_min_batch':32, 'seed':42, 'ctx_checkpoints':2,
                'ubatch':8, 'draft':7, 'min_available_mib':768,
                'draft_cache_k':'q8_0', 'draft_cache_v':'q8_0'},
        }
        runtime = {
            'binary':'llama-nx5-chat',
            'binary_sha256':hashlib.sha256(b'cpu fixture').hexdigest(),
            'libraries':{},
        }
        cfg['model_revision'] = 'a64932ec615e1463158129feaf2f0752d0c07a2e'
        cfg['revision'] = cfg['profile_digest'] = profile_digest(cfg, runtime)
        (self.root/'configs').mkdir()
        (self.root/'configs/nx5-minicpm-service.json').write_text(json.dumps(cfg))
        (self.root/'configs/nx5-chat-runtime.json').write_text(json.dumps(runtime))
        self.model.config = {'root':str(root), 'state':str(self.root/'jobs')}

        command, child_env, _ = self.model.launch_config()

        self.assertEqual(command[command.index('--cache-type-k')+1], 'q8_0')
        self.assertEqual(command[command.index('--cache-type-v')+1], 'q8_0')
        self.assertEqual(command[command.index('--cache-type-k-draft')+1], 'q8_0')
        self.assertEqual(command[command.index('--cache-type-v-draft')+1], 'q8_0')
        self.assertEqual(command[command.index('-c')+1], '65536')
        self.assertEqual(command[command.index('--spec-draft-n-max')+1], '7')
        original_path = os.environ.get('PATH')
        with patch.dict(os.environ, {'NX5_NATIVE_BACKTRACE_DIRECT':'1'}):
            _, diagnostic_env, _ = self.model.launch_config()
            self.assertEqual(diagnostic_env['PATH'], '/nonexistent')
            self.assertNotIn('NX5_NATIVE_BACKTRACE_DIRECT', diagnostic_env)
            self.assertIsNone(shutil.which('gdb', path=diagnostic_env['PATH']))
            self.assertIsNone(shutil.which('lldb', path=diagnostic_env['PATH']))
        self.assertEqual(os.environ.get('PATH'), original_path)
        self.assertEqual(child_env.get('PATH'), original_path)

        cfg['parameters']['draft_cache_k'] = 'f16'
        (self.root/'configs/nx5-minicpm-service.json').write_text(json.dumps(cfg))
        with self.assertRaisesRegex(RuntimeError, 'profile_digest_mismatch'):
            self.model.launch_config()

    def test_reservation_and_lease_have_distinct_digests(self):
        central = object.__new__(CentralLease)
        central.group = '/group'
        central.observe = Mock()
        record = {}
        def api(method, path, body=None, controller=False):
            if path.endswith('/activity'):
                return {'leases': [], 'pending': [], 'transition': None}
            if path.endswith('/reservations'):
                return {'status':'held'}
            return {'status':'held','operation_id':record['operation_id'],'request_digest':'canonical-lease-digest'}
        central.api = Mock(side_effect=api)
        with patch('resident.require_idle'):
            result = central.acquire({'x':1},'rev',lambda value: record.update(value))
        self.assertEqual(result['lease_digest'],'canonical-lease-digest')
        self.assertNotEqual(result['request_digest'],result['lease_digest'])
        central.api = Mock(return_value={'status':'released'})
        with patch('resident.require_idle'):
            central.release(result)
        self.assertEqual(central.api.call_args_list[0].args[2]['request_digest'],'canonical-lease-digest')

    def test_two_requests_reuse_process_and_retained_usage(self):
        child = Mock(pid=456, stdin=io.StringIO())
        child.poll.return_value = None
        self.model.child, self.model.ready = child, True
        final = {'type': 'final', 'usage': {'prompt_tokens_details': {'cached_tokens': 8000}}}
        self.model.events.put(final)
        self.model.events.put(final)
        with patch('resident.memory_available', return_value=1500), patch.object(self.model, 'launch_config') as launch:
            for n in range(2):
                event, metrics = self.model.generate({'messages': []}, 64, self.root/f'{n}.out', lambda pid: self.assertEqual(pid,456))
                self.assertIs(event, final)
                self.assertFalse(metrics['cold_start'])
                self.assertTrue(metrics['model_loaded_after'])
                self.assertEqual(metrics['lease_status'], 'held')
            launch.assert_not_called()
        self.assertEqual(len(child.stdin.getvalue().splitlines()), 2)

    def test_unconfirmed_receipt_blocks_new_process(self):
        self.model.save({'status': 'held', 'operation_id': 'old'})
        recovered = ResidentModel(self.root, {'state': str(self.root/'jobs')})
        with patch('resident.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                recovered.ensure_loaded()
            spawn.assert_not_called()

    def test_old_guard_cannot_close_replacement_process(self):
        old, current = Mock(), Mock()
        self.model.child, self.model.ready = current, True
        self.model.close(expected_child=old)
        current.terminate.assert_not_called()
        self.assertTrue(self.model.ready)

    def test_native_abort_backtrace_is_captured_without_recording_plain_output(self):
        child = Mock(stdout=io.StringIO(
            'Attaching to process 123\n'
            'Thread 1 (Thread 0xffff):\n'
            '#0  ggml_backend_buffer_get_type (buffer=0x0 prompt=secret-user-text)\n'
            '#1  llama_decode () at llama-context.cpp:1\n'
            '[Inferior 1 (process 123) detached]\n'
        ))
        events = queue.Queue()
        diagnostic = self.root/'native.backtrace'

        ResidentModel.read_events(child, events, diagnostic)

        self.assertEqual(events.get_nowait(), {'type':'error','code':'invalid_native_output'})
        self.assertIsNone(events.get_nowait())
        captured = diagnostic.read_text()
        self.assertIn('ggml_backend_buffer_get_type', captured)
        self.assertIn('llama-context.cpp:1', captured)
        self.assertNotIn('secret-user-text', captured)
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)

    def test_backtrace_capture_bounds_long_lines_and_file_total_then_delivers_json(self):
        oversized_header = 'Attaching to process 123 ' + ('x' * (128 * 1024)) + '\n'
        oversized_frame = '#0  function (arg=' + ('secret' * (16 * 1024)) + ')\n'
        payload = (oversized_header + oversized_frame +
                   '[Inferior 1 (process 123) detached]\n' +
                   '{"type":"ready","model":"minicpm-64k"}\n')
        child = Mock(stdout=io.StringIO(payload))
        events = queue.Queue()
        diagnostic = self.root/'native.backtrace'

        ResidentModel.read_events(child, events, diagnostic)

        self.assertEqual(events.get_nowait(), {'type':'error','code':'invalid_native_output'})
        self.assertEqual(events.get_nowait(), {'type':'ready','model':'minicpm-64k'})
        self.assertIsNone(events.get_nowait())
        self.assertLessEqual(diagnostic.stat().st_size, 64 * 1024)
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('secret', diagnostic.read_text())

        # A second capture for the same operation must not grow an O_APPEND file.
        repeated = Mock(stdout=io.StringIO(
            'Attaching to process 456\n' + ('#0  frame at file.cpp:1\n' * (5000)) +
            '[Inferior 1 (process 456) detached]\n'))
        ResidentModel.read_events(repeated, queue.Queue(), diagnostic)
        self.assertLessEqual(diagnostic.stat().st_size, 64 * 1024)

    def test_non_json_preamble_does_not_hide_later_gdb_backtrace(self):
        events = queue.Queue()
        class InspectBeforeAttachment(io.StringIO):
            def readline(self, size=-1):
                line = super().readline(size)
                if 'Attaching to process 789' in line:
                    self.events_were_empty = self.events.empty()
                return line
        stream = InspectBeforeAttachment(
            'native diagnostic preamble\n'
            '\x1b[1;32mAttaching to process 789\x1b[0m\n'
            '#0  speculative_checkpoint () at server-context.cpp:2661\n'
            '[Inferior 1 (process 789) detached]\n'
            '{"type":"ready","mode":"off"}\n'
        )
        stream.events = events
        child = Mock(stdout=stream)
        diagnostic = self.root/'native.backtrace'

        ResidentModel.read_events(child, events, diagnostic)

        self.assertEqual(events.get_nowait(), {'type':'error','code':'invalid_native_output'})
        self.assertEqual(events.get_nowait(), {'type':'ready','mode':'off'})
        self.assertIsNone(events.get_nowait())
        self.assertTrue(stream.events_were_empty)
        self.assertIn('speculative_checkpoint', diagnostic.read_text())
        self.assertNotIn('native diagnostic preamble', diagnostic.read_text())

    def test_valid_large_protocol_event_is_not_limited_by_diagnostic_file_cap(self):
        content = 'x' * (100 * 1024)
        child = Mock(stdout=io.StringIO(json.dumps({'type':'delta','content':content}) + '\n'))
        events = queue.Queue()

        ResidentModel.read_events(child, events)

        self.assertEqual(events.get_nowait(), {'type':'delta','content':content})
        self.assertIsNone(events.get_nowait())

    def test_native_text_lines_are_not_written_to_diagnostic_capture(self):
        child = Mock(stdout=io.StringIO('not-json plain generated answer\n'))
        events = queue.Queue()
        diagnostic = self.root/'native.backtrace'

        ResidentModel.read_events(child, events, diagnostic)

        self.assertEqual(events.get_nowait(), {'type':'error','code':'invalid_native_output'})
        self.assertIsNone(events.get_nowait())
        self.assertFalse(diagnostic.exists())

    def test_release_follows_process_exit_and_is_idempotent(self):
        child, central = Mock(), Mock()
        child.poll.return_value = None
        self.model.child, self.model.central = child, central
        self.model.record = {'status': 'held'}
        def release(record):
            child.wait.assert_called_once()
        central.release.side_effect = release
        self.model.close()
        child.poll.return_value = 0
        self.model.close()
        central.release.assert_called_once()
        self.assertEqual(self.model.record['status'], 'released')

    def test_shutdown_cannot_reload(self):
        self.model.close(permanent=True)
        with patch('resident.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'service_stopped'):
                self.model.ensure_loaded()
            spawn.assert_not_called()

    def test_context_limit_does_not_discard_cache(self):
        self.model.child = Mock(pid=456, stdin=io.StringIO())
        self.model.child.poll.return_value = None
        self.model.ready = True
        self.model.events.put({'type':'error','code':'context_length_exceeded'})
        with patch('resident.memory_available', return_value=1500), patch.object(self.model,'close') as close:
            with self.assertRaisesRegex(ValueError, 'context_length_exceeded'):
                self.model.generate({},64,self.root/'result.out',lambda pid:None)
            close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
