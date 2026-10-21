#!/usr/bin/env python3
"""Explicit live acceptance on nx5: real two-model chat, streaming and disconnect recovery."""
import argparse
import json
from pathlib import Path
import threading
import time
import urllib.request
import urllib.error

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
a.output.parent.mkdir(parents=True, exist_ok=True)
base = 'http://100.118.44.27:18347'
token = (Path.home() / '.config/nx5-ssd-llm/client.key').read_text().strip()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
proof = {'started_at': time.time(), 'jobs': [], 'max_llama_processes': 0}
quit_monitor = threading.Event()


def call(path, body=None, key=None, auth=True):
    headers = {'Content-Type': 'application/json'}
    if auth: headers['Authorization'] = 'Bearer ' + token
    if key: headers['Idempotency-Key'] = key
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        return opener.open(req, timeout=300)
    except urllib.error.HTTPError as error:
        return error


def save():
    a.output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')


def observe():
    while not quit_monitor.is_set():
        active = []
        for path in Path('/proc').iterdir():
            if not path.name.isdigit(): continue
            try:
                if (path / 'exe').resolve(strict=True).name.startswith('llama-'):
                    active.append(int(path.name))
            except (FileNotFoundError, PermissionError, ProcessLookupError): pass
        proof['max_llama_processes'] = max(proof['max_llama_processes'], len(active))
        if len(active) > 1: proof['concurrency_violation'] = active
        quit_monitor.wait(0.25)


assert call('/v1/models', auth=False).status == 401
models = json.load(call('/v1/models'))
assert {m['id'] for m in models['data']} == {'qwen36-128k', 'minicpm-64k'}
assert call('/v1/chat/completions', {'model': 'qwen36-128k', 'messages': [{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:abc'}}]}]}).status == 400
proof.update(models=models, unauthorized_rejected=True, images_rejected=True)
monitor = threading.Thread(target=observe, daemon=True)
monitor.start()
try:
    for index, model in enumerate(['qwen36-128k', 'minicpm-64k', 'qwen36-128k']):
        stream = index != 1
        body = {'model': model, 'messages': [
            {'role': 'system', 'content': '用中文回答，保留用户给出的暗号原样。'},
            {'role': 'user', 'content': '我的暗号是 BLUE-PELICAN-73。请记住它。'},
            {'role': 'assistant', 'content': '我记住了你的暗号。'},
            {'role': 'user', 'content': '我的暗号是什么？再回答17乘23等于多少，并用三句话说明SSD专家缓存是否删除了模型专家。'}],
            'temperature': 0, 'seed': 42, 'max_tokens': 192, 'stream': stream}
        if stream: body['stream_options'] = {'include_usage': True}
        key = 'openai-live-' + str(int(proof['started_at'])) + '-' + str(index)
        record = {'request': body, 'key': key, 'started_at': time.time()}
        proof['jobs'].append(record)
        save()
        response = call('/v1/chat/completions', body, key)
        record['http_status'] = response.status
        record['job_id'] = response.headers.get('X-Job-ID')
        save()
        assert response.status == 200, response.read().decode()
        if stream:
            # Both APIs must reject competing work while this model owns admission.
            other = {**body, 'model': 'minicpm-64k'}
            assert call('/v1/chat/completions', other).status == 429
            assert call('/v1/jobs', {'request_id': key + '-other', 'profile':'minicpm-64k','prompt':'busy?'}).status == 409
            record['cross_api_mutex'] = True
            chunks = []
            for raw in response:
                if not raw.startswith(b'data: '): continue
                data = raw[6:].strip()
                if data == b'[DONE]':
                    record['done'] = True
                    break
                event = json.loads(data)
                assert 'error' not in event, event
                chunks.append(event)
                choices = event.get('choices', [])
                if choices and choices[0]['delta'].get('content'):
                    record.setdefault('first_content_s', time.time() - record['started_at'])
                    if index == 2:
                        response.close()
                        record['disconnected_after_content'] = True
                        assert call('/v1/chat/completions', other).status == 429
                        break
            record['chunks'] = chunks
            if index == 0:
                record['stream_text'] = ''.join(c['choices'][0]['delta'].get('content','') for c in chunks if c['choices'])
                assert record.get('done') and chunks[-1].get('usage')
                assert chunks[-2]['choices'][0]['finish_reason'] in ('stop','length')
        else:
            record['response'] = json.load(response)
        for _ in range(1200):
            job = json.load(call('/v1/jobs/' + record['job_id']))
            if job['status'] in ('succeeded','failed','unknown'): break
            time.sleep(0.25)
        record['job'] = job
        record['wall_s'] = time.time() - record['started_at']
        save()
        assert job['status'] == 'succeeded', job
        assert job['result']['lease_status'] == 'released'
        assert job['result']['context_capacity'] == (131072 if model.startswith('qwen') else 65536)
        assert 'BLUE-PELICAN-73' in job['result']['text'] and '391' in job['result']['text']
        if index == 0:
            assert record['stream_text'] == job['result']['text']
            replay = dict(body, stream=False)
            replay.pop('stream_options')
            recovered = call('/v1/chat/completions', replay, key)
            assert recovered.headers['X-Job-ID'] == record['job_id']
            assert json.load(recovered)['choices'][0]['message']['content'] == record['stream_text']
            record['idempotent_json_replay'] = True
        print(json.dumps({'model': model, 'status': job['status'], 'wall_s': record['wall_s'],
                          'first_content_s': record.get('first_content_s'), 'decode_tps': job['result']['decode_tps']}), flush=True)
    proof['health_after'] = json.load(call('/healthz', auth=False))
    assert proof['health_after']['state'] == 'ready' and not proof['health_after']['model_loaded']
    assert not proof.get('concurrency_violation')
    proof['passed'] = True
finally:
    quit_monitor.set()
    monitor.join()
    proof['finished_at'] = time.time()
    save()
