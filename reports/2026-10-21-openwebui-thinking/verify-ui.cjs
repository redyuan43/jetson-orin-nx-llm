const {chromium}=require('/Users/ivan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('fs'),path=require('path'),os=require('os'),assert=require('assert/strict');
(async()=>{
 const dir=__dirname, privateDir=path.join(os.homedir(),'.config/nx5-openwebui');
 const login=JSON.parse(fs.readFileSync(path.join(privateDir,'login.json'),'utf8'));
 const proof={started_at:new Date().toISOString(),requests:[],page_errors:[],http_errors:[]};
 const save=()=>fs.writeFileSync(path.join(dir,'ui-acceptance.json'),JSON.stringify(proof,null,2)+'\n');
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const ctx=await browser.newContext({viewport:{width:1440,height:1050},storageState:path.join(privateDir,'browser-auth.json')});
  const page=await ctx.newPage();
  page.on('pageerror',e=>proof.page_errors.push(e.message));
  page.on('response',r=>{if(r.status()>=400)proof.http_errors.push({status:r.status(),path:new URL(r.url()).pathname});});
  async function choose(label){
   await page.locator('button[aria-label="模型控件"]').click();
   const thinking=page.locator('button[aria-label="思考模式"]');
   await thinking.hover();await thinking.click();
   await page.getByRole('menuitemradio',{name:label,exact:true}).waitFor({state:'visible'});
   await page.screenshot({path:path.join(dir,'thinking-menu.png')});
   await page.getByRole('menuitemradio',{name:label,exact:true}).click();
   await page.waitForTimeout(400);await page.keyboard.press('Escape');await page.keyboard.press('Escape');
  }
  for(const model of ['qwen36-128k','minicpm-64k']){
   const h=await (await page.request.get('http://nx5.taild500c8.ts.net:18347/healthz')).json();assert.equal(h.state,'ready');
   await page.goto(login.url+'/?model='+model,{waitUntil:'domcontentloaded'});
   await page.locator('#chat-input').waitFor({state:'visible',timeout:45000});await page.waitForTimeout(2000);await page.keyboard.press('Escape');
   for(const [enabled,label,prompt,answer] of [[true,'开启','计算 17×23，最终答案只写数字。','391'],[false,'关闭（标准测试）','将上一轮最终答案加1，只写结果。','392']]){
    await choose(label);
    const record={model,enabled,submitted_at:new Date().toISOString()};proof.requests.push(record);save();
    const count=await page.locator('[id^="info-"]').count();
    await page.locator('#chat-input').fill(prompt);await page.locator('#chat-input').press('Enter');
    console.log('submitted',model,enabled);
    await page.waitForURL(/\/c\//,{timeout:45000});record.url=page.url();save();
    await page.waitForFunction(n=>document.querySelectorAll('[id^="info-"]').length>n,count,{timeout:300000});
    await page.waitForTimeout(1500);
    const message=await page.evaluate(async()=>{
     const id=location.pathname.split('/').pop();const r=await fetch('/api/v1/chats/'+id,{headers:{Authorization:'Bearer '+localStorage.token}});const d=await r.json();
     const m=Object.values(d.chat.history.messages).filter(m=>m.role==='assistant').at(-1);
     return {id:m.id,content:m.content,output:m.output,usage:m.usage,done:m.done,statusHistory:m.statusHistory,error:m.error};
    });
    const reasoning=(message.output||[]).filter(o=>o.type==='reasoning'||o.attributes?.type==='reasoning_content');
    const reasoningChars=reasoning.reduce((n,o)=>n+(o.content||[]).map(p=>p.text||'').join('').length,0);
    const contents=(message.output||[]).filter(o=>o.type==='message').map(o=>JSON.stringify(o)).join('');
    record.reasoning_items=reasoning.length;record.reasoning_chars=reasoningChars;record.output_types=(message.output||[]).map(o=>({type:o.type,attribute_type:o.attributes?.type}));record.usage=message.usage;record.answer_present=(contents||message.content).includes(answer);record.error=message.error;
    save();
    assert.equal(record.answer_present,true);assert.ok(message.usage.decode_tps>0);assert.ok(!message.error);
    if(enabled)assert.ok(reasoningChars>0);else assert.equal(reasoning.length,0);
    await page.screenshot({path:path.join(dir,model+(enabled?'-on':'-off')+'.png')});
    record.health_after=await (await page.request.get('http://nx5.taild500c8.ts.net:18347/healthz')).json();assert.equal(record.health_after.state,'ready');
    console.log(JSON.stringify({model,enabled,reasoning_items:reasoning.length,reasoning_chars:reasoningChars,answer_present:record.answer_present,tps:message.usage.decode_tps}));save();
   }
   // Leave ordinary chat in the default enabled mode after the off-mode test.
   await choose('默认 · 开启');
  }
  await page.setViewportSize({width:390,height:844});await page.waitForTimeout(1000);
  await page.locator('button[aria-label="模型控件"]').click();
  await page.locator('button[aria-label="思考模式"]').click();
  await page.getByRole('menuitemradio',{name:'开启',exact:true}).waitFor({state:'visible'});
  await page.screenshot({path:path.join(dir,'mobile-menu.png')});
  proof.mobile=await page.evaluate(()=>({width:innerWidth,bodyWidth:document.body.scrollWidth}));assert.ok(proof.mobile.bodyWidth<=390);
  assert.equal(proof.page_errors.length,0);assert.equal(proof.http_errors.filter(e=>!(e.status===403&&e.path==='/api/v1/channels/')).length,0);
  proof.passed=true;
 }finally{save();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
