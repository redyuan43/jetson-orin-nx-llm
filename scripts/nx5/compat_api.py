"""OpenAI text chat wire format. No model selection, execution or leases here."""
import math
import json
import re
import uuid

MODELS = ('minicpm-64k',)


class InvalidRequest(ValueError):
    def __init__(self, code, param=None):
        super().__init__(code)
        self.param = param


def error(code, param=None, kind='invalid_request_error'):
    return {'error': {'message': code, 'type': kind, 'param': param, 'code': code}}


def valid_name(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) is not None


def normalize_tools(body):
    tools = body.get('tools', [])
    if tools is None:
        tools = []
    if not isinstance(tools, list) or len(tools) > 128:
        raise InvalidRequest('invalid_tools', 'tools')
    names = set()
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {'type', 'function'} or tool['type'] != 'function':
            raise InvalidRequest('invalid_tool', 'tools')
        fn = tool['function']
        if (not isinstance(fn, dict) or set(fn) - {'name', 'description', 'parameters', 'strict'}
                or not valid_name(fn.get('name')) or fn['name'] in names
                or not isinstance(fn.get('description', ''), str)
                or not isinstance(fn.get('parameters', {}), dict)
                or ('strict' in fn and type(fn['strict']) is not bool)):
            raise InvalidRequest('invalid_tool_function', 'tools')
        names.add(fn['name'])
    choice = body.get('tool_choice')
    if choice is None:
        choice = 'auto' if tools else 'none'
    if isinstance(choice, dict):
        fn = choice.get('function')
        if (set(choice) != {'type', 'function'} or choice['type'] != 'function'
                or not isinstance(fn, dict) or set(fn) != {'name'}
                or not valid_name(fn['name']) or fn['name'] not in names):
            raise InvalidRequest('invalid_tool_choice', 'tool_choice')
    elif not isinstance(choice, str) or choice not in ('auto', 'none', 'required'):
        raise InvalidRequest('invalid_tool_choice', 'tool_choice')
    if not tools and choice != 'none':
        raise InvalidRequest('tool_choice_requires_tools', 'tool_choice')
    if type(body.get('parallel_tool_calls', True)) is not bool:
        raise InvalidRequest('invalid_parallel_tool_calls', 'parallel_tool_calls')
    return tools, choice


