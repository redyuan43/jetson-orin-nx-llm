#!/usr/bin/env python3
"""Private, single-job MiniCPM admission and coordinated ComfyUI mode switching.

The central coordinator owns resource leases. This process never runs a model
before a held reservation, and never repeats an inference after restart.
"""
import argparse
import base64
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GROUP = 'nano5-igpu-group'
RESOURCE = 'nano5-igpu'
TERMINAL = {'succeeded', 'failed', 'cancelled'}
VERSION = 'nano5-minicpm-20261021-r1'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temp.chmod(0o600)
    os.replace(temp, path)


def model_answer(raw):
    parts = re.split(r'(?:[|/\\ \-]\x08){2,}', raw)
    if len(parts) < 2:
        raise ValueError('model_answer_boundary_missing')
    text = re.sub(r'[^\n]\x08', '', parts[-1]).split('[ Prompt:')[0].strip()
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


class Service:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['root'])
        self.repo = Path(config.get('deployment', self.root / 'deployment'))
        self.state = Path(config['state'])
        self.state.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.jobs = {}
        for p in self.state.glob('op_*.json'):
            job = json.loads(p.read_text())
            if job['status'] not in TERMINAL:
                job.update(status='unknown', error='restart_requires_reconciliation')
                atomic(p, job)
            self.jobs[job['id']] = job
        self.active = 0
        self.switching = False
        self.observed = {'mode': 'unknown', 'state': 'unknown', 'active': 1}
        self.observed_at = None
        self.central_ok = False
        self.commit = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def request(self, method, url, body=None, token=None, timeout=10):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + Path(token).read_text().strip()
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with self.opener.open(req, timeout=timeout) as r:
            return json.load(r)

    def central(self, method, path, body=None, controller=False):
        return self.request(method, self.config['central'] + path, body,
                            self.config['controller_token_file' if controller else 'executor_token_file'])

    def unit(self):
        r = subprocess.run(['systemctl', '--user', 'show', self.config['comfy_unit'],
                            '--property=ActiveState', '--value'], capture_output=True, text=True, check=True)
        return r.stdout.strip()

    def marker(self):
        p = self.state / 'mode.json'
        return json.loads(p.read_text()).get('mode', 'off') if p.exists() else 'off'

    def local_observation(self):
        if any(j['status'] == 'unknown' for j in self.jobs.values()):
            return {'mode': 'unknown', 'state': 'unknown', 'active': 1}
        state = self.unit()
        if state == 'active':
            if self.active or self.marker() == 'minicpm':
                raise RuntimeError('mode_conflict')
            q = self.request('GET', 'http://127.0.0.1:8188/queue')
            if any(not isinstance(q.get(k), list) for k in ('queue_running', 'queue_pending')):
                raise ValueError('incomplete_comfy_activity')
            return {'mode': 'comfy', 'state': 'ready', 'active': len(q['queue_running']) + len(q['queue_pending'])}
        if state not in {'inactive', 'failed'}:
            raise RuntimeError('comfy_state_unknown')
        # Reject unmanaged llama jobs too; a direct CLI remains an administrator tool.
        if not self.active:
            for p in Path('/proc').iterdir():
                if not p.name.isdigit():
                    continue
                try:
                    if (p / 'exe').resolve(strict=True).name.startswith('llama-'):
                        raise RuntimeError('unmanaged_inference_active')
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    pass
        return {'mode': self.marker(), 'state': 'ready', 'active': self.active}

    def transition(self):
        data = self.central('GET', '/internal/v1/mode-transitions', controller=True)
        return next((t for t in data['transitions'] if t['group_id'] == GROUP), None)

    @staticmethod
    def same_barrier(a, b):
        keys = ('group_id', 'executor_id', 'operation_id', 'target_mode')
        return bool(a and b and a.get('lease_barrier') is True and b.get('lease_barrier') is True
                    and all(a.get(k) == b.get(k) for k in keys)
                    and a.get('resources') == b.get('resources') == [RESOURCE])

    def report(self, observation, transition=None):
        body = dict(observation, operation_id=transition['operation_id'] if transition else None)
        self.central('POST', f'/internal/v1/resource-groups/{GROUP}/observation', body, controller=True)
        self.observed, self.observed_at, self.central_ok = observation, time.time(), True

    def switch(self, transition, before):
        target = transition['target_mode']
        if target not in {'comfy', 'minicpm', 'off'} or before['state'] != 'ready' or before['active']:
            return
        with self.lock:
            if self.active or self.switching:
                return
            self.switching = True
        try:
            self.report(dict(before, state='loading'), transition)
            if not self.same_barrier(transition, self.transition()):
                raise RuntimeError('transition_changed')
            now = self.local_observation()
            if now['state'] != 'ready' or now['active']:
                raise RuntimeError('device_busy')
            atomic(self.state / 'last-transition.json', dict(transition, phase='dispatching'))
            if before['mode'] == 'comfy':
                subprocess.run(['systemctl', '--user', 'stop', self.config['comfy_unit']], check=True, timeout=60)
                if self.unit() not in {'inactive', 'failed'}:
                    raise RuntimeError('comfy_stop_unconfirmed')
            if not self.same_barrier(transition, self.transition()):
                raise RuntimeError('transition_changed')
            atomic(self.state / 'mode.json', {'mode': 'minicpm' if target == 'minicpm' else 'off'})
            if target == 'comfy':
                subprocess.run(['systemctl', '--user', 'start', self.config['comfy_unit']], check=True, timeout=60)
            atomic(self.state / 'last-transition.json', dict(transition, phase='actions_complete'))
        finally:
            with self.lock:
                self.switching = False

    def observe_loop(self):
        while True:
            transition = None
            try:
                transition = self.transition()
                observation = self.local_observation()
                if transition and observation['mode'] != transition['target_mode']:
                    self.switch(transition, observation)
                    observation = self.local_observation()
                self.report(observation, transition)
            except Exception as exc:
                self.central_ok = False
                self.observed = {'mode': 'unknown', 'state': 'unknown', 'active': 1}
                try:
                    self.report(self.observed, transition)
                except Exception:
                    pass
                print('observation:', type(exc).__name__, flush=True)
            time.sleep(3)

    def save(self, job):
        atomic(self.state / (job['id'] + '.json'), job)

    def reserve(self, job):
        path = '/internal/v1/reservations'
        body = {'operation_id': job['id'], 'request_digest': job['digest'], 'candidates': [
            {'backend_id': 'nano5-' + job['mode'], 'mapping_version': VERSION,
             'resources': [{'resource_id': RESOURCE, 'units': 1}], 'mode': job['mode'],
             'probe_status': 'ready', 'probed_at': time.time()}]}
        # A reservation is idempotent under this original operation ID. Never
        # replace it with another ID after a timeout; inference is sent once.
        started = time.monotonic()
        while time.monotonic() - started < 2400:
            body['candidates'][0]['probed_at'] = time.time()
            r = self.central('POST', path, body)
            if r['status'] == 'held':
                lease = self.central('GET', f'/internal/v1/leases/{job["id"]}')
                if lease['status'] != 'held' or lease['operation_id'] != job['id']:
                    raise RuntimeError('lease_not_held')
                job['lease_digest'] = lease['request_digest']
                self.save(job)
                return
            if r['status'] not in {'waiting', 'preparing'}:
                raise RuntimeError('reservation_not_held')
            time.sleep(3)
        raise TimeoutError('reservation_wait_timeout')

    def release(self, job, status):
        self.central('POST', f'/internal/v1/leases/{job["id"]}/release',
                     {'terminal_status': status, 'evidence_digest': job['release_evidence'], 'request_digest': job['lease_digest']})

    def run(self, job, payload):
        held, started = False, time.monotonic()
        try:
            self.reserve(job)
            held = True
            with self.lock:
                observation = self.local_observation()
                if self.switching or observation['state'] != 'ready' or observation['mode'] != job['mode'] or observation['active']:
                    raise RuntimeError('mode_not_ready')
                self.active = 1
                job.update(status='running', started_at=time.time())
                self.save(job)
            if payload.get('action') == 'select_mode':
                job['result'] = {'mode': job['mode'], 'model_loaded': False}
            else:
                folder = self.state / job['id']
                folder.mkdir(mode=0o700, exist_ok=True)
                prompt = folder / 'prompt.txt'
                prompt.write_text(payload['prompt'])
                cmd = [sys.executable, str(self.repo / 'scripts/minicpm/run-nano5.py'), str(prompt),
                       '--root', str(self.root), '--profile', payload['profile'], '--tokens', str(payload['max_tokens']),
                       '--tag', 'result', '--output', str(folder)]
                if payload.get('image_base64'):
                    pic = folder / 'input.jpg'
                    pic.write_bytes(base64.b64decode(payload['image_base64'], validate=True))
                    cmd += ['--image', str(pic)]
                with (folder / 'driver.log').open('w') as f:
                    child = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
                    job['child_pid'] = child.pid
                    self.save(job)
                    rc = child.wait()
                metrics_path = folder / 'result.json'
                metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}
                if rc or metrics.get('stop_reason'):
                    raise RuntimeError('inference_failed')
                answer = model_answer((folder / 'result.out').read_text(errors='replace'))
                if not answer:
                    raise RuntimeError('empty_answer')
                job['result'] = {'text': answer, 'profile': payload['profile'],
                                 'inference_wall_s': metrics['wall_s'],
                                 'min_available_mib': metrics['min_available_kib'] / 1024,
                                 'metrics': metrics['metrics']}
            job.update(status='succeeded', finished_at=time.time(), wall_s=time.monotonic() - started)
        except Exception as exc:
            job.update(status='failed' if held else 'unknown', error=str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__,
                       finished_at=time.time(), wall_s=time.monotonic() - started)
        finally:
            with self.lock:
                self.active = 0
                terminal = job['status']
                if held:
                    job.update(status='releasing', terminal_status=terminal, release_evidence=digest(job))
                self.save(job)
            if held:
                try:
                    self.release(job, terminal)
                    job['status'] = terminal
                    self.save(job)
                except Exception:
                    # Preserve the hold, never submit another inference to recover it.
                    job.update(status='unknown', error='lease_release_unconfirmed')
                    self.save(job)

    def submit(self, payload):
        allowed = {'request_id', 'prompt', 'profile', 'max_tokens', 'image_base64', 'action', 'mode'}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError('invalid_fields')
        rid = payload.get('request_id')
        if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', rid):
            raise ValueError('request_id_required')
        mode = 'minicpm'
        if payload.get('action') == 'select_mode':
            mode = payload.get('mode')
            if mode not in {'comfy', 'minicpm'}:
                raise ValueError('invalid_mode')
        else:
            if payload.get('action') is not None or not isinstance(payload.get('prompt'), str) or not 1 <= len(payload['prompt']) <= 30000:
                raise ValueError('invalid_prompt')
            payload.setdefault('profile', 'vision-8k' if payload.get('image_base64') else 'long-64k')
            payload.setdefault('max_tokens', 2048)
            if payload['profile'] not in {'fast-8k', 'long-64k', 'vision-8k'} or type(payload['max_tokens']) is not int or not 1 <= payload['max_tokens'] <= 6144:
                raise ValueError('invalid_profile_or_output_limit')
            if bool(payload.get('image_base64')) != (payload['profile'] == 'vision-8k'):
                raise ValueError('vision_profile_requires_one_image')
            if payload.get('image_base64'):
                from PIL import Image
                raw = base64.b64decode(payload['image_base64'], validate=True)
                if len(raw) > 12 * 1024**2:
                    raise ValueError('image_too_large')
                with Image.open(io.BytesIO(raw)) as im:
                    if im.format not in {'JPEG', 'PNG'} or im.width * im.height > 16_000_000:
                        raise ValueError('invalid_image')
                    im.verify()
        identifier = 'op_' + hashlib.sha256(rid.encode()).hexdigest()[:32]
        fingerprint = digest(payload)
        with self.lock:
            old = self.jobs.get(identifier)
            if old:
                if old['digest'] != fingerprint:
                    raise RuntimeError('request_id_conflict')
                return old
            if any(j['status'] not in TERMINAL for j in self.jobs.values()):
                raise RuntimeError('device_job_busy')
            job = {'id': identifier, 'request_id': rid, 'digest': fingerprint, 'status': 'waiting',
                   'mode': mode, 'created_at': time.time()}
            self.jobs[identifier] = job
            self.save(job)
            threading.Thread(target=self.run, args=(job, payload), daemon=True).start()
            return job

    def health(self):
        return {'service_id': 'nano5-minicpm', 'version': VERSION, 'commit': self.commit,
                'mode': self.observed['mode'], 'state': self.observed['state'],
                'active': self.observed['active'], 'model_loaded': None if self.active else False,
                'inference_or_switch_request_active': bool(self.active),
                'observed_at': self.observed_at, 'coordinator_reachable': self.central_ok}


