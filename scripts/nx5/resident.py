"""One persistent MiniCPM process and one central lease, retained between requests."""
import hashlib
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import threading
import time
import urllib.request
import uuid


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write_atomic(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(path)


def memory_available():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) / 1024
    raise RuntimeError('memory_unavailable')


def require_idle():
    for unit in ('siyuan-fleet-control.service', 'siyuan-image-comfy.service',
                 'nx5-ornith-35b-a3b-iq2s.service'):
        state = subprocess.check_output(['systemctl', '--user', 'show', unit,
                                        '--property=ActiveState,MainPID'], text=True)
        if 'ActiveState=inactive' not in state or 'MainPID=0' not in state:
            raise RuntimeError('conflicting_service_active')
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            exe = (entry/'exe').resolve(strict=True).name
            cmd = (entry/'cmdline').read_bytes().lower()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if exe.startswith('llama-') or (exe.startswith('python') and b'comfy' in cmd and b'main.py' in cmd):
            raise RuntimeError('inference_already_running')


class CentralLease:
    def __init__(self):
        private = Path.home()/'.config/siyuan-text-ingress'
        self.wrapper = json.loads((private/'wrapper.json').read_text())['central']
        self.ingress = json.loads((private/'ingress.json').read_text())['central']
        self.keys = {}
        for line in (private/'credentials.env').read_text().splitlines():
            for word in shlex.split(line, comments=True):
                if '=' in word:
                    key, value = word.split('=', 1)
                    self.keys[key] = value
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.group = '/internal/v1/resource-groups/nx5-igpu-group'

    def api(self, method, path, body=None, controller=False):
        cfg = self.wrapper if controller else self.ingress
        req = urllib.request.Request(cfg['base_url'] + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + self.keys[cfg['auth']['token_env']],
                     'Content-Type': 'application/json'})
        with self.opener.open(req, timeout=15) as response:
            return json.load(response)

    def observe(self, mode, active, operation=None):
        return self.api('POST', self.group + '/observation',
            {'mode': mode, 'state': 'ready' if mode == 'llm' else 'unknown',
             'active': active, 'operation_id': operation}, True)

    def acquire(self, request, revision, save):
        activity = self.api('GET', self.group + '/activity', controller=True)
        if activity['leases'] or activity['pending'] or activity['transition']:
            raise RuntimeError('central_resource_busy')
        require_idle()
        record = {'operation_id': 'op_' + uuid.uuid4().hex, 'request_digest': digest(request),
                  'status': 'reserving', 'created_at': time.time(), 'request': request}
        save(record)  # retain the operation even if the reservation response is lost
        body = {k: record[k] for k in ('operation_id', 'request_digest')}
        body['candidates'] = [{'backend_id': 'nx5-ssd-minicpm', 'mapping_version': revision,
            'resources': [{'resource_id': 'nx5-igpu', 'units': 1}], 'mode': 'llm',
            'probe_status': 'ready', 'probed_at': time.time()}]
        self.observe('off', 0)
        for _ in range(30):
            body['candidates'][0]['probed_at'] = time.time()
            reservation = self.api('POST', '/internal/v1/reservations', body)
            if reservation['status'] == 'held':
                lease = self.api('GET', '/internal/v1/leases/' + record['operation_id'])
                if lease['status'] != 'held' or lease['request_digest'] != record['request_digest']:
                    raise RuntimeError('lease_mismatch')
                record.update(status='held', held_at=time.time())
                save(record)
                return record
            if reservation['status'] == 'preparing':
                transition = self.api('GET', self.group + '/activity', controller=True)['transition']
                if transition['operation_id'] != record['operation_id'] or transition['target_mode'] != 'llm':
                    raise RuntimeError('lease_transition_mismatch')
                require_idle()
                self.observe('llm', 0, record['operation_id'])
            elif reservation['status'] != 'waiting':
                raise RuntimeError('lease_not_granted')
            time.sleep(1)
        raise RuntimeError('lease_reservation_pending')

    def release(self, record):
        require_idle()
        self.observe('off', 0)
        self.api('POST', '/internal/v1/leases/' + record['operation_id'] + '/release', {
            'terminal_status': 'succeeded', 'request_digest': record['request_digest'],
            'evidence_digest': digest({'process_exited': True, 'operation_id': record['operation_id']})})
        lease = self.api('GET', '/internal/v1/leases/' + record['operation_id'])
        if lease['status'] != 'released':
            raise RuntimeError('lease_release_unconfirmed')


