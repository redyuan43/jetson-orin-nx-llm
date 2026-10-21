#!/usr/bin/env python3
"""Authorized live smoke: Qwen -> MiniCPM -> Qwen, with admission conflict checks.

Run on nx5. The private token never enters stdout or process arguments.
"""
import argparse
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
base = 'http://100.118.44.27:18347'
token = (Path.home() / '.config/nx5-ssd-llm/client.key').read_text().strip()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
proof = {'started_at': time.time(), 'jobs': [], 'max_llama_processes': 0}


def call(method, path, body=None, auth=True):
    req = urllib.request.Request(base + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + token} if auth else {})})
    try:
        with opener.open(req, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def save():
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')


def observe():
    models = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit(): continue
        try:
            if (p / 'exe').resolve(strict=True).name.startswith('llama-'):
                models.append(int(p.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError): pass
    proof['max_llama_processes'] = max(proof['max_llama_processes'], len(models))
    assert len(models) <= 1, 'simultaneous_model_processes'
    return models


assert call('POST', '/v1/jobs', {}, auth=False)[0] == 401
assert call('GET', '/v1/jobs/op_' + '0' * 32, auth=False)[0] == 401
assert call('POST', '/v1/jobs', {'request_id': 'invalid', 'profile': '../x', 'prompt': 'x'})[0] == 400
proof['unauthorized_rejected'] = True
proof['invalid_profile_rejected'] = True
for index, profile in enumerate(['qwen36-128k', 'minicpm-64k', 'qwen36-128k']):
    request = {'request_id': f'nx5-api-{int(proof["started_at"])}-{index}', 'profile': profile,
               'prompt': '请用中文回答：17×23等于多少？再用两句话解释SSD缓存专家是否会删除模型的专家。', 'max_tokens': 192}
    status, receipt = call('POST', '/v1/jobs', request)
    assert status == 202, (status, receipt)
    proof['jobs'].append({'request': request, 'receipt': receipt})
    save()
    duplicate = call('POST', '/v1/jobs', request)
    assert duplicate[0] == 202 and duplicate[1]['id'] == receipt['id']
    assert call('POST', '/v1/jobs', {**request, 'prompt': 'different'})[0] == 409
    other = {**request, 'request_id': request['request_id'] + '-busy',
             'profile': 'minicpm-64k' if profile.startswith('qwen') else 'qwen36-128k'}
    status, busy = call('POST', '/v1/jobs', other)
    assert status == 409 and busy['error'] == 'device_job_busy', (status, busy)
    proof['jobs'][-1].update(idempotent_retry=True, changed_request_rejected=True, other_model_busy_rejected=True)
    for _ in range(2400):
        observe()
        status, job = call('GET', receipt['status_url'])
        assert status == 200
        if job['status'] in {'succeeded', 'failed', 'unknown'}:
            break
        time.sleep(0.25)
    else:
        raise TimeoutError('Keep original operation; do not replay')
    proof['jobs'][-1]['final'] = job
    save()
    assert job['status'] == 'succeeded', job
    assert '391' in job['result']['text']
    assert job['result']['lease_status'] == 'released'
    assert job['result']['context_capacity'] == (131072 if profile.startswith('qwen') else 65536)
    assert not observe()
    print(json.dumps({'profile': profile, 'status': job['status'], 'wall_s': job['result']['process_wall_s']}, ensure_ascii=False), flush=True)
proof['health_after'] = call('GET', '/healthz', auth=False)[1]
proof['finished_at'] = time.time()
proof['passed'] = True
save()
print(json.dumps({'passed': True, 'max_llama_processes': proof['max_llama_processes']}))
