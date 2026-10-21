#!/usr/bin/env python3
"""Upsert the nx5 provider without changing other providers or the global default.

Uses a private Octop auth.json and reads the nx5 service key over SSH.
No credentials are printed or written to the report.
"""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://bus002.taild500c8.ts.net:8088')
    parser.add_argument('--auth', type=Path, default=Path.home() / '.config/octop-bus002/auth.json')
    parser.add_argument('--profile', type=Path, default=Path(__file__).resolve().parents[2] / 'deploy/octop/nx5-provider.json')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    token = json.loads(args.auth.read_text())['access_token']

    def call(path, body=None, method=None):
        req = urllib.request.Request(args.base_url.rstrip('/') + path,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
            data=None if body is None else json.dumps(body).encode(), method=method)
        with urllib.request.urlopen(req, timeout=90) as response:
            return json.load(response)

    profile = json.loads(args.profile.read_text())
    assert 'api_key' not in profile, 'Do not put credentials in the profile'
    profile['api_key'] = subprocess.check_output([
        'ssh', '-o', 'Hostname=192.168.100.154', '-o', 'BatchMode=yes', 'nx5',
        'cat .config/nx5-ssd-llm/client.key'], text=True).strip()
    existing = call('/api/admin/providers')
    active = call('/api/providers/active-model')
    matches = [p for p in existing if p['name'] == profile['name']]
    assert len(matches) <= 1, 'Duplicate provider names'
    fetched = call('/api/admin/providers/fetch-models',
                   {k: profile[k] for k in ('name', 'kind', 'base_url', 'api_key')})
    assert fetched.get('ok') is True, 'Model discovery failed'
    if matches:
        result = call('/api/admin/providers/' + str(matches[0]['id']),
                      {k: v for k, v in profile.items() if k != 'name'}, 'PATCH')
    else:
        result = call('/api/admin/providers', profile)
    assert result['api_key'] == profile['api_key'] and result['enabled']
    resolved = [p for p in call('/api/providers/resolved') if p['provider_name'] == profile['name']]
    assert {p['model'] for p in resolved} == {m['id'] for m in profile['models'] if m['enabled']}
    after = call('/api/admin/providers')
    assert call('/api/providers/active-model') == active
    for provider in existing:
        if provider['name'] != profile['name']:
            assert next(p for p in after if p['id'] == provider['id']) == provider
    proof = {
        'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'provider_id': result['id'],
        'provider': {k: v for k, v in result.items() if k != 'api_key'},
        'resolved_models': resolved, 'discovery_ok': True,
        'existing_providers_unchanged': True, 'active_model_unchanged': True,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'provider_id': result['id'], 'models': [p['model'] for p in resolved],
                      'discovery_ok': True, 'existing_providers_unchanged': True,
                      'active_model_unchanged': True}))


if __name__ == '__main__':
    main()
