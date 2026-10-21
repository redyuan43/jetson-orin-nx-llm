#!/usr/bin/env python3
"""Run one pinned nx5 model under the central GPU lease; never kill another job."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import urllib.request


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def main():
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prompt', type=Path)
    parser.add_argument('--tag', default='qwen36-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    parser.add_argument('--root', type=Path, default=Path.home() / 'github/qwen38-kvmem')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=repo / 'validation')
    parser.add_argument('--tokens', type=int, default=7680)
    parser.add_argument('--window', type=int, choices=range(64, 193))
    parser.add_argument('--ctx', type=int)
    parser.add_argument('--draft', type=int, choices=range(8))
    parser.add_argument('--log-verbosity', type=int)
    parser.add_argument('--no-spec-draft-backend-sampling', action='store_true', default=None)
    parser.add_argument('--sampling', choices=('benchmark', 'qwen-coding', 'qwen-instruct'))
    parser.add_argument('--reasoning', choices=('on', 'off'))
    parser.add_argument('--chat-json', action='store_true', help='Prompt file contains validated chat JSON')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.tag):
        parser.error('tag must be a plain basename')
    args.prompt = args.prompt.resolve(strict=True)
    config = json.loads(args.config.read_text())
    model = args.root / config['model_dir']
    family = config.get('family', 'qwen36')
    assert family in ('qwen36', 'minicpm')
    if family == 'qwen36':
        verified = json.loads((model / 'ssd-verification.json').read_text())
        assert verified['all_tensor_bytes_identical'] and verified['source_sha256'] == config['source_sha256']
        template = model / 'chat-template.jinja'
        template_hash = verified['template_sha256']
    else:
        assert model.resolve() == (args.root / 'models/MiniCPM-V-4.7').resolve()
        template = repo / config['chat_template']
        template_hash = config['chat_template_sha256']
        assert (model / 'experts').is_dir()
        for name, size in config['model_file_sizes'].items():
            assert (model / name).stat().st_size == size, name
    checks = {args.root / config['library_dir'] / k: v for k, v in config['library_sha256'].items()}
    checks[args.root / 'minicpm-les/llama.cpp-omni/build-jetson/bin/llama-cli'] = config['cli_sha256']
    checks[template] = template_hash
    if args.chat_json:
        runtime = json.loads((repo / 'configs/nx5-chat-runtime.json').read_text())
        checks[args.root / runtime['binary']] = runtime['binary_sha256']
        for name, expected in runtime['libraries'].items():
            checks[args.root / name] = expected
    for path, expected in checks.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, str(path)
    parameters = config['parameters'].copy()
    for key in ('ctx', 'window', 'sampling', 'reasoning', 'draft', 'log_verbosity', 'no_spec_draft_backend_sampling'):
        if getattr(args, key) is not None:
            parameters[key] = getattr(args, key)
    if args.reasoning is not None:
        kwargs = json.loads(parameters['chat_template_kwargs'])
        kwargs['enable_thinking'] = args.reasoning == 'on'
        parameters['chat_template_kwargs'] = json.dumps(kwargs, separators=(',', ':'))
        parameters['reasoning_budget'] = -1 if args.reasoning == 'on' else 0
    if not 0 < args.tokens < parameters['ctx']:
        parser.error('output budget must be positive and below the context capacity')
    args.output.mkdir(parents=True, exist_ok=True)
    proof_path = args.output / (args.tag + '.lease.json')
    if proof_path.exists():
        parser.error('Existing receipt: reconcile the original operation; do not replay')
    units = ['siyuan-fleet-control.service', 'siyuan-image-comfy.service',
             'nx5-ornith-35b-a3b-iq2s.service']

    def inactive():
        states = {}
        for unit in units:
            state = subprocess.check_output(['systemctl', '--user', 'show', unit,
                                            '--property=ActiveState,MainPID'], text=True)
            assert 'ActiveState=inactive' in state and 'MainPID=0' in state, (unit, state)
            states[unit] = state
        for p in Path('/proc').iterdir():
            if not p.name.isdigit():
                continue
            try:
                exe = (p / 'exe').resolve(strict=True).name
                cmd = (p / 'cmdline').read_bytes().replace(b'\0', b' ').lower()
                assert not exe.startswith('llama-'), ('Inference already running', p.name)
                assert not (exe.startswith('python') and b'comfy' in cmd and b'main.py' in cmd)
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
        return states

    credentials = Path.home() / '.config/siyuan-text-ingress'
    wrapper = json.loads((credentials / 'wrapper.json').read_text())
    ingress = json.loads((credentials / 'ingress.json').read_text())
    secrets = {}
    for line in (credentials / 'credentials.env').read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, value = line.split('=', 1)
        secrets[key.removeprefix('export ').strip()] = shlex.split(value)[0]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    group = '/internal/v1/resource-groups/nx5-igpu-group'

    def api(method, path, body=None, controller=False):
        central = (wrapper if controller else ingress)['central']
        req = urllib.request.Request(central['base_url'] + path,
            data=None if body is None else json.dumps(body).encode(), method=method,
            headers={'Authorization': 'Bearer ' + secrets[central['auth']['token_env']],
                     'Content-Type': 'application/json'})
        with opener.open(req, timeout=20) as response:
            return json.load(response)

    def observe(mode, state, active, operation=None):
        return api('POST', group + '/observation', {
            'mode': mode, 'state': state, 'active': active, 'operation_id': operation}, True)

    activity = api('GET', group + '/activity', controller=True)
    assert not activity['leases'] and not activity['pending'] and not activity['transition'], activity
    request = {'model': config['model'], 'source_sha256': config.get('source_sha256'),
               'prompt_sha256': hashlib.sha256(args.prompt.read_bytes()).hexdigest(),
               'tag': args.tag, 'invocation_scope': str(args.output.resolve()),
               'tokens': args.tokens, 'parameters': parameters, 'chat_json': args.chat_json}
    op = 'op_' + digest(request)[:32]
    proof = {'operation_id': op, 'request': request, 'created_at': time.time(),
             'initial_activity': activity, 'initial_units': inactive(), 'status': 'waiting',
             'controller': 'manual CLI experiment; original controller and services remain inactive'}

    def save():
        temp = proof_path.with_suffix('.tmp')
        temp.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
        temp.chmod(0o600)
        temp.replace(proof_path)

    save()
    body = {'operation_id': op, 'request_digest': digest(request), 'candidates': [{
        'backend_id': 'nx5-ssd-' + family, 'mapping_version': config['revision'],
        'resources': [{'resource_id': 'nx5-igpu', 'units': 1}],
        'mode': 'llm', 'probe_status': 'ready', 'probed_at': time.time()}]}
    reservation = api('POST', '/internal/v1/reservations', body)
    assert reservation['status'] in ('waiting', 'preparing'), reservation
    observe('off', 'ready', 0)
    lease = None
    for _ in range(30):
        body['candidates'][0]['probed_at'] = time.time()
        reservation = api('POST', '/internal/v1/reservations', body)
        if reservation['status'] == 'preparing':
            transition = api('GET', group + '/activity', controller=True)['transition']
            assert transition['operation_id'] == op and transition['target_mode'] == 'llm', transition
            inactive()
            observe('llm', 'ready', 0, op)
        elif reservation['status'] == 'held':
            lease = api('GET', '/internal/v1/leases/' + op)
            assert lease['status'] == 'held'
            break
        elif reservation['status'] != 'waiting':
            raise RuntimeError('Unexpected reservation status: ' + reservation['status'])
        time.sleep(1)
    if lease is None:
        raise SystemExit('Not admitted; reconcile original operation without starting inference')
    command = [sys.executable, str(repo / 'scripts/minicpm/bench-audit.py'), args.tag,
               '--root', str(args.root), '--prompt', str(args.prompt),
               '--output', str(args.output), '--tokens', str(args.tokens),
               '--library-dir', str(args.root / config['library_dir']),
               '--chat-template-file', str(template)]
    if args.chat_json:
        command += ['--chat-runner', str(args.root / runtime['binary'])]
    if family == 'qwen36':
        command += ['--model-dir', str(model)]
    for key, value in parameters.items():
        option = '--' + key.replace('_', '-')
        if isinstance(value, bool):
            if value:
                command.append(option)
        else:
            command.extend([option, str(value)])
    inactive()
    observe('llm', 'ready', 1)
    proof.update(status='running', started_at=time.time(), command=command,
                 lease_digest=lease['request_digest'])
    save()
    print(json.dumps({'operation': op, 'tag': args.tag, 'status': 'running'}), flush=True)
    with (args.output / (args.tag + '.driver.log')).open('w') as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        while child.poll() is None:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    observe('llm', 'ready', 1)
                except Exception as error:
                    proof.setdefault('heartbeat_errors', []).append(type(error).__name__)
                    save()
    summary_path = args.output / (args.tag + '.json')
    if not summary_path.exists():
        proof.update(status='unknown', error='Missing benchmark receipt; keep original lease')
        save()
        raise SystemExit(1)
    summary = json.loads(summary_path.read_text())
    proof['final_units'] = inactive()
    observe('off', 'unknown', 0)
    terminal = 'succeeded' if child.returncode == 0 and summary['stop_reason'] is None else 'failed'
    api('POST', '/internal/v1/leases/' + op + '/release', {
        'terminal_status': terminal, 'request_digest': lease['request_digest'],
        'evidence_digest': digest(summary)})
    proof.update(status=terminal, finished_at=time.time(), wall_s=summary['wall_s'],
                 lease_after=api('GET', '/internal/v1/leases/' + op)['status'])
    save()
    print(json.dumps({'status': terminal, 'wall_s': summary['wall_s'], 'lease': proof['lease_after']}), flush=True)
    return child.returncode


if __name__ == '__main__':
    raise SystemExit(main())
