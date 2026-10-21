import copy
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('generation_stats', Path(__file__).resolve().parents[1]/'deploy/openwebui/generation_stats.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class StatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_requests_usage_and_status_preserves_answer(self):
        for model in ('qwen36-128k','minicpm-64k'):
            f=module.Filter()
            request={'model':model,'stream':True,'messages':[{'role':'user','content':'hello'}]}
            self.assertEqual((await f.inlet(request))['stream_options'],{'include_usage':True})
            body={'model':model,'messages':[{'role':'assistant','content':'<svg/>','usage':{
                'decode_tps':9.32,'generation_s':98.1,'total_s':141.7}}]}
            before=copy.deepcopy(body);events=[]
            async def emit(event):events.append(event)
            self.assertEqual(await f.outlet(body,emit),before)
            self.assertEqual(events[0]['data']['description'],'生成速度 9.32 token/s · 生成耗时 98.1 秒 · 总耗时 141.7 秒')
            self.assertTrue(events[0]['data']['done'])

    async def test_missing_or_invalid_metrics_never_fabricate_speed(self):
        events=[]
        async def emit(event):events.append(event)
        for usage in ({},{'decode_tps':float('nan'),'generation_s':1,'total_s':2}):
            await module.Filter().outlet({'model':'qwen36-128k','messages':[{'role':'assistant','usage':usage}]},emit)
        self.assertFalse(events)
        request={'model':'qwen36-128k','stream':False}
        self.assertNotIn('stream_options',await module.Filter().inlet(request))
        other={'model':'unrelated','stream':True}
        self.assertEqual(await module.Filter().inlet(other),other)
