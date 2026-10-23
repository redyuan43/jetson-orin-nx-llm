#!/usr/bin/env python3
"""Tailnet admission for persistent MiniCPM with one cached inference slot."""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compat_api import InvalidRequest, normalize, error as chat_error, completion, collect_tool_calls
from resident import ResidentModel, ManagedComfy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROFILES = {
    'minicpm-64k': 'configs/nx5-minicpm-service.json',
}
TERMINAL = {'succeeded', 'failed'}
STREAM_HEARTBEAT_SECONDS = 5


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def atomic(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.chmod(0o600)
    os.replace(temp, path)


def answer_from_cli(raw):
    parts = re.split(r'(?:[|/\\ \-]\x08){2,}', raw)
    if len(parts) < 2 or '[ Prompt:' not in parts[-1]:
        raise ValueError('answer_boundary_missing')
    answer = re.sub(r'[^\n]\x08', '', parts[-1]).split('[ Prompt:')[0].strip()
    if not answer:
        raise ValueError('empty_answer')
    return answer


def validate(payload):
    if not isinstance(payload, dict) or set(payload) - {'request_id', 'profile', 'prompt', 'max_tokens'}:
        raise ValueError('invalid_fields')
    payload = dict(payload)
    rid = payload.get('request_id')
    if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', rid):
        raise ValueError('invalid_request_id')
    if payload.get('profile') not in PROFILES:
        raise ValueError('invalid_profile')
    if not isinstance(payload.get('prompt'), str) or not 1 <= len(payload['prompt']) <= 500000:
        raise ValueError('invalid_prompt')
    if '\x00' in payload['prompt']:
        raise ValueError('invalid_prompt')
    payload.setdefault('max_tokens', 2048)
    if type(payload['max_tokens']) is not int or not 1 <= payload['max_tokens'] <= 7680:
        raise ValueError('invalid_output_limit')
    return payload


class Service:
    def __init__(self, config):
        self.config = config
        self.repo = Path(__file__).resolve().parents[2]
        self.state = Path(config['state'])
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.jobs = {}
        for p in self.state.glob('op_*.json'):
            job = json.loads(p.read_text())
            if job['status'] not in TERMINAL:
                job.update(status='unknown', error='restart_requires_reconciliation')
                atomic(p, job)
            self.jobs[job['id']] = job
        release = json.loads((self.repo / 'release.json').read_text())
        for name, expected in release['files'].items():
            if hashlib.sha256((self.repo / name).read_bytes()).hexdigest() != expected:
                raise ValueError('release_file_changed: ' + name)
        self.release = {k: release[k] for k in ('release', 'commit', 'source_commit')}
        self.resident = ResidentModel(self.repo, config)
        self.closing = False
        self.mode = ManagedComfy(self) if config.get('managed_comfy') is True else None
        self.resident.manager = self.mode

    def save(self, job):
        atomic(self.state / (job['id'] + '.json'), job)

    def public(self, job):
        return {k: v for k, v in job.items() if k not in {'digest', 'child_pid'}}

    def health(self):
        with self.lock:
            pending = [j for j in self.jobs.values() if j['status'] not in TERMINAL]
            blocked = (self.resident.blocked or any(j['status'] == 'unknown' for j in pending)
                       or (self.mode and (self.mode.intent.get('phase') == 'unknown'
                                          or self.mode.policy_state.get('phase') == 'unknown')))
            native = self.resident.health()
            return {'service_id': 'nx5-ssd-llm', **self.release, **native,
                    'state': 'unknown' if blocked else 'stopping' if self.closing else 'busy' if pending else 'ready',
                    'mode': ('comfy' if self.mode and self.mode.observed['mode'] == 'comfy' else
                             'flint' if self.mode and self.mode.observed['mode'] == 'flint' else
                             'minicpm-64k' if native['model_loaded'] or pending else 'off'),
                    'active': len(pending), 'observed_at': time.time(), 'resource': 'nx5-igpu',
                    'managed_comfy': self.mode.health() if self.mode else None,
                    'availability_scope': 'NX5 resident owner serializes MiniCPM, Flint and Comfy modes; text leases remain with their original executor.' if self.mode else 'MiniCPM loads on first request and stays resident with one prefix-cache slot and a held central lease.'}

    def unload(self):
        with self.lock:
            if any(j['status'] not in TERMINAL for j in self.jobs.values()):
                raise RuntimeError('device_job_busy')
            if self.mode and self.mode.policy_state.get('phase') == 'loading':
                raise RuntimeError('device_policy_loading')
            self.resident.close()
            if self.resident.blocked:
                raise RuntimeError('resident_requires_reconciliation')
            return self.health()

    def shutdown(self):
        with self.lock:
            self.closing = True
        if self.mode:
            self.mode.stop.set()
        self.resident.close(permanent=True)
        with self.lock:
            for job in self.jobs.values():
                if job['status'] not in TERMINAL:
                    job.update(status='unknown' if self.resident.blocked else 'failed',
                               error='service_stopped', finished_at=time.time())
                    self.save(job)

    def submit(self, payload):
        return self.admit(validate(payload))

    def admit(self, payload):
        identifier = 'op_' + hashlib.sha256(('nx5-ssd-llm:' + payload['request_id']).encode()).hexdigest()[:32]
        fingerprint = digest(payload)
        with self.lock:
            if self.closing or self.resident.blocked:
                raise RuntimeError('resident_requires_reconciliation')
            if payload['profile'] not in PROFILES:
                raise InvalidRequest('model_not_found', 'model')
            old = self.jobs.get(identifier)
            if old:
                if old['digest'] != fingerprint:
                    raise RuntimeError('request_id_conflict')
                return self.public(old)
            if any(j['status'] not in TERMINAL for j in self.jobs.values()):
                raise RuntimeError('device_job_busy')
            if self.mode:
                self.mode.gate_text()
            job = {'id': identifier, 'request_id': payload['request_id'], 'digest': fingerprint,
                   'profile': payload['profile'], 'status': 'waiting', 'created_at': time.time()}
            self.jobs[identifier] = job
            self.save(job)
            threading.Thread(target=self.run, args=(job, payload), daemon=True).start()
            return self.public(job)

    def run(self, job, payload):
        folder = self.state / job['id']
        try:
            folder.mkdir(mode=0o700, exist_ok=False)
            chat = payload.get('chat') or {'messages': [{'role': 'user', 'content': payload['prompt']}],
                                           'enable_thinking': False}
            prompt = folder / 'prompt.txt'
            prompt.write_text(json.dumps(chat, ensure_ascii=False))
            prompt.chmod(0o600)
            def on_ready(pid):
                with self.lock:
                    job.update(status='running', started_at=time.time(), child_pid=pid)
                    self.save(job)
            final, process = self.resident.generate(chat, payload['max_tokens'], folder/'result.out', on_ready)
            events = [json.loads(line) for line in (folder/'result.out').read_text().splitlines()]
            if (final['type'] != 'final' or
                    final['content'] != ''.join(e['content'] for e in events if e['type'] == 'delta') or
                    final.get('reasoning_content', '') != ''.join(e['reasoning_content'] for e in events if e['type'] == 'reasoning_delta') or
                    final.get('tool_calls', []) != collect_tool_calls(events)):
                raise RuntimeError('incomplete_chat_result')
            with self.lock:
                job.update(status='succeeded', finished_at=time.time(), result={
                    'text': final['content'], 'profile': payload['profile'],
                    'reasoning_content': final.get('reasoning_content', ''), 'tool_calls': final.get('tool_calls', []),
                    'context_capacity': 65536, 'input_tokens': final['usage']['prompt_tokens'],
                    'output_tokens': final['usage']['completion_tokens'],
                    'cached_input_tokens': final['usage'].get('prompt_tokens_details', {}).get('cached_tokens', 0),
                    'prompt_tokens_processed': final['timings']['prompt_n'],
                    'prefill_s': final['timings']['prompt_ms']/1000,
                    'generation_s': final['timings']['predicted_ms']/1000,
                    'decode_tps': final['timings']['predicted_per_second'],
                    'finish_reason': final['finish_reason'], **process})
        except Exception as exc:
            if not isinstance(exc, ValueError) or str(exc) != 'context_length_exceeded':
                self.resident.close()
            code = 'context_length_exceeded' if str(exc) == 'context_length_exceeded' else 'inference_failed'
            with self.lock:
                job.update(status='unknown' if self.resident.blocked else 'failed',
                           error='reconciliation_required' if self.resident.blocked else code)
            print('job:', job['id'], type(exc).__name__, flush=True)
        finally:
            with self.lock:
                job.setdefault('finished_at', time.time())
                self.save(job)


def make_server(config, service=None):
    service = service or Service(config)
    token = Path(config['client_token_file']).read_text().strip()
    if len(token) < 32:
        raise ValueError('invalid_client_token')
    documents = service.repo / 'docs/nx5-service'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, value, job=None):
            raw = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            if job:
                self.send_header('X-Job-ID', job['id'])
                self.send_header('X-Request-ID', job['request_id'])
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def authorized(self):
            return hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode())

        def do_GET(self):
            if self.path == '/healthz':
                return self.send(200, service.health())
            if self.path == '/v1/service-catalog':
                catalog = json.loads((documents / 'catalog.json').read_text())
                catalog.update(release=service.release, runtime=service.health(),
                               guide_markdown=(documents / 'API.md').read_text())
                return self.send(200, catalog)
            if self.path == '/v1/client-contract':
                contract = json.loads((documents / 'openapi.json').read_text())
                contract['info']['version'] = service.release['commit']
                return self.send(200, contract)
            if not self.authorized():
                return self.send(401, chat_error('unauthorized', kind='authentication_error'))
            if self.path == '/v1/models':
                return self.send(200, {'object': 'list', 'data': [
                    {'id': name, 'object': 'model', 'created': 0, 'owned_by': 'nx5',
                     'context_window': 131072 if name == 'qwen36-128k' else 65536}
                    for name in PROFILES]})
            match = re.fullmatch(r'/v1/jobs/(op_[a-f0-9]{32})', self.path)
            with service.lock:
                job = service.jobs.get(match[1]) if match else None
                return self.send(200, service.public(job)) if job else self.send(404, {'error': 'not_found'})

        def job_snapshot(self, job):
            with service.lock:
                return service.public(service.jobs[job['id']]).copy()

        def chat(self, job, stream, include_usage):
            if stream:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('X-Accel-Buffering', 'no')
                self.send_header('Connection', 'close')
                self.send_header('X-Job-ID', job['id'])
                self.send_header('X-Request-ID', job['request_id'])
                self.end_headers()
                self.close_connection = True
                self.connection.settimeout(20)

                def event(value):
                    self.wfile.write(('data: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode())
                    self.wfile.flush()

                try:
                    event(completion(job, delta={'role': 'assistant', 'content': ''}))
                    offset, last_heartbeat = 0, time.monotonic()
                    path = service.state / job['id'] / 'result.out'
                    while True:
                        snapshot = self.job_snapshot(job)
                        if path.exists():
                            with path.open('rb') as source:
                                source.seek(offset)
                                while True:
                                    line = source.readline()
                                    if not line.endswith(b'\n'):
                                        break
                                    offset = source.tell()
                                    item = json.loads(line)
                                    if item['type'] == 'delta':
                                        event(completion(job, delta={'content': item['content']}))
                                    elif item['type'] == 'reasoning_delta':
                                        event(completion(job, delta={'reasoning_content': item['reasoning_content']}))
                                    elif item['type'] == 'tool_call_delta':
                                        event(completion(job, delta={'tool_calls': [item['tool_call']]}))
                        if snapshot['status'] == 'succeeded':
                            event(completion(snapshot, finish=snapshot['result']['finish_reason']))
                            if include_usage:
                                event(completion(snapshot, usage_only=True))
                            break
                        if snapshot['status'] in ('failed', 'unknown'):
                            event(chat_error(snapshot['error'], kind='server_error'))
                            break
                        if time.monotonic() - last_heartbeat >= STREAM_HEARTBEAT_SECONDS:
                            # SDK chunk timers ignore SSE comments. An empty delta
                            # preserves liveness without inventing content or usage.
                            event(completion(job, delta={}))
                            last_heartbeat = time.monotonic()
                        time.sleep(0.1)
                    self.wfile.write(b'data: [DONE]\n\n')
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                    # Disconnect never releases a running model's lease or replays work.
                    return
            else:
                while True:
                    snapshot = self.job_snapshot(job)
                    if snapshot['status'] == 'succeeded':
                        return self.send(200, completion(snapshot), job)
                    if snapshot['status'] in ('failed', 'unknown'):
                        code = snapshot['error']
                        return self.send(400 if code == 'context_length_exceeded' else 503,
                                         chat_error(code, kind='server_error'), job)
                    time.sleep(0.2)

        def do_POST(self):
            is_chat = self.path == '/v1/chat/completions'
            if not self.authorized():
                return self.send(401, chat_error('unauthorized', kind='authentication_error') if is_chat else {'error': 'unauthorized'})
            if self.path == '/v1/model/unload':
                try:
                    return self.send(200, service.unload())
                except RuntimeError as exc:
                    return self.send(409, chat_error(str(exc)))
            if self.path not in ('/v1/jobs', '/v1/chat/completions'):
                return self.send(404, chat_error('not_found'))
            try:
                self.connection.settimeout(20)
                size = int(self.headers.get('Content-Length', '0'))
                if not 1 <= size <= 2 * 1024**2:
                    return self.send(413, chat_error('invalid_body_size') if is_chat else {'error': 'invalid_body_size'})
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError('incomplete_body')
                body = json.loads(raw)
                if is_chat:
                    payload, stream, usage = normalize(body, self.headers.get('Idempotency-Key'))
                    job = service.admit(payload)
                    return self.chat(job, stream, usage)
                job = service.submit(body)
                return self.send(202, {'id': job['id'], 'status': job['status'], 'status_url': '/v1/jobs/' + job['id']})
            except RuntimeError as exc:
                code = str(exc)
                busy = code in {'device_job_busy', 'device_mode_waiting'}
                return self.send(429 if is_chat and busy else 409,
                                 chat_error(code, kind='rate_limit_error' if busy else 'invalid_request_error') if is_chat else {'error': code})
            except InvalidRequest as exc:
                return self.send(404 if str(exc) == 'model_not_found' else 400, chat_error(str(exc), exc.param))
            except (ValueError, TypeError, TimeoutError):
                return self.send(400, chat_error('invalid_request') if is_chat else {'error': 'invalid_request'})

    server = ThreadingHTTPServer((config['bind'], config.get('port', 18347)), Handler)
    server.service = service
    if service.mode:
        threading.Thread(target=service.mode.run, daemon=True).start()
    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    server = make_server(json.loads(parser.parse_args().config.read_text()))
    def stop_service(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop_service)
    signal.signal(signal.SIGINT, stop_service)
    try:
        server.serve_forever()
    finally:
        server.service.shutdown()
        server.server_close()
