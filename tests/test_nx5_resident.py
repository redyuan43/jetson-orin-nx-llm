import io
import hashlib
import json
import os
from pathlib import Path
import queue
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
        owner.service = type('ServiceFixture', (), {'resident': self.model})()
        return owner

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

    def test_vendored_residency_parser_matches_locked_nano5_source(self):
        vendor = Path(__file__).resolve().parents[1] / 'scripts/minicpm/vendor'
        lock = json.loads((vendor/'source-lock.json').read_text())
        self.assertEqual(lock['commit'], '8f687edf013d57ad3ebf5d955d2e0fed5cbafc44')
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
