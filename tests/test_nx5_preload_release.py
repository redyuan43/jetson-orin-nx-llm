import ast
import base64
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('nx5_preload_release', ROOT/'scripts/nx5/publish-preload.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class PreloadReleaseTests(unittest.TestCase):
    def test_config_and_owner_projection_pin_the_expected_model_and_budgets(self):
        config = module.config_for_nx5('a'*64)
        existing = {'managed_comfy': True, 'limit': 7, 'policy_profiles': {
            'llm': {'profile_id': 'nx5-ssd-minicpm', 'version': 'llm'},
            'comfy': {'profile_id': 'nx5-qwen21', 'version': 'qwen21-t2i-nx5-20261005'}}}
        merged = module.merge_owner_config(existing, config)
        self.assertEqual(merged['policy_profiles']['comfy']['weight_preload'], config)
        self.assertEqual(merged['policy_profiles']['llm'], existing['policy_profiles']['llm'])
        self.assertEqual(merged['limit'], 7)
        self.assertNotIn('weight_preload', existing['policy_profiles']['comfy'])
        self.assertEqual(config['model_class'], 'QwenImage21')
        self.assertEqual(config['budget']['max_cuda_bytes'], 3*1024**3)
        self.assertEqual(config['budget']['reserve_cuda_bytes'], 4*1024**3)
        self.assertEqual(config['budget']['min_loaded_bytes'], 256*1024**2)
        self.assertTrue(config['budget']['unified_memory'])

    def test_existing_token_env_is_extracted_without_echoing_or_mutating_source(self):
        credentials = b"OTHER=value\nNX5_CENTRAL_TOKEN='safe.token-123'\n"
        token = module.token_from_credentials(credentials, 'NX5_CENTRAL_TOKEN')
        self.assertEqual(token, b'safe.token-123')
        self.assertEqual(credentials, b"OTHER=value\nNX5_CENTRAL_TOKEN='safe.token-123'\n")
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            module.token_from_credentials(credentials+b'NX5_CENTRAL_TOKEN=second\n', 'NX5_CENTRAL_TOKEN')

    def test_owner_and_node_private_configs_share_contract_and_secrets_are_create_only(self):
        existing = {'managed_comfy': True, 'policy_profiles': {
            'comfy': {'profile_id': 'nx5-qwen21', 'version': 'qwen21-t2i-nx5-20261005'}}}
        artifacts = module.config_artifacts(existing, 'b'*64, home='/tmp/nx5-fixture')
        owner = json.loads(artifacts['owner_config'])['policy_profiles']['comfy']['weight_preload']
        node = json.loads(artifacts['node_config'])
        self.assertEqual(owner, node)
        self.assertEqual(len(artifacts['ticket_key']), 43)
        self.assertNotIn(artifacts['ticket_key'], artifacts['owner_config'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'private/token'
            digest = module.atomic_private_file(path, b'opaque-token')
            self.assertEqual(digest, module.sha(b'opaque-token'))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(module.atomic_private_file(path, b'opaque-token',
                             expected_sha256=digest, allow_existing=True), digest)
            with self.assertRaisesRegex(ValueError, 'content_conflict'):
                module.atomic_private_file(path, b'different', expected_sha256=digest, allow_existing=True)

    def test_package_audits_fixed_source_and_rejects_unrelated_delta(self):
        namespace = {'__name__': '_nx5_deploy_files'}
        exec(compile((ROOT/'scripts/nx5/deploy.py').read_bytes(), 'scripts/nx5/deploy.py', 'exec'), namespace)
        source_names = sorted(set(namespace['FILES']) | module.LOCAL_PRELOAD_FILES |
                              {'scripts/nx5/deploy.py', 'scripts/nx5/publish-preload.py'})
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'cpu@example.invalid'], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'CPU fixture'], check=True)
            for name in source_names:
                path = repo/name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT/name, path)
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'baseline'], check=True)
            baseline = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            old_baseline = module.BASELINE
            module.BASELINE = baseline
            try:
                owner = repo/'scripts/nx5/resident.py'
                owner.write_bytes(owner.read_bytes()+b'\n# CPU candidate\n')
                subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
                subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'candidate'], check=True)
                revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
                bundle = module.package(repo, revision)
                self.assertEqual(bundle['manifest']['source_commit'], revision)
                self.assertEqual(bundle['manifest']['vendor_commit'], module.VENDOR_COMMIT)
                self.assertEqual(bundle['manifest']['graph_sha256'],
                                 module.sha(module.canonical(json.loads((ROOT/module.GRAPH).read_text()))))
                self.assertIn('scripts/minicpm/vendor/weight_preload_node/__init__.py', bundle['files'])

                # Exercise the real consumer against a materialized package, not
                # just the publisher's manifest dictionary. Service.__init__ reads
                # release.json and requires the legacy release/commit header.
                materialized = repo/'materialized-release'
                for name, row in bundle['files'].items():
                    target = materialized/name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(base64.b64decode(row['content']))
                (materialized/'release.json').write_text(json.dumps(bundle['manifest']))
                service_path = materialized/'scripts/nx5/service.py'
                sys.path.insert(0, str(service_path.parent))
                try:
                    service_spec = importlib.util.spec_from_file_location(
                        'nx5_service_from_candidate', service_path)
                    service_module = importlib.util.module_from_spec(service_spec)
                    service_spec.loader.exec_module(service_module)
                    with patch.object(service_module, 'ResidentModel',
                                      return_value=SimpleNamespace(manager=None)):
                        service = service_module.Service({'state': str(materialized/'state')})
                finally:
                    sys.path.remove(str(service_path.parent))
                hashes = bundle['manifest']['files']
                self.assertEqual(bundle['manifest']['release'], 'nx5-ssd-llm-'+revision[:12])
                self.assertEqual(bundle['manifest']['commit'], module.sha(
                    json.dumps(hashes, sort_keys=True).encode()))
                self.assertEqual(service.release, {
                    'release': bundle['manifest']['release'],
                    'commit': bundle['manifest']['commit'],
                    'source_commit': revision,
                })

                unrelated = repo/'unrelated.txt'
                unrelated.write_text('not owned\n')
                subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
                subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'unrelated'], check=True)
                bad = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
                with self.assertRaisesRegex(ValueError, 'out_of_scope'):
                    module.package(repo, bad)
            finally:
                module.BASELINE = old_baseline

    def test_source_files_executes_fixed_deploy_with_file_context_without_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'cpu@example.invalid'], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'CPU fixture'], check=True)
            deploy = repo/'scripts/nx5/deploy.py'
            deploy.parent.mkdir(parents=True)
            deploy.write_text("from pathlib import Path\nFILES = [Path(__file__).name]\ndef main(): raise AssertionError('main executed')\n")
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'candidate'], check=True)
            revision = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            self.assertIn('deploy.py', module.source_files(repo, revision))

    def test_remote_phase_guard_refuses_reapplying_unknown_or_completed_release(self):
        self.assertEqual(module.next_phase('captured', 'prepare'), 'prepared')
        self.assertEqual(module.next_phase('prepared', 'apply'), 'applying')
        for phase in ('applying', 'unknown', 'activated', 'verified'):
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, 'inspect_only'):
                module.next_phase(phase, 'apply')

    def test_apply_cli_passes_remote_baseline_used_by_remote_owner_idle(self):
        operation = 'nx5-weight-preload-publish-20261025-002'
        source = 'a'*40
        now = module.time.time()
        central = {'resource': {'resource_id':'nx5-igpu','capacity':0,'occupied':0,'revision':12},
            'leases': [], 'group': {'group_id':'nx5-igpu-group','resources':['nx5-igpu'],
                'mode':'comfy','state':'ready','active':0,'transition':None,'pending':[],
                'policy_demand':None,'observed_at':now},
            'public': {'mode':'comfy','state':'ready','active':0,'fresh':True,
                'transition_pending':False,'policy_demand':None,'observed_at':now}}
        flint_unit = {'ActiveState':'inactive','MainPID':'0','InvocationID':''}
        remote_baseline = {'flint_unit':flint_unit}
        manifest = {'source_commit':source,'files':{},'vendor_commit':module.VENDOR_COMMIT,
                    'graph_sha256':'c'*64}
        bundle = {'manifest':manifest,'files':{},
                  'manifest_sha256':module.sha(module.canonical(manifest))}

        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp)/'native-preload-release-plan.json'
            plan_path.write_text(json.dumps({'schema_version':1,'operation':operation,
                'maintenance_revision':12,'source_commit':source,
                'manifest_sha256':bundle['manifest_sha256'],'graph_sha256':'c'*64,
                'central_baseline':central,'remote_baseline':remote_baseline,'phase':'prepared'}))
            captured = {}

            def fake_call_remote(payload):
                captured['payload'] = payload
                tree = ast.parse(module.REMOTE_PRELOAD)
                owner_idle = next(node for node in tree.body
                                  if isinstance(node, ast.FunctionDef) and node.name == 'owner_idle')
                remote = ast.Module(body=[owner_idle], type_ignores=[])
                namespace = {'P':payload,
                    'need':lambda condition, code: None if condition else (_ for _ in ()).throw(RuntimeError(code))}
                exec(compile(remote, 'REMOTE_PRELOAD.owner_idle', 'exec'), namespace)
                namespace['owner_idle']({'owner_unit':{'ActiveState':'active','MainPID':'123'},
                    'owner':{'state':'ready','mode':'comfy','active':0,'model_loaded':False,
                             'resident_pid':None},
                    'jobs':{'nonterminal_count':0},'queue':{'running':0,'pending':0},
                    'comfy_unit':{'ActiveState':'active'},'new_artifacts':{'private':False},
                    'flint_unit':flint_unit})
                return json.dumps({'phase':'verified'})

            with patch.object(module, 'package', return_value=bundle), \
                 patch.object(module, 'central_snapshot', return_value=central), \
                 patch.object(module, 'call_remote', side_effect=fake_call_remote), \
                 patch.object(sys, 'argv', ['publish-preload.py','--action','apply',
                    '--commit',source,'--operation',operation,'--maintenance-revision','12',
                    '--plan',str(plan_path)]):
                module.main()

            self.assertEqual(captured['payload']['baseline'], remote_baseline)

    def test_generated_remote_program_is_valid_python_for_fixed_payload(self):
        source = module.remote_program({'action':'capture','operation':'nx5-preload-test',
            'source_commit':'a'*40,'maintenance_revision':12})
        compile(source, 'nx5 generated remote publisher', 'exec')

    def test_central_guard_requires_exact_paused_resource_and_fresh_idle_policy(self):
        now = 1000.0
        snapshot = {'resource': {'resource_id': 'nx5-igpu', 'capacity': 0,
                    'occupied': 0, 'revision': 17},
            'leases': [], 'group': {'group_id': 'nx5-igpu-group', 'mode': 'comfy',
                'state': 'ready', 'active': 0, 'transition': None, 'pending': [],
                'observed_at': now},
            'public': {'mode': 'comfy', 'state': 'ready', 'active': 0, 'fresh': True,
                'transition_pending': False, 'observed_at': now}}
        self.assertTrue(module.validate_central_snapshot(snapshot, operation='nx5-preload-test',
                                                         revision=17, now=now))
        self.assertTrue(module.validate_central_snapshot(snapshot,
            operation='nx5-weight-preload-publish-20261025-002', revision=17, now=now))
        refreshed = json.loads(json.dumps(snapshot))
        refreshed['group']['observed_at'] = now + 1
        refreshed['public']['observed_at'] = now + 1
        self.assertTrue(module.central_baseline_matches(snapshot, refreshed))
        for path, change in [('revision', {'resource': {'revision': 18}}),
                             ('capacity', {'resource': {'capacity': 1}}),
                             ('activity', {'group': {'active': 1}}),
                             ('lease', {'leases': [{'status': 'held'}]}),
                             ('stale', {'group': {'observed_at': 900.0}}),
                             ('transition', {'group': {'transition': {'operation_id': 'other'}}})]:
            bad = json.loads(json.dumps(snapshot))
            bad.update(change) if path == 'lease' else bad.update({
                'resource': {**bad['resource'], **change.get('resource', {})},
                'group': {**bad['group'], **change.get('group', {})}})
            with self.subTest(path=path), self.assertRaises(ValueError):
                module.validate_central_snapshot(bad, operation='nx5-preload-test', revision=17, now=now)

    def test_central_baseline_compare_ignores_only_refresh_timestamps(self):
        baseline = {'resource': {'resource_id':'nx5-igpu','capacity':0,'occupied':0,'revision':17},
            'leases': [], 'group': {'group_id':'nx5-igpu-group','mode':'comfy','state':'ready','active':0,
                'transition':None,'pending':[],'policy_demand':None,'observed_at':1000},
            'public': {'mode':'comfy','state':'ready','active':0,'fresh':True,'transition_pending':False,
                'policy_demand':None,'observed_at':1000}}
        refreshed = json.loads(json.dumps(baseline))
        refreshed['group']['observed_at'] = refreshed['public']['observed_at'] = 1001
        self.assertTrue(module.central_baseline_matches(baseline, refreshed))
        refreshed['group']['pending'] = [{'status':'waiting'}]
        self.assertFalse(module.central_baseline_matches(baseline, refreshed))

    def test_central_snapshot_filters_only_active_nx5_leases_using_live_api_shape(self):
        now = 2000.0
        resource = {'resource_id':'nx5-igpu','capacity':0,'occupied':0,'revision':17}
        group = {'group_id':'nx5-igpu-group','resources':['nx5-igpu'],'mode':'comfy',
            'state':'ready','active':0,'transition':None,'pending':None,'policy_demand':None,
            'observed_at':now}
        public = {'groups':[{'group_id':'nx5-igpu-group','actual':{
            'mode':'comfy','state':'ready','active':0,'fresh':True,
            'transition_pending':False,'observed_at':now,'policy_demand':None}}]}
        lease_rows = ([{'status':'released','resources':[{'resource_id':'nx5-igpu','units':1}]}
                       for _ in range(31)] +
                      [{'status':'held','resources':[{'resource_id':'other-gpu','units':1}]}])
        responses = {'/console/admin/v1/resources':{'resources':[resource],'groups':[group]},
            '/console/admin/v1/leases':{'leases':lease_rows},'/agent/device-policies.json':public}
        class Response:
            def __init__(self,value):self.raw=json.dumps(value).encode()
            def __enter__(self):return self
            def __exit__(self,*_args):return False
            def read(self,_limit=-1):return self.raw
        class Opener:
            def open(self,request,timeout=0):
                from urllib.parse import urlsplit
                return Response(responses[urlsplit(request.full_url).path])
        with tempfile.TemporaryDirectory() as tmp:
            token=Path(tmp)/'admin.key';token.write_text('fixture-only-token')
            with patch.object(module.urllib.request,'build_opener',return_value=Opener()):
                snapshot=module.central_snapshot(token,now=now)
            self.assertEqual(snapshot['leases'],[])
            self.assertTrue(module.validate_central_snapshot(snapshot,
                operation='nx5-weight-preload-maintenance-20261025-001',revision=17,now=now))
            lease_rows.append({'status':'held','resources':[{'resource_id':'nx5-igpu','units':1}]})
            with patch.object(module.urllib.request,'build_opener',return_value=Opener()):
                busy=module.central_snapshot(token,now=now)
            with self.assertRaisesRegex(ValueError,'central_leases_not_empty'):
                module.validate_central_snapshot(busy,
                    operation='nx5-weight-preload-maintenance-20261025-001',revision=17,now=now)

    def test_comfy_dropin_adds_only_pinned_vendor_and_private_config(self):
        text = module.comfy_dropin('/home/nx/.local/share/nx5-ssd-llm/current',
                                   '/home/nx/.config/nx5-ssd-llm/weight-preload.json')
        self.assertIn('PYTHONPATH=/home/nx/.local/share/nx5-ssd-llm/current/scripts/minicpm/vendor', text)
        self.assertIn('SIYUAN_WEIGHT_PRELOAD_CONFIG=/home/nx/.config/nx5-ssd-llm/weight-preload.json', text)
        self.assertNotIn('ExecStart=', text)
        self.assertNotIn('siyuan-flint', text)
        self.assertIn('SendSIGKILL=no', text)
        self.assertIn('TimeoutStopSec=infinity', text)

    def test_capability_receipt_is_exact_profile_graph_budget_and_scope(self):
        config = module.config_for_nx5('c'*64)
        self.assertEqual(module.capability_for(config), {
            'schema_version': 1, 'group_id': 'nx5-igpu-group', 'profile_id': 'nx5-qwen21',
            'profile_version': 'qwen21-t2i-nx5-20261005', 'graph_sha256': 'c'*64,
            'model_class': 'QwenImage21', 'budget': config['budget'],
            'scope': 'main_model_partial_cuda', 'inference_allowed': False})

    def test_remote_reads_owner_on_its_configured_tailnet_bind_and_comfy_loopback(self):
        self.assertIn("http://100.118.44.27:18347/healthz", module.REMOTE_PRELOAD)
        self.assertIn("http://127.0.0.1:8188/queue", module.REMOTE_PRELOAD)

    def test_capture_is_read_only_and_can_record_a_blocked_baseline(self):
        capture = module.REMOTE_PRELOAD.split("if action=='capture':", 1)[1].split(
            "if action=='prepare':", 1)[0]
        self.assertNotIn('owner_idle(row)', capture)
        self.assertIn("print(json.dumps(row,sort_keys=True))", capture)

    def test_remote_release_hash_audit_and_create_only_secret_write_are_executable(self):
        prefix = module.REMOTE_PRELOAD.split("if action=='capture':", 1)[0]
        prefix = prefix.replace("P=PAYLOAD; action=P['action'];",
            "P={'action':'test','operation':'nx5-preload-test','source_commit':'a'*40}; action=P['action'];", 1)
        namespace = {}
        exec(compile(prefix, 'nx5 remote release helpers', 'exec'), namespace)
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp)/'release'
            release.mkdir()
            file = release/'runtime.py'
            file.write_bytes(b'fixed bytes')
            manifest = {'files': {'runtime.py': module.sha(b'fixed bytes')}}
            self.assertTrue(namespace['verify_release'](release, manifest))
            file.write_bytes(b'changed bytes')
            with self.assertRaisesRegex(RuntimeError, 'release_file_hash_mismatch'):
                namespace['verify_release'](release, manifest)
            secret = Path(tmp)/'private/token'
            namespace['atomic'](secret, b'one', 0o600, create_only=True)
            with self.assertRaises(FileExistsError):
                namespace['atomic'](secret, b'two', 0o600, create_only=True)
            self.assertEqual(secret.read_bytes(), b'one')

    def test_remote_verification_pins_live_restart_environment_jobs_and_old_dropins(self):
        for required in (
                "health=get('http://100.118.44.27:18347/healthz')",
                "active_manifest==P['manifest']",
                "proc_environment(comfy_pid)==expected_env",
                "row['jobs']==P['baseline']['jobs']",
                "after.get('InvocationID')!=before.get('InvocationID')",
                "comfy_existing_dropin_changed",
                "comfy_dropin_not_loaded",
                "verify_release(resolved,manifest)"):
            with self.subTest(required=required):
                self.assertIn(required, module.REMOTE_PRELOAD)

    def test_restart_allowlist_excludes_all_processes_owned_by_only_two_units(self):
        rows = [{'pid':1,'unit':'nx5-ssd-llm.service'}, {'pid':2,'unit':'siyuan-image-comfy.service'},
                {'pid':3,'unit':'siyuan-image-comfy.service'}, {'pid':4,'unit':'siyuan-nx5-flint-ingress.service'}]
        self.assertEqual(module.unaffected_processes(rows),
            [{'pid':4,'unit':'siyuan-nx5-flint-ingress.service'}])


if __name__ == '__main__':
    unittest.main()
