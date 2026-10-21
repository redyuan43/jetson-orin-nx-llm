"""OpenAI text chat wire format. No model selection, execution or leases here."""
import math
import re
import uuid

MODELS = ('qwen36-128k', 'minicpm-64k')


class InvalidRequest(ValueError):
    def __init__(self, code, param=None):
        super().__init__(code)
        self.param = param


def error(code, param=None, kind='invalid_request_error'):
    return {'error': {'message': code, 'type': kind, 'param': param, 'code': code}}


def normalize(body, key=None):
    fields = {'model', 'messages', 'stream', 'stream_options', 'max_tokens', 'max_completion_tokens',
              'temperature', 'top_p', 'seed', 'stop', 'presence_penalty', 'frequency_penalty',
              'n', 'tools', 'tool_choice', 'response_format', 'user'}
    if not isinstance(body, dict):
        raise InvalidRequest('invalid_request')
    unknown = set(body) - fields
    if unknown:
        raise InvalidRequest('unsupported_parameter', sorted(unknown)[0])
    if body.get('model') not in MODELS:
        raise InvalidRequest('model_not_found', 'model')
    if type(body.get('stream', False)) is not bool:
        raise InvalidRequest('invalid_stream', 'stream')
    stream = body.get('stream', False)
    options = body.get('stream_options')
    if options is None:
        options = {}
    if not isinstance(options, dict) or set(options) - {'include_usage'} or type(options.get('include_usage', False)) is not bool:
        raise InvalidRequest('invalid_stream_options', 'stream_options')
    if options and not stream:
        raise InvalidRequest('stream_options_requires_stream', 'stream_options')
    for field, neutral in (('n', 1), ('tools', []), ('tool_choice', 'none'), ('response_format', {'type': 'text'})):
        if field in body and body[field] is not None and body[field] != neutral:
            raise InvalidRequest('unsupported_parameter', field)
    if 'n' in body and type(body['n']) is not int:
        raise InvalidRequest('invalid_n', 'n')
    if 'user' in body and (not isinstance(body['user'], str) or len(body['user']) > 200):
        raise InvalidRequest('invalid_user', 'user')
    messages = body.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 1024:
        raise InvalidRequest('invalid_messages', 'messages')
    normalized = []
    for i, message in enumerate(messages):
        if not isinstance(message, dict) or set(message) - {'role', 'content'}:
            raise InvalidRequest('unsupported_message', 'messages')
        role, content = message.get('role'), message.get('content')
        if role not in ('system', 'user', 'assistant') or (role == 'system' and i != 0):
            raise InvalidRequest('unsupported_role', 'messages')
        if isinstance(content, list):
            if not all(isinstance(x, dict) and set(x) == {'type', 'text'} and x['type'] == 'text'
                       and isinstance(x['text'], str) for x in content):
                raise InvalidRequest('text_only', 'messages')
            content = ''.join(x['text'] for x in content)
        if not isinstance(content, str) or '\0' in content:
            raise InvalidRequest('invalid_content', 'messages')
        # Reject invalid Unicode before persisting or handing to the native engine.
        try:
            content.encode('utf-8')
        except UnicodeError:
            raise InvalidRequest('invalid_unicode', 'messages')
        normalized.append({'role': role, 'content': content})
    if normalized[-1]['role'] != 'user':
        raise InvalidRequest('last_message_must_be_user', 'messages')
    if not 1 <= sum(len(m['content']) for m in normalized) <= 500000:
        raise InvalidRequest('invalid_message_size', 'messages')
    a, b = body.get('max_tokens'), body.get('max_completion_tokens')
    if a is not None and b is not None and a != b:
        raise InvalidRequest('conflicting_output_limits', 'max_tokens')
    limit = b if b is not None else a if a is not None else 2048
    if type(limit) is not int or not 1 <= limit <= 7680:
        raise InvalidRequest('invalid_output_limit', 'max_tokens')
    request = {'messages': normalized}
    for name, lower, upper in [('temperature', 0, 2), ('top_p', 0, 1),
                                ('presence_penalty', -2, 2), ('frequency_penalty', -2, 2)]:
        if name in body and body[name] is not None:
            value = body[name]
            if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
                raise InvalidRequest('invalid_sampling_parameter', name)
            request[name] = value
    if body.get('seed') is not None:
        if type(body['seed']) is not int or not 0 <= body['seed'] <= 4294967295:
            raise InvalidRequest('invalid_seed', 'seed')
        request['seed'] = body['seed']
    stop = body.get('stop')
    if isinstance(stop, str):
        stop = [stop]
    if stop is not None:
        if not isinstance(stop, list) or len(stop) > 4 or not all(isinstance(s, str) and 1 <= len(s) <= 256 and '\0' not in s for s in stop):
            raise InvalidRequest('invalid_stop', 'stop')
        try:
            ''.join(stop).encode('utf-8')
        except UnicodeError:
            raise InvalidRequest('invalid_unicode', 'stop')
        request['stop'] = stop
    if key is not None and (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', key)):
        raise InvalidRequest('invalid_idempotency_key', 'Idempotency-Key')
    payload = {'request_id': key or 'chat-' + uuid.uuid4().hex, 'profile': body['model'],
               'kind': 'chat', 'max_tokens': limit, 'chat': request}
    return payload, stream, options.get('include_usage', False)


def completion(job, delta=None, finish=None, usage_only=False):
    result = job.get('result', {})
    streaming = delta is not None or finish is not None or usage_only
    value = {'id': 'chatcmpl-' + job['id'][3:],
             'object': 'chat.completion.chunk' if streaming else 'chat.completion',
             'created': int(job['created_at']), 'model': job['profile']}
    if usage_only:
        value['choices'] = []
    elif streaming:
        value['choices'] = [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]
    else:
        value['choices'] = [{'index': 0, 'message': {'role': 'assistant', 'content': result['text']},
                             'finish_reason': result['finish_reason'], 'logprobs': None}]
    if usage_only or not streaming:
        value['usage'] = {'prompt_tokens': result['input_tokens'], 'completion_tokens': result['output_tokens'],
                          'total_tokens': result['input_tokens'] + result['output_tokens']}
        # Keep standard usage intact. OpenWebUI consumes this llama.cpp-style
        # extension separately; emit it only once per response, not per delta.
        timing = {key: result[key] for key in ('decode_tps', 'generation_s', 'prefill_s', 'process_wall_s')
                  if key in result}
        if job.get('finished_at') is not None:
            timing['total_s'] = max(0, job['finished_at'] - job['created_at'])
        if timing:
            value['timings'] = timing
    return value
