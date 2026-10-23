"""One persistent MiniCPM process and one central lease, retained between requests."""
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shlex
import subprocess
import threading
import time
import urllib.request
import uuid


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def profile_digest(model_profile, runtime_profile):
    model_profile = {key: value for key, value in model_profile.items()
                     if key not in {'revision', 'profile_digest'}}
    return digest({'model_profile': model_profile, 'runtime_profile': runtime_profile})


def write_atomic(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        stream.write(json.dumps(value, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.chmod(0o600)
    temp.replace(path)


def memory_available():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) / 1024
    raise RuntimeError('memory_unavailable')


def require_idle():
    for unit in ('siyuan-fleet-control.service', 'siyuan-image-comfy.service',
                 'nx5-ornith-35b-a3b-iq2s.service', 'nx5-flint.service'):
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
            {'mode': mode, 'state': 'ready',
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
                if lease['status'] != 'held' or lease['operation_id'] != record['operation_id']:
                    raise RuntimeError('lease_mismatch')
                record.update(status='held', held_at=time.time(), lease_digest=lease['request_digest'])
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
            'terminal_status': 'succeeded', 'request_digest': record['lease_digest'],
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
        self.model_revision = None
        self.log = None
        self.manager = None

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
        expected_profile_digest = profile_digest(cfg, runtime)
        if (cfg.get('profile_digest') != expected_profile_digest
                or cfg.get('revision') != expected_profile_digest):
            raise RuntimeError('profile_digest_mismatch')
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
        if os.environ.get('NX5_NATIVE_BACKTRACE_DIRECT') == '1':
            # ggml's gdb self-attach sees its parent blocked in waitpid; force its
            # in-process fork fallback for this native child only.
            env.pop('NX5_NATIVE_BACKTRACE_DIRECT', None)
            env['PATH'] = '/nonexistent'
        env.update(LD_LIBRARY_PATH=str(root/cfg['library_dir']) + ':' + str(root/'minicpm-les/llama.cpp-omni/build-jetson/bin'),
                   LLAMA_MOE_BANK_CAP=str(max(p['window'], (p['bank'] + 39)//40)),
                   LLAMA_MOE_DECODE_WINDOW=str(p['window']), LLAMA_MOE_IO_THREADS=str(p['io_threads']),
                   LLAMA_MOE_WINDOW_MAX_ROWS=str(p['rows']), LLAMA_MOE_F_NOCACHE='0',
                   LLAMA_MOE_PREWARM=str(root/'minicpm/prewarm_top128.txt'), GGML_CUDA_LES_MAPPED='1',
                   GGML_OP_OFFLOAD_MIN_BATCH=str(p['offload_min_batch']))
        command = [str(root/runtime['binary']), '--resident', '-m', str(model/'stripped.gguf'),
            '--moe-experts', str(model/'experts'), '--moe-slot-bank', str(p['bank']),
            '--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0', '-ngl', '99', '-fa', 'on',
            '--cache-type-k-draft', p['draft_cache_k'],
            '--cache-type-v-draft', p['draft_cache_v'],
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
        self.central = self.manager.wire() if self.manager else CentralLease()
        self.failure = None
        acquire = self.manager.acquire if self.manager else self.central.acquire
        self.record = acquire({'model': 'minicpm-64k', 'resident_session': uuid.uuid4().hex,
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
            diagnostic_path = self.folder/(self.record['operation_id'] + '.backtrace')
            threading.Thread(target=self.read_events,
                args=(self.child, self.events, diagnostic_path), daemon=True).start()
            self.guard = threading.Thread(target=self.watch, args=(self.child, self.guard_stop, self.central, dict(self.record), cfg['parameters']['min_available_mib']), daemon=True)
            self.guard.start()
            event = self.next_event(time.monotonic() + 180)
            if event.get('type') != 'ready':
                raise RuntimeError('resident_load_failed')
            self.ready = True
            self.model_revision = cfg['revision']
            self.loaded_at = time.time()
            return True
        except Exception:
            self.close()
            raise

    @staticmethod
    def read_events(child, events, diagnostic_path=None):
        capturing_backtrace = False
        diagnostic = None
        diagnostic_bytes = 0
        diagnostic_limit = 64 * 1024
        max_line_chars = 2 * 1024 * 1024
        invalid_output_reported = False
        invalid_output_seen = False
        ansi_escape = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')

        def report_invalid_output():
            nonlocal invalid_output_reported
            if not invalid_output_reported:
                events.put({'type': 'error', 'code': 'invalid_native_output'})
                invalid_output_reported = True

        def bounded_lines(stream):
            """Read bounded chunks so a missing newline cannot grow memory without limit."""
            pending = ''
            oversized = False
            oversized_header = False
            while True:
                chunk = stream.readline(4096)
                if not chunk:
                    if pending or oversized:
                        yield pending, oversized, oversized_header
                    return
                if not pending and not oversized and chunk.startswith('Attaching to process '):
                    oversized_header = False
                if not oversized:
                    pending += chunk
                    if len(pending) > max_line_chars:
                        oversized = True
                        oversized_header = pending.startswith('Attaching to process ')
                        pending = pending[:128] if oversized_header else ''
                if chunk.endswith('\n'):
                    yield pending, oversized, oversized_header
                    pending, oversized, oversized_header = '', False, False

        def safe_diagnostic_line(line, oversized=False):
            if oversized:
                if line.startswith('Attaching to process '):
                    match = re.match(r'^Attaching to process (\d+)\b', line)
                    return f'Attaching to process {match.group(1)}\n' if match else None
                return '[oversized line omitted]\n'
            header = re.match(r'^Attaching to process (\d+)\s*$', line.rstrip('\r\n'))
            if header:
                return f'Attaching to process {header.group(1)}\n'
            thread = re.match(r'^Thread (\d+) \(Thread (0x[0-9a-fA-F]+)\):\s*$', line.rstrip('\r\n'))
            if thread:
                return f'Thread {thread.group(1)} (Thread {thread.group(2)}):\n'
            inferior = re.match(r'^\[Inferior (\d+) \(process (\d+)\) (detached|exited)\]\s*$', line.rstrip('\r\n'))
            if inferior:
                return f'[Inferior {inferior.group(1)} (process {inferior.group(2)}) {inferior.group(3)}]\n'
            frame = re.match(r'^(#\d+)\s+(.+)$', line.rstrip('\r\n'))
            if frame:
                rest = frame.group(2)
                symbol = rest.split(' (', 1)[0]
                location = re.search(r'\) at ([\w./+\\ -]+:\d+(?::\d+)?)$', rest)
                suffix = f' at {location.group(1)}' if location else ''
                if symbol and re.fullmatch(r'[\w:~<>,*&. -]+', symbol):
                    return f'{frame.group(1)} {symbol}{suffix}\n'
            return None

        try:
            for line, oversized, oversized_header in bounded_lines(child.stdout):
                normalized_line = ansi_escape.sub('', line)
                if capturing_backtrace:
                    if diagnostic_bytes < diagnostic_limit:
                        safe_line = safe_diagnostic_line(normalized_line, oversized)
                        if safe_line:
                            encoded = safe_line.encode('utf-8', errors='replace')
                            encoded = encoded[:diagnostic_limit-diagnostic_bytes]
                            diagnostic.write(encoded.decode('utf-8', errors='ignore'))
                            diagnostic.flush()
                            diagnostic_bytes += len(encoded)
                    if '[Inferior ' in normalized_line and ('detached' in normalized_line or 'exited' in normalized_line):
                        report_invalid_output()
                        capturing_backtrace = False
                    continue
                try:
                    if oversized:
                        raise ValueError('native_line_too_long')
                    event = json.loads(line)
                except (TypeError, ValueError):
                    gdb_start = (re.match(r'^Attaching to process \d+\b', normalized_line)
                                 or re.match(r'^Thread \d+ \(Thread ', normalized_line)
                                 or re.match(r'^New LWP \d+\b', normalized_line)
                                 or re.match(r'^\[New LWP \d+\]', normalized_line)
                                 or re.match(r'^#0\s+', normalized_line))
                    if diagnostic_path is not None and gdb_start:
                        invalid_output_seen = True
                        diagnostic_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        fd = os.open(diagnostic_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                            getattr(os, 'O_NOFOLLOW', 0), 0o600)
                        diagnostic = os.fdopen(fd, 'w', encoding='utf-8')
                        os.fchmod(fd, 0o600)
                        capturing_backtrace = True
                        safe_line = safe_diagnostic_line(normalized_line, oversized)
                        if safe_line:
                            encoded = safe_line.encode('utf-8', errors='replace')[:diagnostic_limit]
                            diagnostic.write(encoded.decode('utf-8', errors='ignore'))
                            diagnostic.flush()
                            diagnostic_bytes = len(encoded)
                        continue
                    invalid_output_seen = True
                    continue
                events.put(event)
        except Exception:
            invalid_output_seen = True
        finally:
            if invalid_output_seen:
                report_invalid_output()
            if diagnostic is not None:
                diagnostic.close()
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
                        if lease['status'] != 'held' or lease['request_digest'] != record['lease_digest']:
                            raise RuntimeError('resident_lease_lost')
                        if not self.manager:
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
            deadline = time.monotonic() + 2400
            self.min_available_mib = memory_available()
            on_ready(self.child.pid)
            self.child.stdin.write(json.dumps({**chat, 'max_tokens': max_tokens}, ensure_ascii=False) + '\n')
            self.child.stdin.flush()
            with output.open('w') as stream:
                while True:
                    event = self.next_event(deadline)
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
            self.model_revision = None
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


class ManagedComfy:
    """NX5's opt-in lifecycle owner, sharing the SSD service's admission lock.

    The legacy Ornith switch/controller must remain stopped. Central reservations
    choose the next mode; this owner never manufactures a mode transition.
    """
    GROUP = 'nx5-igpu-group'
    RESOURCE = 'nx5-igpu'
    UNIT = 'siyuan-image-comfy.service'
    FLINT_UNIT = 'nx5-flint.service'
    FLINT_GATE_UNIT = 'nx5-flint-ingress.service'
    MEDIA_EXECUTOR = 'nx6-image-executor'

    def __init__(self, service):
        self.service = service
        self.central = None
        self.stop = threading.Event()
        self.path = service.state.parent/'managed-comfy.json'
        self.intent = json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.intent.get('phase') in {'dispatching', 'unknown'}:
            self.save(dict(self.intent, phase='unknown'))
        self.observed = {'mode': 'unknown', 'state': 'unknown', 'active': 1}
        self.policy_profiles = service.config.get('policy_profiles', {})
        if (not isinstance(self.policy_profiles, dict)
                or set(self.policy_profiles) - {'llm', 'flint', 'comfy'}):
            raise ValueError('policy_profiles_invalid')
        for mode, profile in self.policy_profiles.items():
            if (not isinstance(profile, dict) or set(profile) != {'profile_id', 'version'}
                    or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', v)
                           for v in profile.values())):
                raise ValueError('policy_profile_invalid')
            if mode == 'llm':
                native = json.loads((service.repo/'configs/nx5-minicpm-service.json').read_text())
                if profile['version'] != native['revision']:
                    raise ValueError('policy_profile_version_mismatch')
        self.policy_path = service.state.parent/'policy-demand.json'
        self.policy_state = json.loads(self.policy_path.read_text()) if self.policy_path.exists() else {}
        self.policy_worker = None
        if self.policy_state.get('phase') in {'loading', 'yielding', 'unknown'}:
            self.save_policy(dict(self.policy_state, phase='unknown'))

    def wire(self):
        if self.central is None:
            self.central = CentralLease()
        return self.central

    def save(self, intent):
        write_atomic(self.path, intent)
        self.intent = intent

    def health(self):
        return dict(self.observed, phase=self.intent.get('phase', 'idle'),
                    policy_state=self.policy_state.get('phase', 'idle'))

    def save_policy(self, value):
        write_atomic(self.policy_path, value)
        self.policy_state = value

    @staticmethod
    def policy_identity(value):
        return {key:value.get(key) for key in ('operation_id', 'profile_id', 'profile_version',
            'mode', 'activation', 'policy_revision', 'executor_id')}

    def policy_demand(self, row):
        if not self.policy_profiles or row.get('fresh') is not True:
            return None
        value = row.get('policy_demand')
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RuntimeError('policy_demand_invalid')
        profile = self.policy_profiles.get(value.get('mode'))
        if value.get('mode') == 'off':
            # Off retains the default profile's exact identity; it never loads it.
            profile = next((p for p in self.policy_profiles.values()
                if p['profile_id'] == value.get('profile_id')
                and p['version'] == value.get('profile_version')), None)
        if (not profile or value.get('profile_id') != profile['profile_id']
                or value.get('profile_version') != profile['version']
                or value.get('activation') != ('resident' if value['mode'] in {'llm','flint'} else 'controller')
                or value.get('executor_id') != 'device-policy:' + self.GROUP
                or not isinstance(value.get('operation_id'), str)
                or not re.fullmatch(r'op_[a-f0-9]{32}', value['operation_id'])
                or type(value.get('policy_revision')) is not int or value['policy_revision'] < 1
                or value.get('state') not in {'requested', 'transitioning', 'unknown'}):
            raise RuntimeError('policy_demand_not_approved')
        previous = self.policy_state.get('demand', {})
        same = self.policy_identity(value) == self.policy_identity(previous)
        if value['state'] == 'unknown' and not same:
            return None
        if previous.get('operation_id') == value['operation_id'] and not same:
            raise RuntimeError('policy_demand_identity_conflict')
        if self.policy_state.get('phase') == 'loading' and not same:
            raise RuntimeError('policy_demand_changed_during_load')
        return value

    def policy_comfy_transition(self, transition, policy):
        return bool(policy and policy['mode'] in {'comfy', 'flint', 'off'}
            and transition.get('executor_id') == 'device-policy:' + self.GROUP
            and transition.get('operation_id') == policy['operation_id']
            and transition.get('target_mode') == policy['mode']
            and (policy['state'] in {'requested', 'transitioning'} or
                 (self.intent.get('phase') in {'actions_complete', 'completed'}
                  and self.intent.get('operation_id') == policy['operation_id'])))

    def check_policy_barrier(self, transition, policy):
        if policy:
            current = self.policy_demand(self.checked_activity(require_ready=False))
            if (not current or self.policy_identity(current) != self.policy_identity(policy)
                    or not self.policy_comfy_transition(transition, current)):
                raise RuntimeError('policy_demand_changed')
        self.check_barrier(transition)

    def start_policy_load(self, policy):
        previous = self.policy_state.get('demand', {})
        if (self.policy_identity(previous) == self.policy_identity(policy)
                and self.policy_state.get('phase') in {'loading', 'applied', 'unknown'}):
            return
        self.save_policy({'demand':policy, 'phase':'loading'})
        self.policy_worker = threading.Thread(target=self.load_policy, args=(dict(policy),), daemon=True)
        self.policy_worker.start()

    def load_policy(self, policy):
        """The regular reservation loader must leave tick free to service its barrier."""
        try:
            with self.service.lock:
                if (self.service.closing or self.stop.is_set() or self.service.resident.blocked
                        or any(j['status'] not in {'succeeded','failed'} for j in self.service.jobs.values())):
                    raise RuntimeError('policy_load_not_idle')
                row = self.checked_activity(require_ready=False)
                current = self.policy_demand(row)
                if not current or self.policy_identity(current) != self.policy_identity(policy):
                    self.save_policy({'demand':policy, 'phase':'cancelled'})
                    return
                if current['state'] == 'unknown':
                    self.save_policy({'demand':policy, 'phase':'unknown'})
                    return
                if (row.get('state') != 'ready' or row.get('active') != 0
                        or row['leases'] or row['pending']
                        or (self.unit() == 'active' and self.queue())):
                    self.save_policy({'demand':policy, 'phase':'deferred'})
                    return
            # ensure_loaded owns close_lock while waiting for its original nx5-text
            # reservation. Never hold service.lock across that wait.
            self.service.resident.ensure_loaded()
            with self.service.lock:
                resident = self.service.resident
                if (not resident.health()['model_loaded']
                        or resident.model_revision != policy['profile_version']):
                    raise RuntimeError('policy_model_not_loaded')
                self.save_policy({'demand':policy, 'phase':'applied',
                                  'resident_operation_id':resident.record.get('operation_id')})
        except Exception as exc:
            with self.service.lock:
                self.save_policy({'demand':policy, 'phase':'unknown', 'error':type(exc).__name__})

    def profile_observation(self, observation):
        profile = self.policy_profiles.get(observation['mode'])
        if not profile:
            return observation
        loaded = None
        if observation['mode'] == 'llm':
            resident = self.service.resident
            loaded = bool(resident.health()['model_loaded'] and resident.model_revision == profile['version'])
        elif observation['mode'] == 'flint':
            loaded = bool(self.flint_runtime()['ready'])
        return dict(observation, profile_id=profile['profile_id'], profile_version=profile['version'],
                    model_loaded=loaded)

    def activity(self):
        return self.wire().api('GET', self.wire().group+'/activity', controller=True)

    def checked_activity(self, require_ready=True):
        row = self.activity()
        if (row.get('group_id') != self.GROUP or row.get('resources') != [self.RESOURCE]
                or (require_ready and (row.get('fresh') is not True or row.get('state') != 'ready'))
                or any(type(row.get(k)) is not list for k in ('leases', 'pending'))):
            raise RuntimeError('central_group_unknown')
        return row

    def demand(self, row):
        return next((p for p in row['pending'] if p.get('executor_id') == self.MEDIA_EXECUTOR
                     and p.get('status') == 'waiting'
                     and any(r.get('resource_id') == self.RESOURCE for r in p.get('resources', []))), None)

    def flint_gate_activity(self):
        url = self.service.config.get('flint_gate_activity_url', 'http://127.0.0.1:18085/internal/activity')
        token_path = Path(self.service.config.get('flint_controller_token_file',
            str(Path.home()/'.config/siyuan-nx5-flint/controller.key')))
        token = token_path.read_text().strip()
        if len(token) < 32:
            raise RuntimeError('flint_controller_credential_invalid')
        request = urllib.request.Request(url, headers={'Authorization':'Bearer '+token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return json.load(response)
        except Exception as exc:
            raise RuntimeError('flint_gate_unavailable') from exc

    def demand_for_mode(self, mode):
        if mode != 'flint':
            raise RuntimeError('unsupported_resident_profile')
        if self.flint_gate_unit() != 'active':
            return None
        row = self.flint_gate_activity()
        if (row.get('group_id') != self.GROUP or row.get('resources') != [self.RESOURCE]
                or row.get('mode') != 'flint' or row.get('profile_id') != 'nx5-flint'
                or not isinstance(row.get('profile_digest'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', row['profile_digest'])
                or any(type(row.get(key)) is not int or row[key] < 0 for key in ('active','unknown','waiting'))
                or type(row.get('observed_at')) not in (int,float)
                or not 0 <= time.time()-row['observed_at'] <= 60
                or type(row.get('ingress_closed')) is not bool or type(row.get('enabled')) is not bool):
            raise RuntimeError('flint_gate_activity_unknown')
        expected = self.policy_profiles.get('flint')
        if (not expected or row.get('profile_id') != expected.get('profile_id')
                or row.get('profile_digest') != expected.get('version')):
            raise RuntimeError('flint_gate_profile_mismatch')
        if not row['waiting'] or row['active'] or row['unknown']:
            return None
        public = row.get('public_enabled')
        qualification = row.get('qualification_only')
        owner_admission = row.get('owner_admission')
        if (type(public) is not bool or type(qualification) is not bool
                or type(owner_admission) is not bool or row['enabled'] is not True
                or not ((public and not qualification and owner_admission)
                        or (qualification and not public and not owner_admission))):
            raise RuntimeError('flint_gate_admission_unqualified')
        return row

    def own_flint_transition(self, transition, row, gate):
        if (not transition or transition.get('target_mode') != 'flint'
                or transition.get('executor_id') != 'nx5-text'
                or not gate or type(gate.get('waiting')) is not int or gate['waiting'] < 1
                or gate.get('active') != 0 or gate.get('unknown') != 0):
            return False
        if any(pending.get('status') not in {'waiting', 'preparing'}
                or pending.get('resources') != [{'resource_id': self.RESOURCE, 'units': 1}]
                for pending in row['pending']):
            return False
        matches = [pending for pending in row['pending']
            if pending.get('executor_id') == transition.get('executor_id')
            and pending.get('operation_id') == transition.get('operation_id')
            and pending.get('status') in {'waiting', 'preparing'}
            and pending.get('resources') == [{'resource_id': self.RESOURCE, 'units': 1}]]
        return len(matches) == 1

    def gate_text(self):
        if self.policy_state.get('phase') == 'unknown':
            raise RuntimeError('policy_requires_reconciliation')
        if self.policy_state.get('phase') == 'loading':
            raise RuntimeError('device_mode_waiting')
        if self.policy_profiles:
            policy = self.policy_demand(self.checked_activity(require_ready=False))
            if policy and policy['mode'] in {'comfy', 'flint', 'off'} and policy['state'] != 'unknown':
                raise RuntimeError('device_mode_waiting')
        if self.intent.get('phase') == 'unknown':
            raise RuntimeError('lifecycle_requires_reconciliation')
        if self.intent.get('phase') in {'yielding', 'dispatching', 'actions_complete'}:
            raise RuntimeError('device_mode_waiting')
        if self.service.resident.health()['model_loaded']:
            if self.demand_for_mode('flint'):
                self.save({'phase':'yielding', 'target_mode':'flint'})
                raise RuntimeError('device_mode_waiting')
            demand = self.demand(self.checked_activity())
            if demand:
                self.save({'phase': 'yielding', 'media_operation_id': demand['operation_id']})
                raise RuntimeError('device_mode_waiting')

    def transition(self):
        rows = self.wire().api('GET', '/internal/v1/mode-transitions', controller=True)['transitions']
        return next((t for t in rows if t.get('group_id') == self.GROUP), None)

    def same_barrier(self, a, b):
        return bool(a and b and a.get('lease_barrier') is True and b.get('lease_barrier') is True
                    and a.get('group_id') == b.get('group_id') == self.GROUP
                    and a.get('resources') == b.get('resources') == [self.RESOURCE]
                    and all(a.get(k) == b.get(k) for k in ('executor_id','operation_id','target_mode')))

    def check_barrier(self, original):
        if not self.same_barrier(original, self.transition()):
            raise RuntimeError('transition_changed')

    def unit(self):
        result = subprocess.run(['systemctl','--user','show',self.UNIT,'--property=ActiveState','--value'],
                                capture_output=True, text=True, check=True, timeout=10)
        state = result.stdout.strip()
        if state not in {'active', 'inactive'}:
            raise RuntimeError('comfy_state_unknown')
        return state

    def flint_unit(self):
        result = subprocess.run(['systemctl','--user','show',self.FLINT_UNIT,'--property=ActiveState','--value'],
                                capture_output=True, text=True, check=True, timeout=10)
        state = result.stdout.strip()
        if state not in {'active', 'inactive'}:
            raise RuntimeError('flint_state_unknown')
        return state

    def flint_gate_unit(self):
        result = subprocess.run(['systemctl','--user','show',self.FLINT_GATE_UNIT,'--property=ActiveState','--value'],
                                capture_output=True, text=True, check=True, timeout=10)
        state = result.stdout.strip()
        if state not in {'active', 'inactive'}:
            raise RuntimeError('flint_gate_state_unknown')
        return state

    def action(self, verb):
        subprocess.run(['systemctl','--user',verb,self.UNIT], check=True, timeout=60)

    def flint_action(self, verb):
        subprocess.run(['systemctl','--user',verb,self.FLINT_UNIT], check=True, timeout=60)

    def flint_runtime(self):
        token_path = Path(self.service.config.get('flint_native_token_file',
            str(Path.home()/'.config/siyuan-nx5-flint/native.key')))
        token = token_path.read_text().strip()
        if len(token) < 32:
            raise RuntimeError('flint_native_credential_invalid')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get(path):
            request = urllib.request.Request('http://127.0.0.1:18086'+path,
                headers={'Authorization':'Bearer '+token})
            with opener.open(request, timeout=5) as response:
                return json.load(response)
        try:
            health = get('/health')
            models = get('/v1/models')
            slots = get('/slots')
        except Exception as exc:
            raise RuntimeError('flint_native_unavailable') from exc
        model_ids = {row.get('id') for row in models.get('data', []) if isinstance(row, dict)}
        if health.get('status') != 'ok' or model_ids != {'flint'} or not isinstance(slots, list) or not slots:
            raise RuntimeError('flint_native_identity_mismatch')
        if any(not isinstance(row, dict) or type(row.get('is_processing')) is not bool for row in slots):
            raise RuntimeError('flint_native_slots_unknown')
        return {'ready':True, 'active':sum(row['is_processing'] for row in slots), 'slots':len(slots)}

    def wait_flint_ready(self, timeout=90):
        deadline = time.monotonic()+timeout
        last = None
        while time.monotonic() < deadline:
            try:
                return self.flint_runtime()
            except RuntimeError as exc:
                last = exc
                if str(exc) not in {'flint_native_unavailable'}:
                    raise
                time.sleep(1)
        raise RuntimeError('flint_readiness_timeout') from last

    def request(self, path):
        request = urllib.request.Request('http://127.0.0.1:8188'+path)
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=8) as response:
            return json.load(response)

    def queue(self):
        row = self.request('/queue')
        if not isinstance(row, dict) or any(type(row.get(k)) is not list for k in ('queue_running','queue_pending')):
            raise RuntimeError('incomplete_comfy_activity')
        return len(row['queue_running']) + len(row['queue_pending'])

    def report(self, observation, transition=None):
        observation = self.profile_observation(observation)
        result = self.wire().api('POST', self.wire().group+'/observation',
                                dict(observation, operation_id=transition['operation_id'] if transition else None),
                                controller=True)
        self.observed = observation
        return result

    def tick(self):
        with self.service.lock:
            resident = self.service.resident
            if self.policy_state.get('phase') == 'unknown':
                raise RuntimeError('policy_requires_reconciliation')
            if self.intent.get('phase') == 'unknown':
                raise RuntimeError('lifecycle_requires_reconciliation')
            if self.service.closing or resident.blocked or any(j['status'] == 'unknown' for j in self.service.jobs.values()):
                raise RuntimeError('resident_requires_reconciliation')
            row = self.checked_activity(require_ready=False)
            policy = self.policy_demand(row)
            transition = self.transition()
            native = resident.child is not None and resident.child.poll() is None
            unit = self.unit()
            flint_unit = self.flint_unit()
            if (native and unit == 'active') or (flint_unit == 'active' and unit == 'active') or (native and flint_unit == 'active'):
                raise RuntimeError('mode_conflict')
            demand = self.demand(row) if row.get('fresh') is True and row.get('state') == 'ready' else None
            flint_demand = self.demand_for_mode('flint')
            pending_text = any(j['status'] not in {'succeeded','failed'} for j in self.service.jobs.values())
            policy_loading = self.policy_state.get('phase') == 'loading'
            policy_yield = bool(policy and policy['mode'] in {'comfy', 'flint', 'off'} and policy['state'] != 'unknown'
                and row.get('state') == 'ready' and row.get('active') == 0
                and (policy['mode'] != 'off' or (not row['pending'] and not transition))
                and resident.record.get('status') == 'held'
                and row['leases'] and all(p.get('executor_id') == 'nx5-text'
                    and p.get('operation_id') == resident.record.get('operation_id') for p in row['leases']))
            if native and not pending_text and not resident.active and not policy_loading and (demand or flint_demand or policy_yield):
                if policy_yield:
                    self.save_policy({'demand':policy, 'phase':'yielding'})
                self.save({'phase':'yielding', 'media_operation_id':demand['operation_id'],
                           'resident_operation_id':resident.record['operation_id']} if demand else
                          {'phase':'yielding', 'flint_profile_id':flint_demand['profile_id'],
                           'resident_operation_id':resident.record['operation_id']} if flint_demand else
                          {'phase':'yielding', 'policy_operation_id':policy['operation_id'],
                           'resident_operation_id':resident.record['operation_id']})
                try:
                    resident.close()
                except Exception:
                    if policy_yield:
                        self.save_policy({'demand':policy, 'phase':'unknown'})
                    raise
                if resident.blocked:
                    if policy_yield:
                        self.save_policy({'demand':policy, 'phase':'unknown'})
                    raise RuntimeError('resident_requires_reconciliation')
                if policy_yield:
                    self.save_policy({'demand':policy, 'phase':'yielded'})
                return
            if self.intent.get('phase') == 'yielding' and not demand and not transition:
                self.save(dict(self.intent, phase='completed'))
            if transition:
                target = transition.get('target_mode')
                own_text = (target in {'llm', 'flint'} and transition.get('executor_id') == 'nx5-text'
                            and transition.get('operation_id') == resident.record.get('operation_id')
                            and resident.record.get('mode', 'llm') == target)
                own_flint = self.own_flint_transition(transition, row, flint_demand)
                policy_transition = self.policy_comfy_transition(transition, policy)
                if not (target == 'comfy' and transition.get('executor_id') == self.MEDIA_EXECUTOR) and not own_text and not own_flint and not policy_transition:
                    raise RuntimeError('unsupported_transition')
                # A text admission can race the central off barrier while its
                # local job/loader is still waiting for the original reservation.
                # The barrier prevents that reservation becoming held until off
                # completes; no native child, held lease or running job is spared.
                off_waiting_text = (target == 'off' and policy_transition and pending_text
                    and not native and not resident.ready and not policy_loading
                    and resident.record.get('status') in {None, 'released', 'reserving'}
                    and all(j['status'] in {'waiting', 'succeeded', 'failed'} for j in self.service.jobs.values()))
                if native or row['leases'] or (pending_text and not own_text and not own_flint and not off_waiting_text):
                    return
                # Once the original off barrier is granted, later waiting work
                # must wait for its completion instead of deadlocking the owner.
                if target == 'off' and any(p.get('status') != 'waiting' for p in row['pending']):
                    return
                recorded_off = (self.intent.get('operation_id') == transition['operation_id']
                    and self.intent.get('phase') in {'actions_complete', 'completed'})
                if target == 'off' and ((resident.active and not off_waiting_text) or (not recorded_off and
                        (row.get('state') != 'ready' or type(row.get('active')) is not int or row['active'] != 0))):
                    return
                if target in {'llm', 'off'} and unit == 'active' and self.queue():
                    return
                self.check_policy_barrier(transition, policy if policy_transition else None)
                if policy_transition:
                    # Retain the demand contract before a lifecycle effect, so a
                    # later unknown central report can reconcile only this ID.
                    self.save_policy({'demand':policy, 'phase':'transitioning'})
                if target != 'flint' and flint_unit == 'active':
                    runtime = self.flint_runtime()
                    if runtime['active']:
                        return
                    self.check_policy_barrier(transition, policy if policy_transition else None)
                    self.save(dict(transition, phase='dispatching', step='stop-flint'))
                    try:
                        self.flint_action('stop')
                    except Exception:
                        self.save(dict(self.intent, phase='unknown'))
                        raise
                    self.save(dict(transition, phase='actions_complete', step='stop-flint'))
                    flint_unit = self.flint_unit()
                    if flint_unit != 'inactive':
                        raise RuntimeError('lifecycle_effect_unconfirmed')
                if target == 'flint' and unit == 'active':
                    if self.queue():
                        return
                    self.check_policy_barrier(transition, policy if policy_transition else None)
                    self.save(dict(transition, phase='dispatching', step='stop-comfy'))
                    try:
                        self.action('stop')
                    except Exception:
                        self.save(dict(self.intent, phase='unknown'))
                        raise
                    self.save(dict(transition, phase='actions_complete', step='stop-comfy'))
                    unit = self.unit()
                    if unit != 'inactive':
                        raise RuntimeError('lifecycle_effect_unconfirmed')
                target_unit = self.FLINT_UNIT if target == 'flint' else self.UNIT
                action_for_target = self.flint_action if target == 'flint' else self.action
                target_state = flint_unit if target == 'flint' else unit
                desired_state = 'active' if target in {'comfy', 'flint'} else 'inactive'
                verb = 'start' if desired_state == 'active' else 'stop'
                needs_action = target_state != desired_state
                if needs_action:
                    if (self.intent.get('phase') in {'actions_complete', 'completed'}
                            and self.intent.get('operation_id') == transition['operation_id']
                            and self.intent.get('step') not in {'stop-comfy', 'stop-flint'}):
                        raise RuntimeError('lifecycle_effect_unconfirmed')
                    if target == 'comfy':
                        require_idle()
                    if target == 'flint':
                        require_idle()
                    self.check_policy_barrier(transition, policy if policy_transition else None)
                    self.save(dict(transition, phase='dispatching', step='target-action'))
                    try:
                        action_for_target(verb)
                    except Exception:
                        self.save(dict(self.intent, phase='unknown'))
                        raise
                    self.save(dict(transition, phase='actions_complete', step='target-action'))
                    target_state = self.flint_unit() if target == 'flint' else self.unit()
                    if target_state != desired_state:
                        raise RuntimeError('lifecycle_effect_unconfirmed')
                    if target == 'flint':
                        self.wait_flint_ready()
                self.check_policy_barrier(transition, policy if policy_transition else None)
                if target == 'llm':
                    require_idle()
                observation = {'mode':target, 'state':'ready', 'active':self.queue() if target == 'comfy' else 0}
                self.report(observation, transition)
                self.save(dict(transition, phase='completed'))
                if policy_transition:
                    self.save_policy({'demand':policy, 'phase':'applied'})
                return
            if (policy and policy['mode'] == 'llm' and policy['state'] != 'unknown'
                    and row.get('fresh') is True and row.get('state') == 'ready' and row.get('active') == 0
                    and not pending_text and not resident.active and not row['leases'] and not row['pending']
                    and not demand and (unit != 'active' or not self.queue())):
                self.start_policy_load(policy)
            if unit == 'active':
                observation = {'mode':'comfy','state':'ready','active':self.queue()}
            elif self.flint_unit() == 'active':
                runtime = self.flint_runtime()
                observation = {'mode':'flint','state':'ready','active':runtime['active']}
            elif native:
                observation = {'mode':'llm','state':'ready' if resident.ready else 'loading',
                               'active':int(resident.active)}
            else:
                own_hold = any(p.get('operation_id') == resident.record.get('operation_id')
                               and p.get('executor_id') == 'nx5-text' for p in row['leases'])
                observation = {'mode':'llm' if own_hold else 'off','state':'ready','active':0}
            self.report(observation)
            # A ready report can be accepted while its response is lost. Reconcile
            # its known lifecycle effect; never repeat the completed command.
            if (self.intent.get('phase') == 'actions_complete'
                    and self.intent.get('target_mode') == observation['mode']
                    and row.get('mode') == observation['mode']
                    and row.get('fresh') is True and row.get('state') == 'ready'):
                self.save(dict(self.intent, phase='completed'))

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.observed = {'mode':'unknown','state':'unknown','active':1}
                try:
                    self.report(self.observed, self.transition())
                except Exception:
                    pass
                print('managed_comfy:', type(exc).__name__, flush=True)
            self.stop.wait(3)

    def acquire(self, request, revision, save, *, backend_id='nx5-ssd-minicpm', mode='llm'):
        """Enqueue once; refresh only the proven original reservation thereafter."""
        if (backend_id, mode) not in {('nx5-ssd-minicpm', 'llm'), ('nx5-flint', 'flint')}:
            raise RuntimeError('unsupported_resident_profile')
        record = {'operation_id':'op_'+uuid.uuid4().hex, 'request_digest':digest(request),
                  'status':'reserving', 'created_at':time.time(), 'request':request,
                  'backend_id':backend_id, 'mode':mode, 'mapping_version':revision}
        save(record)
        body = {k:record[k] for k in ('operation_id','request_digest')}
        body['candidates'] = [{'backend_id':backend_id, 'mapping_version':revision,
            'resources':[{'resource_id':self.RESOURCE,'units':1}], 'mode':mode,
            'probe_status':'ready', 'probed_at':time.time()}]
        wire = self.wire()
        path = '/internal/v1/reservations/'+record['operation_id']
        try:
            result = wire.api('POST', '/internal/v1/reservations', body)
        except Exception:
            result = wire.api('GET', path)  # a lost response never creates a second intent
        def checked_reservation(value):
            if (not isinstance(value, dict) or value.get('executor_id') != 'nx5-text'
                    or value.get('operation_id') != record['operation_id']
                    or value.get('request_digest') != record['request_digest']):
                raise RuntimeError('reservation_identity_mismatch')
            return value
        deadline = time.monotonic()+2400
        while not self.stop.is_set() and time.monotonic() < deadline:
            checked_reservation(result)
            if result['status'] == 'held':
                lease = wire.api('GET', '/internal/v1/leases/'+record['operation_id'])
                if lease['status'] != 'held' or lease['operation_id'] != record['operation_id']:
                    raise RuntimeError('lease_mismatch')
                record.update(status='held', held_at=time.time(), lease_digest=lease['request_digest'])
                save(record)
                return record
            if result['status'] not in {'waiting','preparing'}:
                raise RuntimeError('lease_not_granted')
            if self.stop.wait(1):
                break
            # GET proves the identity still exists before claim updates its probe.
            checked_reservation(wire.api('GET', path))
            body['candidates'][0]['probed_at'] = time.time()
            result = wire.api('POST', path+'/claim', body)
        raise RuntimeError('lease_reservation_pending')