def collect_tool_calls(events):
    """Rebuild indexed OpenAI deltas, also used to verify the native final result."""
    calls = []
    for event in events:
        if event['type'] != 'tool_call_delta':
            continue
        delta = event['tool_call']
        index = delta['index']
        if type(index) is not int or not 0 <= index <= len(calls):
            raise ValueError('invalid_tool_call_index')
        if index == len(calls):
            calls.append({'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
        call = calls[index]
        call['id'] += delta.get('id', '')
        for key in ('name', 'arguments'):
            call['function'][key] += delta.get('function', {}).get(key, '')
    return calls


def normalize(body, key=None):
    fields = {'model', 'messages', 'stream', 'stream_options', 'max_tokens', 'max_completion_tokens',
              'temperature', 'top_p', 'seed', 'stop', 'presence_penalty', 'frequency_penalty',
              'n', 'tools', 'tool_choice', 'parallel_tool_calls', 'response_format', 'user', 'enable_thinking'}
    if not isinstance(body, dict):
        raise InvalidRequest('invalid_request')
    unknown = set(body) - fields
    if unknown:
        raise InvalidRequest('unsupported_parameter', sorted(unknown)[0])
    if body.get('model') not in MODELS:
        raise InvalidRequest('model_not_found', 'model')
    if type(body.get('enable_thinking', False)) is not bool:
        raise InvalidRequest('invalid_enable_thinking', 'enable_thinking')
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
    tools, tool_choice = normalize_tools(body)
    for field, neutral in (('n', 1), ('response_format', {'type': 'text'})):
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
    pending, seen_calls = set(), set()
    for i, message in enumerate(messages):
        if not isinstance(message, dict) or set(message) - {'role', 'content', 'reasoning_content', 'tool_calls', 'tool_call_id', 'name'}:
            raise InvalidRequest('unsupported_message', 'messages')
        role, content = message.get('role'), message.get('content')
        if role not in ('system', 'user', 'assistant', 'tool') or (role == 'system' and i != 0):
            raise InvalidRequest('unsupported_role', 'messages')
        if pending and role != 'tool':
            raise InvalidRequest('missing_tool_results', 'messages')
        calls = message.get('tool_calls')
        if calls is not None:
            if role != 'assistant' or not isinstance(calls, list) or not 1 <= len(calls) <= 128:
                raise InvalidRequest('invalid_tool_calls', 'messages')
            for call in calls:
                if (not isinstance(call, dict) or set(call) != {'id', 'type', 'function'}
                        or call['type'] != 'function' or not valid_name(call['id']) or call['id'] in seen_calls):
                    raise InvalidRequest('invalid_tool_call', 'messages')
                fn = call['function']
                if (not isinstance(fn, dict) or set(fn) != {'name', 'arguments'}
                        or not valid_name(fn['name']) or not isinstance(fn['arguments'], str)):
                    raise InvalidRequest('invalid_tool_call_function', 'messages')
                seen_calls.add(call['id'])
                pending.add(call['id'])
            if content is None:
                content = ''
        if role == 'tool':
            call_id = message.get('tool_call_id')
            if not valid_name(call_id) or call_id not in pending:
                raise InvalidRequest('orphan_tool_result', 'messages')
            pending.remove(call_id)
        elif 'tool_call_id' in message:
            raise InvalidRequest('unexpected_tool_call_id', 'messages')
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
        if calls:
            normalized[-1]['tool_calls'] = calls
        if role == 'tool':
            normalized[-1]['tool_call_id'] = message['tool_call_id']
        if 'name' in message:
            if not valid_name(message['name']):
                raise InvalidRequest('invalid_message_name', 'messages')
            normalized[-1]['name'] = message['name']
        if 'reasoning_content' in message:
            reasoning = message['reasoning_content']
            if role != 'assistant' or (reasoning is not None and (not isinstance(reasoning, str) or '\0' in reasoning)):
                raise InvalidRequest('invalid_reasoning_content', 'messages')
            if reasoning:
                try:
                    reasoning.encode('utf-8')
                except UnicodeError:
                    raise InvalidRequest('invalid_unicode', 'messages')
                normalized[-1]['reasoning_content'] = reasoning
    if pending:
        raise InvalidRequest('missing_tool_results', 'messages')
    if normalized[-1]['role'] not in ('user', 'tool'):
        raise InvalidRequest('last_message_must_be_user', 'messages')
    if not 1 <= sum(len(m['content']) + len(m.get('reasoning_content', '')) for m in normalized) <= 500000:
        raise InvalidRequest('invalid_message_size', 'messages')
    a, b = body.get('max_tokens'), body.get('max_completion_tokens')
    if a is not None and b is not None and a != b:
        raise InvalidRequest('conflicting_output_limits', 'max_tokens')
    limit = b if b is not None else a if a is not None else 2048
    if type(limit) is not int or not 1 <= limit <= 7680:
        raise InvalidRequest('invalid_output_limit', 'max_tokens')
    request = {'messages': normalized}
    if tools:
        request.update(tools=tools, tool_choice=tool_choice,
                       parallel_tool_calls=body.get('parallel_tool_calls', True))
    # Include schemas and tool arguments in the size/Unicode checks.
    try:
        encoded = json.dumps(request, ensure_ascii=False, allow_nan=False)
        encoded.encode('utf-8')
    except (ValueError, UnicodeError):
        raise InvalidRequest('invalid_tool_json', 'tools')
    if len(encoded) > 1500000 or '\x00' in encoded:
        raise InvalidRequest('invalid_message_size', 'messages')
    # Omitted and explicit false retain the original request fingerprint.
    if body.get('enable_thinking', False):
        request['enable_thinking'] = True
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
        if result.get('reasoning_content'):
            value['choices'][0]['message']['reasoning_content'] = result['reasoning_content']
        if result.get('tool_calls'):
            value['choices'][0]['message']['tool_calls'] = result['tool_calls']
    if usage_only or not streaming:
        value['usage'] = {'prompt_tokens': result['input_tokens'], 'completion_tokens': result['output_tokens'],
                          'total_tokens': result['input_tokens'] + result['output_tokens']}
        if 'cached_input_tokens' in result:
            value['usage']['prompt_tokens_details'] = {'cached_tokens': result['cached_input_tokens']}
        # Keep standard usage intact. OpenWebUI consumes this llama.cpp-style
        # extension separately; emit it only once per response, not per delta.
        timing = {key: result[key] for key in ('decode_tps', 'generation_s', 'prefill_s', 'process_wall_s', 'cached_input_tokens', 'prompt_tokens_processed')
                  if key in result}
        if job.get('finished_at') is not None:
            timing['total_s'] = max(0, job['finished_at'] - job['created_at'])
        if timing:
            value['timings'] = timing
    return value
