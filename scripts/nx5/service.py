#!/usr/bin/env python3
"""Tailnet async admission for two mutually exclusive SSD models on nx5."""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROFILES = {
    'qwen36-128k': 'configs/qwen36-iq4xs-128k.json',
    'minicpm-64k': 'configs/nx5-minicpm-service.json',
}
TERMINAL = {'succeeded', 'failed'}


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

    def save(self, job):
        atomic(self.state / (job['id'] + '.json'), job)

    def public(self, job):
        return {k: v for k, v in job.items() if k not in {'digest', 'child_pid'}}

    def health(self):
        with self.lock:
            pending = [j for j in self.jobs.values() if j['status'] not in TERMINAL]
            blocked = any(j['status'] == 'unknown' for j in pending)
            active = next((j['profile'] for j in pending), None)
            return {'service_id': 'nx5-ssd-llm', **self.release,
                    'state': 'unknown' if blocked else 'busy' if pending else 'ready',
                    'mode': active or 'off', 'active': len(pending),
                    'model_loaded': None if pending else False,
                    'observed_at': time.time(), 'resource': 'nx5-igpu',
                    'availability_scope': 'API admission state; central lease and runtime verification are required before inference.'}

    def submit(self, payload):
        payload = validate(payload)
        identifier = 'op_' + hashlib.sha256(('nx5-ssd-llm:' + payload['request_id']).encode()).hexdigest()[:32]
        fingerprint = digest(payload)
        with self.lock:
            old = self.jobs.get(identifier)
            if old:
                if old['digest'] != fingerprint:
                    raise RuntimeError('request_id_conflict')
                return self.public(old)
            if any(j['status'] not in TERMINAL for j in self.jobs.values()):
                raise RuntimeError('device_job_busy')
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
            prompt = folder / 'prompt.txt'
            prompt.write_text(payload['prompt'])
            prompt.chmod(0o600)
            command = [sys.executable, str(self.repo / 'scripts/nx5/run-model.py'), str(prompt),
                       '--config', str(self.repo / PROFILES[payload['profile']]),
                       '--root', self.config['root'], '--output', str(folder),
                       '--tag', 'result', '--tokens', str(payload['max_tokens'])]
            with (folder / 'driver.log').open('w') as log:
                child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                with self.lock:
                    job['child_pid'] = child.pid
                    self.save(job)
                while child.poll() is None:
                    receipt = folder / 'result.lease.json'
                    if receipt.exists():
                        proof = json.loads(receipt.read_text())
                        if proof['status'] == 'running':
                            with self.lock:
                                if job['status'] == 'waiting':
                                    job.update(status='running', started_at=time.time())
                                    self.save(job)
                    try:
                        child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
            receipt = folder / 'result.lease.json'
            proof = json.loads(receipt.read_text()) if receipt.exists() else {}
            if proof and proof.get('lease_after') != 'released':
                raise RuntimeError('lease_requires_reconciliation')
            if child.returncode or not proof:
                raise ValueError('inference_failed')
            metrics = json.loads((folder / 'result.json').read_text())
            if metrics['rc'] or metrics['stop_reason']:
                raise ValueError('inference_failed')
            answer = answer_from_cli((folder / 'result.out').read_text())
            timing = '\n'.join(metrics['metrics'])
            generation = re.search(r'\|\s+eval time =\s+([\d.]+) ms /\s+(\d+) tokens.*?([\d.]+) tokens per second', timing)
            prompt_timing = re.search(r'prompt eval time =\s+([\d.]+) ms /\s+(\d+) tokens', timing)
            if not generation or not prompt_timing:
                raise ValueError('missing_metrics')
            with self.lock:
                job.update(status='succeeded', result={
                    'text': answer, 'profile': payload['profile'],
                    'context_capacity': metrics['args']['ctx'],
                    'input_tokens': int(prompt_timing[2]), 'output_tokens': int(generation[2]),
                    'prefill_s': float(prompt_timing[1]) / 1000,
                    'generation_s': float(generation[1]) / 1000,
                    'decode_tps': float(generation[3]), 'process_wall_s': metrics['wall_s'],
                    'min_available_mib': metrics['min_available_kib'] / 1024,
                    'finish_reason': 'length' if int(generation[2]) >= payload['max_tokens'] else 'stop',
                    'model_loaded_after': False, 'lease_status': 'released'})
        except Exception as exc:
            # Unknown process/lease state blocks all further admission. Never replay.
            receipt = folder / 'result.lease.json'
            proof = json.loads(receipt.read_text()) if receipt.exists() else {}
            running = 'child' in locals() and child.poll() is None
            uncertain = running or bool(proof and proof.get('lease_after') != 'released')
            with self.lock:
                job.update(status='unknown' if uncertain else 'failed',
                           error='reconciliation_required' if uncertain else 'inference_failed')
            print('job:', job['id'], type(exc).__name__, flush=True)
        finally:
            with self.lock:
                job['finished_at'] = time.time()
                self.save(job)


def serve(config):
    service = Service(config)
    token = Path(config['client_token_file']).read_text().strip()
    if len(token) < 32:
        raise ValueError('invalid_client_token')
    documents = service.repo / 'docs/nx5-service'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, value):
            raw = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
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
                return self.send(401, {'error': 'unauthorized'})
            match = re.fullmatch(r'/v1/jobs/(op_[a-f0-9]{32})', self.path)
            with service.lock:
                job = service.jobs.get(match[1]) if match else None
                return self.send(200, service.public(job)) if job else self.send(404, {'error': 'not_found'})

        def do_POST(self):
            if not self.authorized():
                return self.send(401, {'error': 'unauthorized'})
            if self.path != '/v1/jobs':
                return self.send(404, {'error': 'not_found'})
            try:
                self.connection.settimeout(20)
                size = int(self.headers.get('Content-Length', '0'))
                if not 1 <= size <= 2 * 1024**2:
                    return self.send(413, {'error': 'invalid_body_size'})
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError('incomplete_body')
                job = service.submit(json.loads(raw))
                return self.send(202, {'id': job['id'], 'status': job['status'], 'status_url': '/v1/jobs/' + job['id']})
            except RuntimeError as exc:
                return self.send(409, {'error': str(exc)})
            except (ValueError, TypeError, TimeoutError):
                return self.send(400, {'error': 'invalid_request'})

    ThreadingHTTPServer((config['bind'], config.get('port', 18347)), Handler).serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    serve(json.loads(parser.parse_args().config.read_text()))
