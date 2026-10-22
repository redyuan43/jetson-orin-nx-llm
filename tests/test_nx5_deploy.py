import hashlib
import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    'nx5_deploy', Path(__file__).resolve().parents[1] / 'scripts/nx5/deploy.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DeploymentConfigurationTests(unittest.TestCase):
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
            'bind':'100.118.44.27','port':18347,'custom_limit':7}).encode()
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
        self.payload={'apply':True,'managed_comfy':True,
            'expected_config_sha256':hashlib.sha256(self.old_config).hexdigest(),
            'manifest':{'source_commit':self.sha,'commit':'cpu-fingerprint',
                'files':{n:hashlib.sha256(b).hexdigest() for n,b in contents.items()}},
            'files':{n:base64.b64encode(b).decode() for n,b in contents.items()}}
        self.commands=[]

    def execute(self, on_command, on_health=None):
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
        namespace={'upgrade_config':module.upgrade_config,'check_config_baseline':module.check_config_baseline}
        with patch.object(Path,'home',return_value=self.home),patch.object(Path,'iterdir',iterdir), \
             patch.object(subprocess,'run',side_effect=run),patch.object(subprocess,'check_output',side_effect=output), \
             patch.object(urllib.request,'build_opener',return_value=Opener()),redirect_stdout(io.StringIO()):
            exec(compile(frozen,'frozen-remote-deploy','exec'),namespace)

    def journal(self):
        import json
        return json.loads((self.state/('deployment-'+self.sha+'.json')).read_text())

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
