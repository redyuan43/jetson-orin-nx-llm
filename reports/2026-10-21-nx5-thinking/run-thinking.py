"""One-shot nx5 comparison; preserve inactive services, hold the central GPU lease."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
import urllib.request

folder = Path(__file__).resolve().parent
root = Path('/home/nx/github/qwen38-kvmem')
audit = root / 'minicpm-audit-20261021'
proof_path = folder / 'lease-proof.json'
if proof_path.exists():
    raise SystemExit('Existing operation receipt: inspect it; do not replay inference')
configuration = Path.home() / '.config/siyuan-text-ingress'
wrapper = json.loads((configuration / 'wrapper.json').read_text())
ingress = json.loads((configuration / 'ingress.json').read_text())
secrets = {}
for line in (configuration / 'credentials.env').read_text().splitlines():
    if not line.strip() or line.lstrip().startswith('#'):
        continue
    key, value = line.split('=', 1)
    secrets[key.removeprefix('export ').strip()] = shlex.split(value)[0]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
group_path = '/internal/v1/resource-groups/nx5-igpu-group'
units = ['siyuan-fleet-control.service', 'siyuan-image-comfy.service',
         'nx5-ornith-35b-a3b-iq2s.service']


def api(method, path, body=None, controller=False):
    central = (wrapper if controller else ingress)['central']
    request = urllib.request.Request(
        central['base_url'] + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + secrets[central['auth']['token_env']],
                 'Content-Type': 'application/json'}, method=method)
    with opener.open(request, timeout=20) as response:
        return json.load(response)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def save():
    temp = proof_path.with_suffix('.tmp')
    temp.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(proof_path)


def inactive_units():
    states = {}
    for unit in units:
        state = subprocess.check_output(
            ['systemctl', '--user', 'show', unit, '--property=ActiveState,MainPID'], text=True)
        assert 'ActiveState=inactive' in state and 'MainPID=0' in state, (unit, state)
        states[unit] = state
    return states


def no_inference():
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            exe = (process / 'exe').resolve(strict=True).name
            cmd = (process / 'cmdline').read_bytes().replace(b'\0', b' ').lower()
            assert not exe.startswith('llama-'), ('Inference still present', process.name)
            assert not (exe.startswith('python') and b'comfy' in cmd and b'main.py' in cmd), (
                'Comfy still present', process.name)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass


def observation(mode, state, active, operation_id=None):
    return api('POST', group_path + '/observation', {
        'mode': mode, 'state': state, 'active': active, 'operation_id': operation_id}, True)


baseline = json.loads((folder / 'nano-baseline.json').read_text())
settings = baseline['args'].copy()
settings.update(root=str(root), prompt=str(folder / 'prompt.txt'), output=str(folder),
                library_dir=str(audit / 'optimization/cuda-mapped-v2'),
                chat_template_file=str(audit / 'configs/minicpm-v47-instruct.jinja'))
settings.pop('tag')
expected_hashes = {
    root / 'minicpm-les/llama.cpp-omni/build-jetson/bin/llama-cli':
        '49e307ca0635f67cff3714357565147ab50db02d041c22357991c52c2b468ce5',
    Path(settings['library_dir']) / 'libllama.so.0':
        '5ede85f4c76d1299c9dc0e6089b72a6a8bee7e70a60cc5ad561ed112b58b3e6c',
    Path(settings['library_dir']) / 'libggml-cuda.so.0':
        'ea6394c23526ad021d9f449063a1bdc820c7f697d2495169430d7412ffe0dac6',
    Path(settings['chat_template_file']):
        '49c37bcc86c7f7f20ed2ebf79e9f872be41ba2b5f9d0d51e6712cc038ef51d6c',
}
for path, expected in expected_hashes.items():
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, str(path)
assert Path(settings['prompt']).read_text().strip() == (
    '创建一个HTML，内容是SVG绘制一个鹈鹕骑自行车的2D动画，不许参考任何本机其他项目直接自己搞')
request = {'test': 'nx5-pelican-thinking-20261021-01',
           'prompt_sha256': hashlib.sha256(Path(settings['prompt']).read_bytes()).hexdigest(),
           'parameters': settings}
op = 'op_' + hashlib.sha256(request['test'].encode()).hexdigest()[:32]
activity = api('GET', group_path + '/activity', controller=True)
assert not activity['leases'] and not activity['pending'] and not activity['transition'], activity
no_inference()
proof = {'operation_id': op, 'request': request, 'created_at': time.time(),
         'initial_activity': activity, 'initial_units': inactive_units(),
         'hashes_verified': {str(p): v for p, v in expected_hashes.items()},
         'status': 'waiting', 'controller': 'temporary manual CLI experiment; original service stays inactive'}
save()
body = {'operation_id': op, 'request_digest': digest(request), 'candidates': [{
    'backend_id': 'nx5-minicpm-thinking-comparison',
    'mapping_version': 'nx5-thinking-20261021-01',
    'resources': [{'resource_id': 'nx5-igpu', 'units': 1}],
    'mode': 'llm', 'probe_status': 'ready', 'probed_at': time.time()}]}
reservation = api('POST', '/internal/v1/reservations', body)
proof['first_reservation'] = reservation
save()
assert reservation['status'] in ('waiting', 'preparing'), reservation
# All registered services are stopped. Temporarily act as the manual lifecycle
# controller for this isolated CLI job; do not publish an Ornith HTTP endpoint.
observation('off', 'ready', 0)
deadline = time.monotonic() + 30
lease = None
while time.monotonic() < deadline:
    body['candidates'][0]['probed_at'] = time.time()
    reservation = api('POST', '/internal/v1/reservations', body)
    if reservation['status'] == 'preparing':
        activity = api('GET', group_path + '/activity', controller=True)
        transition = activity['transition']
        assert transition['operation_id'] == op, transition
        assert transition['target_mode'] == 'llm', transition
        inactive_units()
        no_inference()
        proof['transition'] = transition
        save()
        observation('llm', 'ready', 0, op)
    elif reservation['status'] == 'held':
        lease = api('GET', '/internal/v1/leases/' + op)
        assert lease['status'] == 'held' and lease['operation_id'] == op
        break
    elif reservation['status'] != 'waiting':
        raise RuntimeError('Unexpected reservation status: ' + reservation['status'])
    time.sleep(1)
if lease is None:
    raise SystemExit('Reservation not held; reconcile the original operation, no inference started')
proof.update(status='held', lease_digest=lease['request_digest'])
save()
args = [sys.executable, str(folder / 'bench-audit.py'), 'nx5-pelican-thinking']
for key, value in settings.items():
    option = '--' + key.replace('_', '-')
    if isinstance(value, bool):
        if value:
            args.append(option)
    elif value is not None:
        args.extend([option, str(value)])
inactive_units()
no_inference()
observation('llm', 'ready', 1)
proof.update(status='running', started_at=time.time(), command=args)
save()
print(json.dumps({'operation': op, 'status': 'running', 'parameters_identical': True}), flush=True)
with (folder / 'driver.log').open('w') as log:
    child = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
    while child.poll() is None:
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                observation('llm', 'ready', 1)
            except Exception as error:
                # A held lease has no expiry. Keep running our already admitted job,
                # keep its lease, and retain a diagnostic without exposing credentials.
                proof.setdefault('heartbeat_errors', []).append(type(error).__name__)
                save()
summary_path = folder / 'nx5-pelican-thinking.json'
if not summary_path.exists():
    proof.update(status='unknown', error='No inference receipt; keep original lease')
    save()
    raise SystemExit(1)
summary = json.loads(summary_path.read_text())
no_inference()
proof['final_units'] = inactive_units()
# Leave automatic admission disabled as it was before this manual test.
observation('off', 'unknown', 0)
terminal = 'succeeded' if child.returncode == 0 and summary['stop_reason'] is None else 'failed'
api('POST', '/internal/v1/leases/' + op + '/release', {
    'terminal_status': terminal, 'evidence_digest': digest(summary),
    'request_digest': lease['request_digest']})
proof.update(status=terminal, finished_at=time.time(), wall_s=summary['wall_s'],
             lease_after=api('GET', '/internal/v1/leases/' + op)['status'],
             final_activity=api('GET', group_path + '/activity', controller=True))
save()
print(json.dumps({'status': terminal, 'wall_s': summary['wall_s'],
                  'lease': proof['lease_after'], 'original_services': 'inactive'}), flush=True)
