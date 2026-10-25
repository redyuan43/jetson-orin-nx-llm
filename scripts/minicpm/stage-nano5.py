#!/usr/bin/env python3
"""Prepare an immutable Nano5 release from a complete Git commit; never activate.

The legacy owner has no close-admission endpoint. Activation requires the scoped
NEW-connection fence plus a verified root channel; staging does not infer either.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

FILES=['scripts/minicpm/nano5-service.py','scripts/minicpm/run-nano5.py','scripts/minicpm/bench-audit.py',
    'scripts/nx5/temporary_admission_fence.py','configs/minicpm-v47-nano5.json',
    'configs/minicpm-v47-instruct.jinja','docs/nano5-service/API.md',
    'docs/nano5-service/catalog.json','docs/nano5-service/openapi.json',
    'scripts/minicpm/vendor/mode_residency.py','scripts/minicpm/vendor/comfy_residency/__init__.py',
    'scripts/minicpm/vendor/source-lock.json']


def sha(raw):return hashlib.sha256(raw).hexdigest()


def stage(base,config_raw,payload,*,apply=False):
    if sha(config_raw)!=payload['expected_config_sha256']:raise RuntimeError('config_CAS_changed')
    revision=payload['source_commit']
    if not re.fullmatch('[a-f0-9]{40}',revision):raise RuntimeError('full_git_commit_required')
    if payload['manifest']['source_commit']!=revision:raise RuntimeError('manifest_identity_changed')
    contents={}
    if set(payload['files'])!=set(payload['manifest']['files']):raise RuntimeError('candidate_file_set_changed')
    for name,encoded in payload['files'].items():
        path=Path(name)
        if path.is_absolute() or '..' in path.parts or not path.parts:raise RuntimeError('unsafe_candidate_path')
        raw=base64.b64decode(encoded,validate=True)
        if sha(raw)!=payload['manifest']['files'][name]:raise RuntimeError('candidate_hash_changed')
        if path.suffix=='.py':compile(raw,name,'exec')
        contents[name]=raw
    target=Path(base)/revision
    if not apply:return {'stage':'preflight','source_commit':revision,'modified':False}
    if not target.exists():
        target.mkdir(parents=True,mode=0o700,exist_ok=False)
        for name,raw in contents.items():
            path=target/name;path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('xb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
        with (target/'release.json').open('x') as stream:
            json.dump(payload['manifest'],stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    if not (target/'release.json').is_file():raise RuntimeError('partial_stage_requires_review')
    if json.loads((target/'release.json').read_text())!=payload['manifest']:raise RuntimeError('staged_manifest_changed')
    for name,expected in payload['manifest']['files'].items():
        path=target/name
        if path.is_symlink() or not path.is_file() or sha(path.read_bytes())!=expected:
            raise RuntimeError('staged_source_changed')
    return {'stage':'verified','source_commit':revision,'directory':str(target),
        'activated':False,'activation_blocker':'verified_root_admission_fence_required'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit',required=True)
    parser.add_argument('--expected-config-sha256',required=True)
    parser.add_argument('--stage',action='store_true')
    args=parser.parse_args()
    if not re.fullmatch('[a-f0-9]{40}',args.commit):raise RuntimeError('full_git_commit_required')
    root=Path(__file__).resolve().parents[2]
    read=lambda name:subprocess.check_output(['git','show',args.commit+':'+name],cwd=root)
    raw={name:read(name) for name in FILES}
    manifest={'source_commit':args.commit,'files':{name:sha(value) for name,value in raw.items()}}
    payload={'source_commit':args.commit,'manifest':manifest,'files':{n:base64.b64encode(v).decode() for n,v in raw.items()},
        'expected_config_sha256':args.expected_config_sha256,'stage':args.stage}
    # The remote implementation is also read from the exact reviewed Git object.
    source=read('scripts/minicpm/stage-nano5.py').decode()
    remote="scope={'__name__':'frozen_nano5_stage'}\nexec(compile("+repr(source)+",'committed_nano5_stage','exec'),scope)\n"
    remote+='import json\nfrom pathlib import Path\np='+repr(payload)+'\n'
    remote+="try:\n print(json.dumps(scope['stage'](Path.home()/'minicpm-v47/releases',(Path.home()/'.config/nano5-minicpm/config.json').read_bytes(),p,apply=p['stage'])))\nexcept Exception as error:\n print(json.dumps({'error':str(error) if isinstance(error,RuntimeError) else type(error).__name__}));raise SystemExit(1)\n"
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','nano5','python3','-'],input=remote,text=True,capture_output=True,timeout=60)
    if result.returncode:raise RuntimeError('nano5_stage_failed')
    print(result.stdout.strip())


if __name__=='__main__':main()
