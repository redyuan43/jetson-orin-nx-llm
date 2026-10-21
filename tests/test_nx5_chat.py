import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from test_nx5_service import module
from compat_api import normalize, InvalidRequest


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'release.json').write_text(json.dumps({'files': {}, 'release': 'test', 'commit': 'test', 'source_commit': 'test'}))
        (self.root / 'key').write_text('x' * 48)
        self.location = patch.object(module, '__file__', str(self.root / 'scripts/nx5/service.py'))
        self.location.start()
        cfg = {'root': str(self.root), 'state': str(self.root / 'state'), 'bind': '127.0.0.1',
               'port': 0, 'client_token_file': str(self.root / 'key')}
        self.service = module.Service(cfg)
        self.run = patch.object(self.service, 'run')
        self.mock_run = self.run.start()
        self.server = module.make_server(cfg, self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.clients = []

    def tearDown(self):
        for conn in self.clients:
            conn.close()
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.run.stop()
        self.location.stop()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        self.clients.append(conn)
        h = {'Authorization': 'Bearer ' + 'x' * 48, 'Content-Type': 'application/json'}
        h.update(headers or {})
        conn.request(method, path, None if body is None else json.dumps(body), h)
        return conn.getresponse()

    def body(self, **kwargs):
        return {'model': 'qwen36-128k', 'messages': [{'role': 'system', 'content': '用中文回答'},
                {'role': 'user', 'content': '暗号青鹈鹕'}, {'role': 'assistant', 'content': '记住了'},
                {'role': 'user', 'content': [{'type': 'text', 'text': '暗号？'}]}], **kwargs}

    def finish(self, job, text='青鹈鹕'):
        with self.service.lock:
            job.update(status='succeeded', result={'text': text, 'input_tokens': 23, 'output_tokens': 4,
                                                   'finish_reason': 'stop'})
            self.service.save(job)

    def test_models_auth_and_invalid_requests_never_start(self):
        r = self.request('GET', '/v1/models', headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(r.status, 401)
        self.assertEqual(json.load(r)['error']['type'], 'authentication_error')
        r = self.request('GET', '/v1/models')
        self.assertEqual([x['id'] for x in json.load(r)['data']], list(module.PROFILES))
        cases = [dict(model='missing'), dict(tools=[{'type': 'function'}]),
                 dict(messages=[{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:abc'}}]}]),
                 dict(temperature=float('nan')), dict(max_tokens=True), dict(seed=-1), dict(n=2),
                 dict(reasoning_effort='high'), dict(stream_options={'include_usage': True})]
        for extra in cases:
            with self.subTest(extra=extra):
                r = self.request('POST', '/v1/chat/completions', self.body(**extra))
                self.assertIn(r.status, (400, 404))
                self.assertIsInstance(json.load(r)['error'], dict)
        self.mock_run.assert_not_called()

    def test_roles_sampling_and_completion_usage(self):
        def execute(job, payload):
            self.finish(job)
        self.mock_run.side_effect = execute
        r = self.request('POST', '/v1/chat/completions', self.body(temperature=0, top_p=0.8, seed=7,
                         stop=['END'], presence_penalty=0.2, frequency_penalty=0.3, max_completion_tokens=64))
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader('X-Job-ID').startswith('op_'))
        data = json.load(r)
        self.assertEqual(data['choices'][0]['message']['content'], '青鹈鹕')
        self.assertEqual(data['usage'], {'prompt_tokens': 23, 'completion_tokens': 4, 'total_tokens': 27})
        payload = self.mock_run.call_args[0][1]
        self.assertEqual([x['role'] for x in payload['chat']['messages']], ['system', 'user', 'assistant', 'user'])
        self.assertEqual(payload['chat']['messages'][-1]['content'], '暗号？')
        self.assertEqual(payload['chat']['stop'], ['END'])
        self.assertEqual(payload['chat']['seed'], 7)
        self.assertEqual(payload['max_tokens'], 64)

    def test_stream_delivers_before_completion_and_handles_utf8_split(self):
        arrived = threading.Event()
        finish = threading.Event()
        def execute(job, payload):
            folder = self.service.state / job['id']
            folder.mkdir()
            raw = (json.dumps({'type': 'delta', 'content': '青鹈鹕'}, ensure_ascii=False) + '\n').encode()
            path = folder / 'result.out'
            path.write_bytes(raw[:-2])
            time.sleep(0.15)
            with path.open('ab') as output:
                output.write(raw[-2:])
            arrived.set()
            finish.wait(3)
            self.finish(job)
        self.mock_run.side_effect = execute
        r = self.request('POST', '/v1/chat/completions', self.body(stream=True, stream_options={'include_usage': True}))
        self.assertEqual(r.status, 200)
        values = []
        while True:
            line = r.readline().decode()
            if line.startswith('data: '):
                values.append(json.loads(line[6:]))
                if values[-1]['choices'][0]['delta'].get('content') == '青鹈鹕':
                    break
        self.assertTrue(arrived.is_set())
        self.assertEqual(self.service.health()['state'], 'busy')
        finish.set()
        rest = r.read().decode()
        self.assertIn('"finish_reason": "stop"', rest)
        self.assertIn('"choices": [], "usage":', rest)
        self.assertTrue(rest.endswith('data: [DONE]\n\n'))

    def test_chat_and_legacy_share_admission_disconnect_keeps_busy(self):
        r = self.request('POST', '/v1/chat/completions', self.body(stream=True), {'Idempotency-Key': 'shared'})
        self.assertEqual(r.status, 200)
        r.read(10)
        r.close()
        self.clients[-1].close()
        legacy = {'request_id': 'legacy', 'profile': 'minicpm-64k', 'prompt': 'hello'}
        r = self.request('POST', '/v1/jobs', legacy)
        self.assertEqual(r.status, 409)
        r.read()
        r = self.request('POST', '/v1/chat/completions', self.body(model='minicpm-64k'))
        self.assertEqual(r.status, 429)
        self.assertEqual(json.load(r)['error']['code'], 'device_job_busy')
        job = next(iter(self.service.jobs.values()))
        self.finish(job)
        r = self.request('POST', '/v1/chat/completions', self.body(), {'Idempotency-Key': 'shared'})
        self.assertEqual(r.status, 200)
        r.read()
        self.assertEqual(self.mock_run.call_count, 1)
        r = self.request('POST', '/v1/chat/completions', self.body(temperature=0), {'Idempotency-Key': 'shared'})
        self.assertEqual(r.status, 409)
        r.read()

    def test_generation_failure_is_not_success(self):
        def execute(job, payload):
            with self.service.lock:
                job.update(status='failed', error='context_length_exceeded')
        self.mock_run.side_effect = execute
        r = self.request('POST', '/v1/chat/completions', self.body())
        self.assertEqual(r.status, 400)
        self.assertEqual(json.load(r)['error']['code'], 'context_length_exceeded')
        r = self.request('POST', '/v1/chat/completions', self.body(stream=True))
        raw = r.read().decode()
        self.assertIn('context_length_exceeded', raw)
        self.assertNotIn('"finish_reason": "stop"', raw)