class ResidentModel:
    def __init__(self, repo, config):
        self.repo, self.config = Path(repo), config
        self.folder = Path(config['state']).parent/'resident'
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.receipt_path = self.folder/'lease.json'
        self.record = json.loads(self.receipt_path.read_text()) if self.receipt_path.exists() else {}
        self.blocked = bool(self.record and self.record.get('status') != 'released')
        self.child = None
        self.central = None
        self.events = queue.Queue()
        self.guard_stop = threading.Event()
        self.close_lock = threading.RLock()
        self.stopping = threading.Event()
        self.active = False
        self.ready = False
        self.failure = None
        self.min_available_mib = None
        self.loaded_at = None
        self.log = None

    def save(self, record=None):
        if record is not None:
            self.record = record
        write_atomic(self.receipt_path, self.record)

    def health(self):
        loaded = self.child is not None and self.child.poll() is None
        return {'model_loaded': loaded and self.ready, 'resident_pid': self.child.pid if loaded else None,
                'resident_since': self.loaded_at if loaded else None,
                'lease_status': self.record.get('status', 'off'),
                'resident_error': self.failure, 'cache': 'single-slot exact-prefix with recurrent checkpoints'}

    def launch_config(self):
        root = Path(self.config['root'])
        cfg = json.loads((self.repo/'configs/nx5-minicpm-service.json').read_text())
        runtime = json.loads((self.repo/'configs/nx5-chat-runtime.json').read_text())
        model = root/cfg['model_dir']
        template = self.repo/cfg['chat_template']
        checks = {root/runtime['binary']: runtime['binary_sha256'], template: cfg['chat_template_sha256']}
        checks.update({root/name: expected for name, expected in runtime['libraries'].items()})
        checks.update({root/cfg['library_dir']/name: expected for name, expected in cfg['library_sha256'].items()})
        for path, expected in checks.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError('runtime_checksum_mismatch')
        for name, size in cfg['model_file_sizes'].items():
            if (model/name).stat().st_size != size:
                raise RuntimeError('model_file_changed')
        p = cfg['parameters']
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('LLAMA_MOE_', 'LLAMA_DRAFT_', 'LLAMA_ARG_', 'GGML_CUDA_', 'GGML_OP_'))}
        env.update(LD_LIBRARY_PATH=str(root/cfg['library_dir']) + ':' + str(root/'minicpm-les/llama.cpp-omni/build-jetson/bin'),
                   LLAMA_MOE_BANK_CAP=str(max(p['window'], (p['bank'] + 39)//40)),
                   LLAMA_MOE_DECODE_WINDOW=str(p['window']), LLAMA_MOE_IO_THREADS=str(p['io_threads']),
                   LLAMA_MOE_WINDOW_MAX_ROWS=str(p['rows']), LLAMA_MOE_F_NOCACHE='0',
                   LLAMA_MOE_PREWARM=str(root/'minicpm/prewarm_top128.txt'), GGML_CUDA_LES_MAPPED='1',
                   GGML_OP_OFFLOAD_MIN_BATCH=str(p['offload_min_batch']))
        command = [str(root/runtime['binary']), '--resident', '-m', str(model/'stripped.gguf'),
            '--moe-experts', str(model/'experts'), '--moe-slot-bank', str(p['bank']),
            '--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0', '-ngl', '99', '-fa', 'on',
            '-c', str(p['ctx']), '-b', '2048', '-ub', str(p['ubatch']), '-n', '2048',
            '--seed', str(p['seed']), '-fit', 'off', '--perf', '-lv', '3', '--no-escape',
            '--ctx-checkpoints', str(p['ctx_checkpoints']), '--cache-ram', '0',
            '--temp', '0.7', '--top-p', '0.95', '--top-k', '100', '--min-p', '0',
            '--presence-penalty', '0', '--repeat-penalty', '1.03', '--repeat-last-n', '-1',
            '--reasoning', 'on', '--chat-template-file', str(template),
            '--spec-type', 'draft-dspark', '--spec-draft-n-max', str(p['draft']),
            '--spec-draft-model', str(model/'dspark-Q4_K_M.gguf')]
        return command, env, cfg

    def ensure_loaded(self):
        with self.close_lock:
            if self.stopping.is_set():
                raise RuntimeError("service_stopped")
            return self._ensure_loaded()

    def _ensure_loaded(self):
        if self.blocked:
            raise RuntimeError('resident_requires_reconciliation')
        if self.child is not None and self.child.poll() is None and self.ready:
            return False
        command, env, cfg = self.launch_config()
        self.central = CentralLease()
        self.failure = None
        self.record = self.central.acquire({'model': 'minicpm-64k', 'resident_session': uuid.uuid4().hex,
            'release': json.loads((self.repo/'release.json').read_text())['source_commit']}, cfg['revision'], self.save)
        try:
            require_idle()
            self.log = (self.folder/(self.record['operation_id']+'.err')).open('w')
            self.events = queue.Queue()
            self.guard_stop = threading.Event()
            self.child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self.log, text=True, encoding='utf-8', bufsize=1, env=env)
            self.record.update(pid=self.child.pid, command=command)
            self.save()
            threading.Thread(target=self.read_events, args=(self.child, self.events), daemon=True).start()
            self.guard = threading.Thread(target=self.watch, args=(self.child, self.guard_stop, self.central, dict(self.record), cfg['parameters']['min_available_mib']), daemon=True)
            self.guard.start()
            event = self.next_event(time.monotonic() + 180)
            if event.get('type') != 'ready':
                raise RuntimeError('resident_load_failed')
            self.ready = True
            self.loaded_at = time.time()
            return True
        except Exception:
            self.close()
            raise

    @staticmethod
    def read_events(child, events):
        try:
            for line in child.stdout:
                events.put(json.loads(line))
        except Exception:
            events.put({'type': 'error', 'code': 'invalid_native_output'})
        finally:
            events.put(None)

    def next_event(self, deadline):
        while time.monotonic() < deadline:
            if self.stopping.is_set():
                raise RuntimeError("service_stopped")
            if self.failure:
                raise RuntimeError(self.failure)
            try:
                event = self.events.get(timeout=0.25)
            except queue.Empty:
                continue
            if event is None:
                raise RuntimeError('resident_process_exited')
            return event
        raise RuntimeError('inference_timeout')

    def watch(self, child, stop, central, record, floor):
        last_heartbeat = 0
        while not stop.wait(0.5):
            with self.close_lock:
                if stop.is_set() or self.child is not child:
                    return
                try:
                    available = memory_available()
                    self.min_available_mib = min(self.min_available_mib or available, available)
                    if available < floor:
                        raise RuntimeError('memory_guard')
                    if child.poll() is not None:
                        raise RuntimeError('resident_process_exited')
                    if time.monotonic() - last_heartbeat >= 10:
                        lease = central.api('GET', '/internal/v1/leases/' + record['operation_id'])
                        if lease['status'] != 'held' or lease['request_digest'] != record['request_digest']:
                            raise RuntimeError('resident_lease_lost')
                        central.observe('llm', int(self.active))
                        last_heartbeat = time.monotonic()
                except Exception as exc:
                    self.failure = str(exc) if isinstance(exc, RuntimeError) else 'resident_heartbeat_failed'
                    self.close(expected_child=child)
                    return

    def generate(self, chat, max_tokens, output, on_ready):
        started = time.monotonic()
        self.active = True
        try:
            cold = self.ensure_loaded()
            self.min_available_mib = memory_available()
            on_ready(self.child.pid)
            self.child.stdin.write(json.dumps({**chat, 'max_tokens': max_tokens}, ensure_ascii=False) + '\n')
            self.child.stdin.flush()
            with output.open('w') as stream:
                while True:
                    event = self.next_event(started + 2400)
                    stream.write(json.dumps(event, ensure_ascii=False) + '\n')
                    stream.flush()
                    if event['type'] == 'error':
                        if event.get('code') == 'context_length_exceeded':
                            raise ValueError('context_length_exceeded')
                        raise RuntimeError('inference_failed')
                    if event['type'] == 'final':
                        return event, {'cold_start': cold, 'resident_pid': self.child.pid,
                            'process_wall_s': time.monotonic()-started, 'min_available_mib': self.min_available_mib,
                            'lease_status': 'held', 'model_loaded_after': True}
        except ValueError:
            raise  # invalid context does not invalidate an existing native cache
        except Exception:
            self.close()
            raise
        finally:
            self.active = False

    def close(self, expected_child=None, permanent=False):
        if permanent:
            self.stopping.set()
        with self.close_lock:
            if expected_child is not None and self.child is not expected_child:
                return
            self.guard_stop.set()
            child = self.child
            if child is not None and child.poll() is None:
                try:
                    child.stdin.close()
                    child.wait(timeout=3)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    child.terminate()
                    try:
                        child.wait(timeout=7)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
            self.ready = False
            if self.record.get('status') == 'held' and self.central is not None:
                try:
                    self.central.release(self.record)
                    self.record.update(status='released', released_at=time.time())
                    self.save()
                except Exception:
                    self.blocked = True
                    self.record.update(status='unknown')
                    self.save()
            elif self.record and self.record.get('status') != 'released':
                self.blocked = True
            if self.log:
                self.log.close()
                self.log = None
