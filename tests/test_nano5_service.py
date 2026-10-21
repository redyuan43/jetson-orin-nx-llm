import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('nano_service', Path(__file__).resolve().parents[1] / 'scripts/minicpm/nano5-service.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NanoServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'root': self.temp.name, 'state': self.temp.name + '/state', 'comfy_unit': 'test-comfy'}
        self.service = module.Service(self.config)
        self.transition = {'group_id': module.GROUP, 'executor_id': 'tester', 'operation_id': 'op_' + '1' * 32,
                           'target_mode': 'minicpm', 'resources': [module.RESOURCE], 'lease_barrier': True}

    def test_changed_barrier_never_stops_service(self):
        t = dict(self.transition, operation_id='op_' + '2' * 32)
        with patch.object(self.service, 'report'), patch.object(self.service, 'transition', return_value=t), patch.object(module.subprocess, 'run') as action:
            with self.assertRaisesRegex(RuntimeError, 'transition_changed'):
                self.service.switch(self.transition, {'mode': 'comfy', 'state': 'ready', 'active': 0})
            action.assert_not_called()

    def test_busy_comfy_is_not_stopped(self):
        with patch.object(module.subprocess, 'run') as action:
            self.service.switch(self.transition, {'mode': 'comfy', 'state': 'ready', 'active': 2})
            action.assert_not_called()

    def test_missing_queue_is_unknown(self):
        with patch.object(self.service, 'unit', return_value='active'), patch.object(self.service, 'request', return_value={'queue_running': []}):
            with self.assertRaisesRegex(ValueError, 'incomplete_comfy_activity'):
                self.service.local_observation()

    def test_conflicting_modes_are_rejected(self):
        module.atomic(self.service.state / 'mode.json', {'mode': 'minicpm'})
        with patch.object(self.service, 'unit', return_value='active'):
            with self.assertRaisesRegex(RuntimeError, 'mode_conflict'):
                self.service.local_observation()

    def test_request_id_is_idempotent_and_conflicts_reject(self):
        payload = {'request_id': 'same', 'prompt': 'hello'}
        with patch.object(module.threading.Thread, 'start') as start:
            first = self.service.submit(dict(payload))
            self.assertEqual(first['id'], self.service.submit(dict(payload))['id'])
            self.assertEqual(start.call_count, 1)
            with self.assertRaisesRegex(RuntimeError, 'request_id_conflict'):
                self.service.submit(dict(payload, prompt='different'))

    def test_restart_preserves_unknown_work_without_replay(self):
        with patch.object(module.threading.Thread, 'start'):
            first = self.service.submit({'request_id': 'pending', 'prompt': 'hello'})
        restarted = module.Service(self.config)
        self.assertEqual(restarted.jobs[first['id']]['status'], 'unknown')
        with self.assertRaisesRegex(RuntimeError, 'device_job_busy'):
            restarted.submit({'request_id': 'new', 'prompt': 'hello'})

    def test_release_uses_lease_digest_not_prompt_digest(self):
        job = {'id': 'op_' + 'a' * 32, 'digest': 'prompt', 'lease_digest': 'lease', 'release_evidence': 'proof'}
        with patch.object(self.service, 'central') as request:
            self.service.release(job, 'succeeded')
            self.assertEqual(request.call_args.args[2]['request_digest'], 'lease')

    def test_answer_parser_preserves_html(self):
        raw = '> prompt\n\n|\b-\b/\b \\b<html>hello</html>\n[ Prompt: 1 t/s ]'.replace('\\b', '\b')
        self.assertEqual(module.model_answer(raw), '<html>hello</html>')


if __name__ == '__main__':
    unittest.main()