def serve(config):
    service = Service(config)
    token = Path(config['client_token_file']).read_text().strip()
    if len(token) < 32:
        raise ValueError('client_token_too_short')
    documents = Path(__file__).resolve().parents[2] / 'docs/nano5-service'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, body):
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def authorized(self):
            return hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode())

        def do_GET(self):
            if self.path == '/healthz':
                return self.send(200, service.health())
            if self.path == '/v1/service-catalog':
                catalog = json.loads((documents / 'catalog.json').read_text())
                catalog.update(release={'release': VERSION, 'commit': service.commit},
                               runtime=service.health(), guide_markdown=(documents / 'API.md').read_text())
                return self.send(200, catalog)
            if self.path == '/v1/client-contract':
                contract = json.loads((documents / 'openapi.json').read_text())
                contract['info']['version'] = service.commit
                return self.send(200, contract)
            if not self.authorized():
                return self.send(401, {'error': 'unauthorized'})
            match = re.fullmatch(r'/v1/jobs/(op_[a-f0-9]{32})', self.path)
            with service.lock:
                job = service.jobs.get(match[1]) if match else None
                return self.send(200, {k: v for k, v in job.items() if k not in {'child_pid', 'digest', 'lease_digest', 'release_evidence'}}) if job else self.send(404, {'error': 'not_found'})

        def do_POST(self):
            if not self.authorized():
                return self.send(401, {'error': 'unauthorized'})
            if self.path != '/v1/jobs':
                return self.send(404, {'error': 'not_found'})
            try:
                self.connection.settimeout(30)
                n = int(self.headers.get('Content-Length', '0'))
                if not 1 <= n <= 17 * 1024**2:
                    return self.send(413, {'error': 'invalid_body_size'})
                body = self.rfile.read(n)
                if len(body) != n:
                    raise ValueError('incomplete_body')
                job = service.submit(json.loads(body))
                return self.send(202, {'id': job['id'], 'status': job['status'], 'status_url': '/v1/jobs/' + job['id']})
            except RuntimeError as exc:
                return self.send(409, {'error': str(exc)})
            except Exception:
                return self.send(400, {'error': 'invalid_request'})

    threading.Thread(target=service.observe_loop, daemon=True).start()
    ThreadingHTTPServer((config['bind'], config.get('port', 18347)), Handler).serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    serve(json.loads(parser.parse_args().config.read_text()))
