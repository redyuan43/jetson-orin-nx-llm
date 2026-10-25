import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    'nx5_deploy', Path(__file__).resolve().parents[1] / 'scripts/nx5/deploy.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DeploymentConfigurationTests(unittest.TestCase):
    def test_publisher_bundle_includes_the_locked_shared_residency_parser(self):
        self.assertTrue({
            'scripts/minicpm/vendor/mode_residency.py',
            'scripts/minicpm/vendor/comfy_residency/__init__.py',
            'scripts/minicpm/vendor/source-lock.json',
        }.issubset(set(module.FILES)))

    def test_flint_recovery_remote_can_inspect_barrier_after_owner_stop(self):
        source = module.REMOTE_FLINT_RECOVERY.replace('PAYLOAD', '{}', 1)
        compile(inspect.getsource(module.validate_flint_recovery_snapshot) + '\n' + source,
                '<flint-recovery-publisher>', 'exec')
        self.assertIn('def snapshot(owner_required=True):', source)
        self.assertIn('after=snapshot(owner_required=False)', source)
        self.assertIn("owner_required else None", source)
        self.assertIn("expected_config_sha256", source)
        self.assertIn("expected_unit_sha256", source)
        self.assertIn("pathlib,re,shlex", source)

    def test_flint_recovery_requires_exact_live_gate_request_and_central_transition(self):
        operation = 'op_' + 'a' * 32
        source = 'b' * 40
        profile = 'c' * 64
        snapshot = {'host':'nx5','tailnet_ipv4':'100.118.44.27','current_source_sha':source,
            'owner_health':{'source_commit':source,'state':'ready','mode':'off','active':0,
                'model_loaded':False,'resident_pid':None,'lease_status':'released','resident_error':None},
            'owner_unit':{'active':'active'},'native_unit':{'active':'inactive'},
            'comfy_unit':{'active':'inactive'},'gate_activity':{'profile_id':'nx5-flint',
                'profile_digest':profile,'mode':'flint','enabled':True,'public_enabled':False,
                'qualification_only':True,'owner_admission':False,'active':0,'unknown':0,'waiting':1,
                'ingress_closed':True},
            'models':[],'gate_request':{'operation_id':operation,'state':'preparing'},
            'central_activity':{'group_id':'nx5-igpu-group','resources':['nx5-igpu'],'fresh':True,
                'leases':[],'transition':{'executor_id':'nx5-text','operation_id':operation,
                    'target_mode':'flint'},'pending':[{'executor_id':'nx5-text','operation_id':operation,
                    'status':'preparing','resources':[{'resource_id':'nx5-igpu','units':1}]}]}}
        self.assertTrue(module.validate_flint_recovery_snapshot(snapshot,operation,source,profile))
        changed = json.loads(json.dumps(snapshot))
        changed['central_activity']['transition']['operation_id'] = 'op_' + 'd' * 32
        with self.assertRaisesRegex(RuntimeError,'original_flint_transition_changed'):
            module.validate_flint_recovery_snapshot(changed,operation,source,profile)
        changed = json.loads(json.dumps(snapshot))
        changed['owner_health']['model_loaded'] = True
        with self.assertRaisesRegex(RuntimeError,'owner_not_fresh_off'):
            module.validate_flint_recovery_snapshot(changed,operation,source,profile)

    def test_publisher_uses_bounded_approved_nx5_ssh_alias(self):
        self.assertEqual(module.remote_ssh_command(['python3', '-']),
                         ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                          'nx5', 'python3', '-'])

    def test_enabling_comfy_preserves_owned_configuration(self):
        defaults = {'bind': '127.0.0.1', 'port': 18347, 'client_token_file': '/private/key'}
        existing = dict(defaults, custom_limit=7)
        result = module.upgrade_config(existing, defaults, managed_comfy=True)
        self.assertEqual(result, dict(existing, managed_comfy=True))
        self.assertNotIn('managed_comfy', existing)

    def test_normal_upgrade_does_not_disable_existing_mode_control(self):
        defaults = {'port': 18347}
        existing = dict(defaults, managed_comfy=True)
        self.assertEqual(module.upgrade_config(existing, defaults), existing)

    def test_managed_llm_profile_version_tracks_published_model_revision(self):
        defaults = {'port': 18347}
        existing = dict(defaults, managed_comfy=True, custom_limit=7,
            policy_profiles={'llm': {'profile_id': 'nx5-ssd-minicpm', 'version': 'old-revision'},
                             'comfy': {'profile_id': 'nx5-qwen21', 'version': 'comfy-revision'}})

        result = module.upgrade_config(existing, defaults, llm_profile_version='new-revision')

        self.assertEqual(result['policy_profiles']['llm'],
                         {'profile_id': 'nx5-ssd-minicpm', 'version': 'new-revision'})
        self.assertEqual(result['policy_profiles']['comfy'], existing['policy_profiles']['comfy'])
        self.assertEqual(result['custom_limit'], 7)
        self.assertEqual(existing['policy_profiles']['llm']['version'], 'old-revision')

    def test_managed_flint_profile_version_tracks_exact_runtime_identity(self):
        existing = {'managed_comfy': True, 'policy_profiles': {
            'llm': {'profile_id': 'nx5-ssd-minicpm', 'version': 'llm-revision'},
            'flint': {'profile_id': 'nx5-flint', 'version': 'old-revision'}}}
        result = module.upgrade_config(existing, {}, flint_profile_version='new-flint')
        self.assertEqual(result['policy_profiles']['flint'],
                         {'profile_id': 'nx5-flint', 'version': 'new-flint'})
        self.assertEqual(existing['policy_profiles']['flint']['version'], 'old-revision')

    def test_managed_flint_profile_is_added_without_changing_other_profiles(self):
        existing = {'managed_comfy': True, 'policy_profiles': {
            'llm': {'profile_id': 'nx5-ssd-minicpm', 'version': 'llm-revision'}}}
        result = module.upgrade_config(existing, {}, flint_profile_version='flint-revision')
        self.assertEqual(result['policy_profiles']['flint'],
                         {'profile_id': 'nx5-flint', 'version': 'flint-revision'})
        self.assertEqual(result['policy_profiles']['llm'], existing['policy_profiles']['llm'])

    def test_unmanaged_configuration_does_not_gain_managed_profile(self):
        existing = {'port': 18347, 'managed_comfy': False}

        result = module.upgrade_config(existing, {'port': 18347}, llm_profile_version='new-revision')

        self.assertEqual(result, existing)

    def test_remote_helper_source_is_self_contained(self):
        namespace = {'hashlib': hashlib}
        exec(inspect.getsource(module.upgrade_config), namespace)

        result = namespace['upgrade_config'](
            {'managed_comfy': True, 'policy_profiles': {'llm': {'profile_id': 'minicpm',
                                                                'version': 'old'}}},
            {}, llm_profile_version='new')

        self.assertEqual(result['policy_profiles']['llm']['version'], 'new')

    def test_published_version_is_accepted_by_actual_managed_comfy_owner(self):
        resident_path = Path(__file__).resolve().parents[1] / 'scripts/nx5/resident.py'
        resident_spec = importlib.util.spec_from_file_location('nx5_candidate_resident', resident_path)
        resident_module = importlib.util.module_from_spec(resident_spec)
        resident_spec.loader.exec_module(resident_module)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'configs').mkdir()
            (root/'configs/nx5-minicpm-service.json').write_text(json.dumps({'revision': 'new-revision'}))
            config = {'managed_comfy': True,
                      'policy_profiles': {'llm': {'profile_id': 'nx5-ssd-minicpm',
                                                  'version': 'old-revision'}}}
            upgraded = module.upgrade_config(config, {}, llm_profile_version='new-revision')

            class Service:
                repo = root
                state = root/'jobs'

                def __init__(self):
                    self.config = upgraded

            owner = resident_module.ManagedComfy(Service())

            self.assertEqual(owner.policy_profiles['llm']['version'], 'new-revision')

    def test_existing_endpoint_or_credentials_cannot_be_replaced_by_defaults(self):
        with self.assertRaisesRegex(RuntimeError, 'service_config_changed'):
            module.upgrade_config({'port': 12345}, {'port': 18347}, managed_comfy=True)

    def test_concurrent_configuration_change_rejects_activation(self):
        old = b'{"managed_comfy":false}'
        expected = hashlib.sha256(old).hexdigest()
        module.check_config_baseline(old, expected, required=True)
        with self.assertRaisesRegex(RuntimeError, 'config_baseline_changed'):
            module.check_config_baseline(b'{"managed_comfy":true}', expected, required=True)

    def test_enabling_existing_configuration_requires_exact_baseline(self):
        with self.assertRaisesRegex(RuntimeError, 'config_baseline_required'):
            module.check_config_baseline(b'{}', None, required=True)


