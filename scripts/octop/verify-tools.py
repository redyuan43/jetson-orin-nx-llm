#!/usr/bin/env python3
"""Run inside the Octop container; read an authenticated provider record from stdin.

Only synthetic add-tool fixtures are used. Never log the input provider record.
Requires the service to be idle; performs two model calls per configured model.
"""
import sys,json,asyncio,time,urllib.request
from octop_harness.config import ModelConfig,ProviderConfig
from octop_harness.llm.factory import build_chat_model
from octop.infra.agents.providers.reasoning import reasoning_capability,reasoning_request_parameters
from langchain_core.messages import HumanMessage,ToolMessage
row=json.load(sys.stdin)
async def main():
 results=[]
 for raw in row['models']:
  with urllib.request.urlopen('http://nx5.taild500c8.ts.net:18347/healthz',timeout=10) as r:h=json.load(r)
  assert h['state']=='ready', 'Existing user job is active'
  cfg=ModelConfig.from_dict(raw)
  provider=ProviderConfig(id=row['name'],base_url=row['base_url'],api_key=row['api_key'],models=[cfg],stream_usage=True)
  llm=build_chat_model(provider,cfg)
  llm.max_retries=0
  overrides=reasoning_request_parameters(reasoning_capability(raw,base_url=row['base_url']),mode=None,effort=None)
  assert overrides['extra_body']['enable_thinking'] is True
  tools=[{'type':'function','function':{'name':'add','description':'Return the sum of two integers.','parameters':{'type':'object','properties':{'a':{'type':'integer'},'b':{'type':'integer'}},'required':['a','b'],'additionalProperties':False}}}]
  messages=[HumanMessage(content='请调用 add 工具计算 17 加 23。必须使用工具，收到结果后最终答案只写数字。')]
  merged=None;started=time.monotonic();call_chunks=0
  async for chunk in llm.bind_tools(tools,tool_choice='required',parallel_tool_calls=False).astream(messages,max_tokens=512,**overrides):
   call_chunks+=len(chunk.tool_call_chunks or [])
   merged=chunk if merged is None else merged+chunk
  assert merged.tool_calls, 'No parsed function call'
  assert len(merged.tool_calls)==1
  call=merged.tool_calls[0];assert call['name']=='add' and call['args']=={'a':17,'b':23},call
  first_reason=len(merged.additional_kwargs.get('reasoning_content',''))
  first_time=time.monotonic()-started
  print(json.dumps({'phase':'tool_called','model':raw['id'],'arguments':call['args'],'reasoning_chars':first_reason,'wall_s':first_time}),flush=True)
  messages += [merged,ToolMessage(content=str(call['args']['a']+call['args']['b']),tool_call_id=call['id'])]
  answer=None
  async for chunk in llm.bind_tools(tools,tool_choice='none').astream(messages,max_tokens=512,**overrides):
   answer=chunk if answer is None else answer+chunk
  assert answer.content.strip()=='40', 'Final answer mismatch'
  with urllib.request.urlopen('http://nx5.taild500c8.ts.net:18347/healthz',timeout=10) as r:after=json.load(r)
  assert after['state']=='ready' and after['model_loaded'] is True
  proof={'model':raw['id'],'tool_name':call['name'],'arguments':call['args'],'tool_result':'40','answer':answer.content,'reasoning_on':True,'reasoning_chars_first_turn':first_reason,'reasoning_chars_answer':len(answer.additional_kwargs.get('reasoning_content','')),'tool_delta_chunks':call_chunks,'first_turn_wall_s':first_time,'total_wall_s':time.monotonic()-started,'usage_first':merged.usage_metadata,'usage_answer':answer.usage_metadata,'resident_pid':after['resident_pid'],'lease_held':after['lease_status']=='held'}
  results.append(proof);print(json.dumps(proof,ensure_ascii=False),flush=True)
 print(json.dumps({'passed':True,'models':len(results)}),flush=True)
asyncio.run(main())
