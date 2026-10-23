import io
import hashlib
import json
from pathlib import Path
import queue
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

        command, _, _ = self.model.launch_config()

        self.assertEqual(command[command.index('--cache-type-k')+1], 'q8_0')
        self.assertEqual(command[command.index('--cache-type-v')+1], 'q8_0')
        self.assertEqual(command[command.index('--cache-type-k-draft')+1], 'q8_0')
        self.assertEqual(command[command.index('--cache-type-v-draft')+1], 'q8_0')
        self.assertEqual(command[command.index('-c')+1], '65536')
        self.assertEqual(command[command.index('--spec-draft-n-max')+1], '7')

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
