import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

spec = importlib.util.spec_from_file_location('nx5_service', Path(__file__).resolve().parents[1] / 'scripts/nx5/service.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'release.json').write_text(json.dumps({'files': {}, 'release': 'test', 'commit': 'test', 'source_commit': 'test'}))
        self.location = patch.object(module, '__file__', str(self.root / 'scripts/nx5/service.py'))
        self.location.start()
        self.config = {'root': str(self.root), 'state': str(self.root / 'state')}
        self.service = module.Service(self.config)

    def tearDown(self):
        self.location.stop()
        self.temp.cleanup()

    def payload(self, rid='one', profile='qwen36-128k'):
        return {'request_id': rid, 'profile': profile, 'prompt': '17*23?', 'max_tokens': 64}

    def test_two_models_racing_admit_only_one(self):
        barrier = threading.Barrier(2)
        def submit(p):
            barrier.wait()
            try:
                return self.service.submit(p)['status']
            except RuntimeError as exc:
                return str(exc)
        with patch.object(self.service, 'run'), ThreadPoolExecutor(2) as pool:
            results = list(pool.map(submit, [self.payload(), self.payload('two', 'minicpm-64k')]))
        self.assertCountEqual(results, ['waiting', 'device_job_busy'])
        self.assertEqual(len(self.service.jobs), 1)

    def test_idempotency_and_conflicting_model(self):
        with patch.object(self.service, 'run'):
            original = self.service.submit(self.payload())
            self.assertEqual(self.service.submit(self.payload())['id'], original['id'])
            with self.assertRaisesRegex(RuntimeError, 'request_id_conflict'):
                self.service.submit(self.payload(profile='minicpm-64k'))

    def test_restart_does_not_replay_unconfirmed_work(self):
        with patch.object(self.service, 'run'):
            original = self.service.submit(self.payload())
        restarted = module.Service(self.config)
        self.assertEqual(restarted.jobs[original['id']]['status'], 'unknown')
        self.assertEqual(restarted.submit(self.payload())['status'], 'unknown')
        with self.assertRaisesRegex(RuntimeError, 'device_job_busy'):
            restarted.submit(self.payload('next', 'minicpm-64k'))

    def test_unreleased_lease_blocks_next_job(self):
        with patch.object(self.service, 'run'):
            accepted = self.service.submit(self.payload())
        job = self.service.jobs[accepted['id']]
        class Child:
            pid = 123
            returncode = 1
            def poll(self): return 1
        def spawn(*args, **kwargs):
            (self.service.state / job['id'] / 'result.lease.json').write_text(json.dumps({'status': 'running'}))
            return Child()
        with patch.object(module.subprocess, 'Popen', side_effect=spawn):
            self.service.run(job, self.payload())
        self.assertEqual(job['status'], 'unknown')
        with self.assertRaisesRegex(RuntimeError, 'device_job_busy'):
            self.service.submit(self.payload('next'))

    def test_invalid_payload_cannot_spawn_model(self):
        for change in ({'profile': 'arbitrary/path'}, {'image_base64': 'abc'}, {'max_tokens': True}, {'max_tokens': 7681}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.service.submit({**self.payload(), **change})
        self.assertFalse(self.service.jobs)


if __name__ == '__main__':
    unittest.main()