class RemoteActivationTests(unittest.TestCase):
    """Execute the frozen remote program against real temporary files."""
    def setUp(self):
        import tempfile
        import json
        import base64
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.base = self.home/'.local/share/nx5-ssd-llm'
        self.state = self.home/'.local/state/nx5-ssd-llm'
        self.private = self.home/'.config/nx5-ssd-llm'
        self.unit = self.home/'.config/systemd/user/nx5-ssd-llm.service'
        self.config = self.private/'service.json'
        self.jobs = self.state/'jobs'
        for p in [self.private,self.unit.parent,self.jobs,self.base/'releases/old',self.home/'github/qwen38-kvmem']:
            p.mkdir(parents=True,exist_ok=True)
        self.current=self.base/'current'
        self.old=self.base/'releases/old'
        self.current.symlink_to(self.old)
        self.old_unit=b'[Unit]\nDescription=original SSD unit\n'
        self.unit.write_bytes(self.old_unit)
        self.old_config=json.dumps({'root':str(self.home/'github/qwen38-kvmem'),
            'state':str(self.jobs),'client_token_file':str(self.private/'client.key'),
            'bind':'100.118.44.27','port':18347,'custom_limit':7,'managed_comfy':True,
            'policy_profiles':{'llm':{'profile_id':'nx5-ssd-minicpm','version':'old-revision'},
                               'comfy':{'profile_id':'nx5-qwen21','version':'comfy-revision'}}}).encode()
        self.config.write_bytes(self.old_config)
        (self.private/'client.key').write_text('cpu-fixture-not-a-real-secret')
        credentials=self.home/'.config/siyuan-text-ingress';credentials.mkdir(parents=True)
        (credentials/'wrapper.json').write_text(json.dumps({'central':{'base_url':'http://coordinator-fixture',
            'auth':{'token_env':'CPU_CENTRAL_TOKEN'}}}))
        (credentials/'credentials.env').write_text('CPU_CENTRAL_TOKEN=cpu-fixture\n')
        binary=self.home/'github/qwen38-kvmem/cpu-binary';binary.write_bytes(b'cpu-fixture')
        self.sha='a'*40
        contents={'configs/nx5-chat-runtime.json':json.dumps({'binary':'cpu-binary',
            'binary_sha256':hashlib.sha256(b'cpu-fixture').hexdigest(),'libraries':{}}).encode(),
            'deploy/nx5/nx5-ssd-llm.service':b'[Unit]\nDescription=candidate SSD unit\n'}
        self.payload={'apply':True,'managed_comfy':False,'llm_profile_version':'new-revision',
            'expected_config_sha256':hashlib.sha256(self.old_config).hexdigest(),
            'manifest':{'source_commit':self.sha,'commit':'cpu-fingerprint',
                'files':{n:hashlib.sha256(b).hexdigest() for n,b in contents.items()}},
            'files':{n:base64.b64encode(b).decode() for n,b in contents.items()}}
        self.commands=[]

    def execute(self, on_command, on_health=None, identity=('nx5', '100.118.44.27')):
        import io
        import json
        import subprocess
        import urllib.request
        from contextlib import redirect_stdout
        from unittest.mock import patch
        real_iterdir=Path.iterdir
        def iterdir(path):
            return iter(()) if str(path)=='/proc' else real_iterdir(path)
        def run(args,**kwargs):
            self.commands.append(list(args));on_command(args)
            return subprocess.CompletedProcess(args,0,'','')
        def output(args,**kwargs):
            if args[:2]==['hostname','-s']:return identity[0]+'\n'
            if args[:3]==['tailscale','ip','-4']:return identity[1]+'\n'
            prop=next((a for a in args if a.startswith('--property=')),'')
            return ('disabled' if prop=='--property=UnitFileState' else 'inactive')+'\n'
        class Opener:
            def open(inner,request,timeout=0):
                url=request if isinstance(request,str) else request.full_url
                if url.endswith('/healthz'):
                    if on_health:on_health()
                    body={'commit':'cpu-fingerprint','state':'ready'}
                else:body={'leases':[],'pending':[],'transition':None}
                return io.BytesIO(json.dumps(body).encode())
        frozen=module.REMOTE.replace('PAYLOAD',repr(self.payload),1)
        helpers='\n'.join(inspect.getsource(f) for f in
                           (module.upgrade_config,module.check_config_baseline))+'\n'
        namespace={}
        with patch.object(Path,'home',return_value=self.home),patch.object(Path,'iterdir',iterdir), \
             patch.object(subprocess,'run',side_effect=run),patch.object(subprocess,'check_output',side_effect=output), \
             patch.object(urllib.request,'build_opener',return_value=Opener()),redirect_stdout(io.StringIO()):
            exec(helpers,namespace)
            exec(compile(frozen,'frozen-remote-deploy','exec'),namespace)

    def journal(self):
        import json
        return json.loads((self.state/('deployment-'+self.sha+'.json')).read_text())

    def test_wrong_remote_identity_stops_before_any_deployment_write(self):
        with self.assertRaisesRegex(RuntimeError, 'wrong_target_identity'):
            self.execute(lambda args: None, identity=('other-device', '100.118.44.27'))
        self.assertEqual(self.config.read_bytes(), self.old_config)
        self.assertEqual(self.unit.read_bytes(), self.old_unit)
        self.assertEqual(self.current.resolve(), self.old)
        self.assertEqual(self.commands, [])

    def test_successful_activation_updates_only_llm_profile_version(self):
        self.execute(lambda args: None)

        import json
        config = json.loads(self.config.read_text())
        self.assertEqual(config['policy_profiles']['llm'],
                         {'profile_id': 'nx5-ssd-minicpm', 'version': 'new-revision'})
        self.assertEqual(config['policy_profiles']['comfy'],
                         {'profile_id': 'nx5-qwen21', 'version': 'comfy-revision'})
        self.assertEqual(config['custom_limit'], 7)
        self.assertEqual(self.journal()['phase'], 'activated')
        self.assertEqual(self.current.readlink(), self.base/'releases'/self.sha)

    def test_disable_failure_after_config_install_restores_original_owned_controls(self):
        def fail(args):
            if args[2]=='disable':raise RuntimeError('disable_failed')
        with self.assertRaises(RuntimeError):self.execute(fail)
        self.assertEqual(self.config.read_bytes(),self.old_config)
        self.assertEqual(self.unit.read_bytes(),self.old_unit)
        self.assertEqual(self.current.readlink(),self.old)
        self.assertEqual(self.journal()['phase'],'rolled_back')

    def test_parallel_config_change_blocks_every_rollback_control_write(self):
        import json
        user_config=b'{"parallel_owner":true}\n'
        def fail(args):
            if args[2]=='start':
                self.config.write_bytes(user_config)
                raise RuntimeError('start_failed')
        with self.assertRaisesRegex(RuntimeError,'ownership_lost'):self.execute(fail)
        self.assertEqual(self.config.read_bytes(),user_config)
        self.assertEqual(self.unit.read_bytes(),b'[Unit]\nDescription=candidate SSD unit\n')
        self.assertEqual(self.current.readlink(),self.base/'releases'/self.sha)
        self.assertFalse(any(args[2]=='stop' for args in self.commands))
        self.assertEqual(self.journal()['phase'],'ownership_lost')

    def test_new_unknown_job_blocks_automatic_stop_or_rollback(self):
        import json
        def fail(args):
            if args[2]=='start':
                (self.jobs/'op_new.json').write_text(json.dumps({'id':'op_new','status':'unknown'}))
                raise RuntimeError('start_response_lost')
        with self.assertRaisesRegex(RuntimeError,'pending_jobs'):self.execute(fail)
        self.assertEqual(self.current.readlink(),self.base/'releases'/self.sha)
        self.assertFalse(any(args[2]=='stop' for args in self.commands))
        self.assertEqual(self.journal()['phase'],'reconciliation_required')

    def test_start_failure_without_new_jobs_restores_all_owned_controls(self):
        starts=0
        def fail(args):
            nonlocal starts
            if args[2]=='start':
                starts+=1
                if starts==1:raise RuntimeError('candidate_start_failed')
        with self.assertRaises(RuntimeError):self.execute(fail)
        self.assertEqual(self.config.read_bytes(),self.old_config)
        self.assertEqual(self.unit.read_bytes(),self.old_unit)
        self.assertEqual(self.current.readlink(),self.old)
        self.assertEqual(starts,2)
        self.assertEqual(self.journal()['phase'],'rolled_back')

    def test_parallel_unit_change_preserves_all_other_installed_controls(self):
        user_unit=b'[Unit]\nDescription=parallel user unit\n'
        def fail(args):
            if args[2]=='start':
                self.unit.write_bytes(user_unit)
                raise RuntimeError('candidate_start_failed')
        with self.assertRaisesRegex(RuntimeError,'ownership_lost'):self.execute(fail)
        self.assertEqual(self.unit.read_bytes(),user_unit)
        self.assertEqual(self.current.readlink(),self.base/'releases'/self.sha)
        self.assertNotEqual(self.config.read_bytes(),self.old_config)
        self.assertFalse(any(args[2]=='stop' for args in self.commands))

    def test_parallel_link_change_preserves_installed_config_and_unit(self):
        user_release=self.base/'releases/parallel-user-release'
        user_release.mkdir()
        def fail(args):
            if args[2]=='start':
                self.current.unlink();self.current.symlink_to(user_release)
                raise RuntimeError('candidate_start_failed')
        with self.assertRaisesRegex(RuntimeError,'ownership_lost'):self.execute(fail)
        self.assertEqual(self.current.readlink(),user_release)
        self.assertNotEqual(self.config.read_bytes(),self.old_config)
        self.assertNotEqual(self.unit.read_bytes(),self.old_unit)
        self.assertFalse(any(args[2]=='stop' for args in self.commands))

    def test_health_success_does_not_hide_lost_configuration_ownership(self):
        user_config=b'{"parallel_owner_after_start":true}\n'
        with self.assertRaisesRegex(RuntimeError,'ownership_lost'):
            self.execute(lambda args:None,lambda:self.config.write_bytes(user_config))
        self.assertEqual(self.config.read_bytes(),user_config)
        self.assertEqual(self.current.readlink(),self.base/'releases'/self.sha)
        self.assertFalse(any(args[2]=='stop' for args in self.commands))


if __name__ == '__main__':
    unittest.main()
