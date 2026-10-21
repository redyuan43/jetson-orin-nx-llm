"""
title: NX5 generation statistics
description: Display measured generation TPS and elapsed time without changing model text.
version: 1.0.0
"""
import math


class Filter:
    models = {'qwen36-128k', 'minicpm-64k'}

    async def inlet(self, body: dict) -> dict:
        if body.get('model') in self.models and body.get('stream'):
            body['stream_options'] = {**(body.get('stream_options') or {}), 'include_usage': True}
        return body

    async def outlet(self, body: dict, __event_emitter__=None) -> dict:
        if body.get('model') not in self.models or not __event_emitter__:
            return body
        messages = body.get('messages', [])
        if not messages or messages[-1].get('role') != 'assistant':
            return body
        usage = messages[-1].get('usage') or {}
        keys = ('decode_tps', 'generation_s', 'total_s')
        if not all(type(usage.get(k)) in (int, float) and math.isfinite(usage[k]) and usage[k] >= 0 for k in keys):
            return body
        await __event_emitter__({'type': 'status', 'data': {
            'action': 'nx5_generation_stats', 'done': True,
            'description': (f"生成速度 {usage['decode_tps']:.2f} token/s · "
                            f"生成耗时 {usage['generation_s']:.1f} 秒 · "
                            f"总耗时 {usage['total_s']:.1f} 秒")}})
        return body
