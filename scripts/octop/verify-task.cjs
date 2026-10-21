const fs=require('fs'),path=require('path'),os=require('os');
// Private auth.json is never written to reports. Creates a fresh Octop thread.
const privateRoot=path.join(os.homedir(),'.config/octop-bus002');
const auth=JSON.parse(fs.readFileSync(path.join(privateRoot,'auth.json')));
const model=process.argv[2] || 'minicpm-64k';
if(!['qwen36-128k','minicpm-64k'].includes(model))throw Error('invalid model');
(async()=>{
const health=await (await fetch('http://nx5.taild500c8.ts.net:18347/healthz')).json();
if(health.state!=='ready'||health.active!==0)throw Error('Existing request is active');
const created=await (await fetch('http://bus002.taild500c8.ts.net:8088/api/agents/E5XJH1/threads',{method:'POST',headers:{Authorization:'Bearer '+auth.access_token}})).json();
if(!created.thread_id)throw Error('Could not create isolated test thread');
const started=Date.now(),events={},toolResults=[];let thread=created.thread_id,text='',thinkingChars=0,done=false,errors=[];
const session=created.session_key;
const raw=path.join(privateRoot,'acceptance-'+model+'-'+Date.now()+'.jsonl');fs.writeFileSync(raw,'',{mode:0o600});
const ws=new WebSocket('ws://bus002.taild500c8.ts.net:8088/api/agents/E5XJH1/chat/ws?token='+encodeURIComponent(auth.access_token));
const timer=setTimeout(()=>{console.log(JSON.stringify({pending:true,thread_id:thread,session_key:session,events}));ws.close();process.exitCode=2;},1800000);
ws.addEventListener('open',()=>{
 ws.send(JSON.stringify({type:'user_turn',text:'这是工具接入验收。请必须调用 current_time 工具获取当前时间，然后只回答工具返回的日期和时区。不要猜测时间，不要调用其他工具。',session_key:session,thread_id:thread,model:'nx5-local/'+model,reasoning_mode:'enabled',conversation_mode:'ask',mcp_servers:[],knowledge_base_ids:[],skills:[],delivery:'follow_up'}));
 console.log(JSON.stringify({submitted:true,model,session_key:session}));
});
ws.addEventListener('message',e=>{
 const x=JSON.parse(e.data);fs.appendFileSync(raw,JSON.stringify(x)+'\n');
 const type=x.type||'unknown';events[type]=(events[type]||0)+1;if(x.thread_id)thread=x.thread_id;
 if(type==='tool_result' && x.node==='tools'){toolResults.push(x);console.log(JSON.stringify(x));}
 if(type==='error'){errors.push(x);console.log(JSON.stringify(x));}
 if(['thread','turn.started','turn_status','session','thread_created'].includes(type))console.log(JSON.stringify(x));
 if(type==='token')text+=x.content||x.text||'';
 if(type==='reasoning')thinkingChars+=(x.content||x.text||'').length;
 if(type==='done'||type==='turn.ended'){
  done=true;clearTimeout(timer);const proof={model,thread_id:thread,session_key:session,wall_s:(Date.now()-started)/1000,events,tool_results:toolResults,text,thinking_chars:thinkingChars,errors,done:true};
  fs.writeFileSync(path.join(process.cwd(),'reports/2026-10-22-octop-tools','octop-task-'+model+'.json'),JSON.stringify(proof,null,2)+'\n');
  console.log(JSON.stringify(proof));ws.close();
  if(errors.length||!toolResults.some(r=>(r.messages||[]).some(m=>m.name==='current_time' && m.status==='success'))||!text.trim()||thinkingChars===0||x.failed)process.exitCode=1;
 }
});
ws.addEventListener('error',()=>{console.log(JSON.stringify({websocket_error:true,thread_id:thread}));});
ws.addEventListener('close',()=>{clearTimeout(timer);if(!done){console.log(JSON.stringify({closed_before_done:true,thread_id:thread,events}));process.exitCode=2;}});
})().catch(e=>{console.error(e.message);process.exitCode=1});
