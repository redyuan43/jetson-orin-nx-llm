import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from test_nx5_service import module
from compat_api import normalize, InvalidRequest, completion, collect_tool_calls


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
            job.update(status='succeeded', finished_at=job['created_at'] + 12,
                       result={'text': text, 'input_tokens': 23, 'output_tokens': 4,
                               'decode_tps': 10.0, 'generation_s': 0.4, 'prefill_s': 0.2,
                               'process_wall_s': 4.5, 'finish_reason': 'stop'})
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
        cases += [dict(enable_thinking='true'), dict(enable_thinking=1), dict(enable_thinking=None)]
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
        self.assertEqual(data['timings']['decode_tps'], 10.0)
        self.assertEqual(data['timings']['total_s'], 12)
        self.assertEqual(data['timings']['generation_s'], 0.4)
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
        self.assertEqual(rest.count('"timings":'), 1)
        self.assertIn('"total_s": 12', rest)

    def test_timing_is_not_sent_with_content_or_finish_and_replay_is_stable(self):
        job={'id':'op_abc','created_at':100,'profile':'qwen36-128k'}
        self.assertNotIn('timings', completion(job, delta={'content':'hello'}))
        self.finish(job)
        self.assertNotIn('timings', completion(job, finish='stop'))
        first=completion(job)
        with patch('time.time', return_value=5000):
            replay=completion(job)
        self.assertEqual(first['timings'], replay['timings'])
        self.assertEqual(replay['timings']['total_s'],12)

    def test_thinking_choice_is_part_of_identity_and_off_keeps_old_identity(self):
        implicit=normalize(self.body(), 'same')[0]
        explicit=normalize(self.body(enable_thinking=False), 'same')[0]
        enabled=normalize(self.body(enable_thinking=True), 'same')[0]
        self.assertEqual(implicit,explicit)
        self.assertNotEqual(module.digest(implicit),module.digest(enabled))
        self.assertTrue(enabled['chat']['enable_thinking'])
        history=self.body()
        history['messages'][2]['reasoning_content']='此前的思考'
        self.assertEqual(normalize(history)[0]['chat']['messages'][2]['reasoning_content'],'此前的思考')

    def test_reasoning_is_streamed_and_replayed_separately_from_answer(self):
        def execute(job,payload):
            folder=self.service.state/job['id'];folder.mkdir()
            (folder/'result.out').write_text('\n'.join(json.dumps(e,ensure_ascii=False) for e in [
                {'type':'reasoning_delta','reasoning_content':'先计算'},
                {'type':'delta','content':'391'}])+'\n')
            self.finish(job,'391')
            job['result']['reasoning_content']='先计算'
        self.mock_run.side_effect=execute
        r=self.request('POST','/v1/chat/completions',self.body(enable_thinking=True,stream=True),{'Idempotency-Key':'thinking'})
        values=[json.loads(line[6:]) for line in r.read().decode().splitlines() if line.startswith('data: {')]
        self.assertEqual(''.join(c['choices'][0]['delta'].get('reasoning_content','') for c in values),'先计算')
        self.assertEqual(''.join(c['choices'][0]['delta'].get('content','') for c in values),'391')
        r=self.request('POST','/v1/chat/completions',self.body(enable_thinking=True),{'Idempotency-Key':'thinking'})
        message=json.load(r)['choices'][0]['message']
        self.assertEqual(message,{'role':'assistant','content':'391','reasoning_content':'先计算'})
        self.assertEqual(self.mock_run.call_count,1)
        r=self.request('POST','/v1/chat/completions',self.body(enable_thinking=False),{'Idempotency-Key':'thinking'})
        self.assertEqual(r.status,409)
        r.read()

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

    def tools(self):
        return [{'type': 'function', 'function': {'name': 'add', 'description': 'Add two integers',
                 'parameters': {'type': 'object', 'properties': {'a': {'type': 'integer'},
                 'b': {'type': 'integer'}}, 'required': ['a', 'b']}}}]

    def test_tools_round_trip_accepts_result_and_preserves_reasoning(self):
        call = {'id': 'call_1', 'type': 'function', 'function': {'name': 'add', 'arguments': '{"a":17,"b":23}'}}
        messages = [{'role': 'user', 'content': 'Add 17 and 23 using the tool'},
                    {'role': 'assistant', 'content': None, 'reasoning_content': 'Use add.', 'tool_calls': [call]},
                    {'role': 'tool', 'tool_call_id': 'call_1', 'name': 'add', 'content': '40'}]
        payload = normalize(self.body(messages=messages, tools=self.tools(), enable_thinking=True,
                                      parallel_tool_calls=False))[0]['chat']
        self.assertEqual(payload['messages'][1]['content'], '')
        self.assertEqual(payload['messages'][1]['tool_calls'], [call])
        self.assertEqual(payload['messages'][1]['reasoning_content'], 'Use add.')
        self.assertEqual(payload['messages'][2], messages[2])
        self.assertEqual(payload['tool_choice'], 'auto')
        self.assertFalse(payload['parallel_tool_calls'])
        chosen = {'type': 'function', 'function': {'name': 'add'}}
        self.assertEqual(normalize(self.body(tools=self.tools(), tool_choice=chosen))[0]['chat']['tool_choice'], chosen)

    def test_malformed_tools_and_unmatched_results_never_start(self):
        call = {'id': 'call_1', 'type': 'function', 'function': {'name': 'add', 'arguments': '{}'}}
        bad = [dict(tools={}), dict(tools=False), dict(tools=[{'type': 'function'}]),
               dict(tools=self.tools() * 2), dict(tools=self.tools(), parallel_tool_calls='false'),
               dict(tools=self.tools(), tool_choice={'type': 'function', 'function': {'name': 'missing'}}),
               dict(tools=self.tools(), tool_choice={'type': 'function', 'function': {'name': []}}),
               dict(messages=[{'role': 'tool', 'tool_call_id': 'missing', 'content': '40'}]),
               dict(messages=[{'role': 'assistant', 'tool_calls': [call], 'content': None},
                              {'role': 'user', 'content': 'continue'}]),
               dict(messages=[{'role': 'user', 'content': 'hello', 'tool_calls': [call]}])]
        for item in bad:
            with self.subTest(item=item):
                r = self.request('POST', '/v1/chat/completions', self.body(**item))
                self.assertEqual(r.status, 400)
                r.read()
        self.mock_run.assert_not_called()

    def test_tool_stream_matches_final_and_replay_and_keeps_usage_single(self):
        events = [
            {'type': 'reasoning_delta', 'reasoning_content': 'Use add.'},
            {'type': 'tool_call_delta', 'tool_call': {'index': 0, 'id': 'call_1', 'type': 'function', 'function': {'name': 'add'}}},
            {'type': 'tool_call_delta', 'tool_call': {'index': 0, 'function': {'arguments': '{"a":17,'}}},
            {'type': 'tool_call_delta', 'tool_call': {'index': 0, 'function': {'arguments': '"b":23}'}}},
        ]
        expected = [{'id': 'call_1', 'type': 'function', 'function': {'name': 'add', 'arguments': '{"a":17,"b":23}'}}]
        self.assertEqual(collect_tool_calls(events), expected)
        def execute(job, payload):
            folder = self.service.state/job['id']; folder.mkdir()
            (folder/'result.out').write_text('\n'.join(json.dumps(e) for e in events)+'\n')
            self.finish(job, '')
            job['result'].update(reasoning_content='Use add.', tool_calls=expected, finish_reason='tool_calls')
        self.mock_run.side_effect = execute
        body = self.body(tools=self.tools(), stream=True, stream_options={'include_usage': True}, enable_thinking=True)
        r = self.request('POST', '/v1/chat/completions', body, {'Idempotency-Key': 'toolstream'})
        self.assertEqual(r.status, 200)
        chunks = [json.loads(line[6:]) for line in r.read().decode().splitlines() if line.startswith('data: {')]
        wire = [{'type': 'tool_call_delta', 'tool_call': t} for c in chunks if c['choices']
                for t in c['choices'][0]['delta'].get('tool_calls', [])]
        self.assertEqual(collect_tool_calls(wire), expected)
        self.assertEqual([c['choices'][0]['finish_reason'] for c in chunks if c['choices']][-1], 'tool_calls')
        self.assertEqual(sum('usage' in c for c in chunks), 1)
        r = self.request('POST', '/v1/chat/completions', {**body, 'stream': False, 'stream_options': None}, {'Idempotency-Key': 'toolstream'})
        final = json.load(r)
        self.assertEqual(final['choices'][0]['message']['tool_calls'], expected)
        self.assertEqual(final['choices'][0]['message']['reasoning_content'], 'Use add.')
        self.assertEqual(self.mock_run.call_count, 1)
