"""Read actual native weight metadata without loading models or changing admission."""
import math
import re
from urllib.parse import urlsplit


def validate(spec):
    if not isinstance(spec, dict) or spec.get('kind') not in {'comfy_loaded_models', 'llama_models'}:
        raise ValueError('residency_probe_kind_invalid')
    url = urlsplit(spec.get('url', ''))
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password
            or url.query or url.fragment or not url.path.startswith('/') or url.port is None):
        raise ValueError('local_residency_probe_required')
    for key in ('profile_id', 'profile_version'):
        if not isinstance(spec.get(key), str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', spec[key]):
            raise ValueError('residency_profile_identity_required')
    key = 'model_class' if spec['kind'] == 'comfy_loaded_models' else 'native_model'
    if not isinstance(spec.get(key), str) or not 1 <= len(spec[key]) <= 160:
        raise ValueError('residency_model_identity_required')


def observe(spec, fetch, *, now):
    validate(spec)
    result = {key: spec[key] for key in ('profile_id', 'profile_version')}
    result['model_loaded'] = None
    try:
        value = fetch('GET', spec['url'], auth=spec.get('auth'))
        if not isinstance(value, dict):
            raise ValueError('residency_payload_invalid')
        if spec['kind'] == 'llama_models':
            rows = value.get('data')
            # The controller independently verified this exact single native unit.
            if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                    or rows[0].get('id') != spec['native_model']):
                raise ValueError('native_model_identity_unknown')
            result['model_loaded'] = True
            return result
        observed_now = now() if callable(now) else now
        at, rows = value.get('observed_at'), value.get('models')
        if (value.get('schema_version') != 1 or type(value.get('schema_version')) is not int
                or value.get('source') != 'comfy.current_loaded_models'
                or type(at) not in (int, float) or not math.isfinite(at) or not 0 <= observed_now-at <= 60
                or type(value.get('busy')) is not bool or type(value.get('model_loaded')) is not bool
                or not isinstance(rows, list) or len(rows) > 128
                or type(value.get('count')) is not int or value['count'] != len(rows)):
            raise ValueError('comfy_residency_unknown')
        total, primary = 0, 0
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('model_class'), str)
                    or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,95}', row['model_class'])
                    or row.get('device_kind') not in {'cpu', 'cuda'}
                    or type(row.get('loaded_bytes')) is not int or not 0 <= row['loaded_bytes'] <= 2**60):
                raise ValueError('comfy_residency_unknown')
            total += row['loaded_bytes']
            if row['model_class'] == spec['model_class'] and row['device_kind'] == 'cuda':
                primary += row['loaded_bytes']
        if (type(value.get('loaded_bytes')) is not int or value['loaded_bytes'] != total
                or value['model_loaded'] != (total > 0)):
            raise ValueError('comfy_residency_unknown')
        # A prompt may still be loading its first model. Absence is conclusive only while idle.
        result['model_loaded'] = True if primary > 0 else None if value['busy'] else False
    except Exception:
        # Optional residency evidence cannot erase an independently proven service/queue state.
        # Never log arbitrary native responses, URLs or credentials.
        pass
    return result
