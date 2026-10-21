const {chromium}=require('/Users/ivan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('fs'),path=require('path'),os=require('os'),assert=require('assert/strict');
(async()=>{
 const privateDir=path.join(os.homedir(),'.config/nx5-openwebui');
 const login=JSON.parse(fs.readFileSync(path.join(privateDir,'login.json'),'utf8'));
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const proof={started_at:new Date().toISOString(),chats:[],page_errors:[],http_errors:[]};
 const save=()=>fs.writeFileSync(path.join(__dirname,'ui-acceptance.json'),JSON.stringify(proof,null,2)+'\n');
 try{
  const ctx=await browser.newContext({viewport:{width:1440,height:1000},storageState:path.join(privateDir,'browser-auth.json')});
  const page=await ctx.newPage();
  page.on('pageerror',e=>proof.page_errors.push(e.message));
  page.on('response',r=>{if(r.status()>=400)proof.http_errors.push({status:r.status(),path:new URL(r.url()).pathname});});
  for(const [model,question,answer] of [['qwen36-128k','17×23','391'],['minicpm-64k','19×23','437']]){
   const h=await (await page.request.get('http://nx5.taild500c8.ts.net:18347/healthz')).json();
   assert.equal(h.state,'ready');assert.equal(h.active,0);
   await page.goto(login.url+'/?model='+model,{waitUntil:'domcontentloaded'});
   await page.locator('#chat-input').waitFor({state:'visible',timeout:45000});
   await page.waitForTimeout(2500);await page.keyboard.press('Escape');
   assert.ok((await page.getByRole('button',{name:/^已选择：/}).innerText()).includes(model));
   await page.locator('#chat-input').fill('统计显示验证：只回答 '+question+' 的计算结果。');
   const begin=Date.now();await page.locator('#chat-input').press('Enter');
   console.log('submitted',model);
   await page.waitForURL(/\/c\//,{timeout:45000});
   const record={model,url:page.url(),submitted_at:new Date(begin).toISOString()};proof.chats.push(record);save();
   await page.waitForFunction(()=>document.body.innerText.includes('生成速度')||document.body.innerText.includes('unsupported_parameter'),{},{timeout:180000});
   assert.ok(!(await page.locator('body').innerText()).includes('unsupported_parameter'));
   const data=await page.evaluate(async()=>{
    const id=location.pathname.split('/').pop();
    const r=await fetch('/api/v1/chats/'+id,{headers:{Authorization:'Bearer '+localStorage.token}});
    const d=await r.json();
    return Object.values(d.chat.history.messages).filter(m=>m.role==='assistant').map(m=>({id:m.id,content:m.content,usage:m.usage,statusHistory:m.statusHistory,done:m.done}));
   });
   const m=data.at(-1);assert.ok(m.content.includes(answer));assert.ok(!m.content.includes('生成速度'));
   assert.ok(m.usage.decode_tps>0&&m.usage.generation_s>0&&m.usage.total_s>=m.usage.generation_s);
   assert.equal(m.usage.output_tokens,m.usage.completion_tokens);assert.equal(m.usage.input_tokens,m.usage.prompt_tokens);
   record.message=m;record.browser_wall_s=(Date.now()-begin)/1000;save();
   await page.reload({waitUntil:'domcontentloaded'});
   await page.waitForFunction(()=>document.body.innerText.includes('生成速度'),{},{timeout:30000});
   await page.waitForTimeout(1500);await page.keyboard.press('Escape');
   record.survives_reload=true;
   await page.screenshot({path:path.join(__dirname,model+'-stats.png')});
   await page.locator('[id="info-'+m.id+'"]').hover();
   await page.waitForFunction(()=>document.body.innerText.includes('decode_tps'),{},{timeout:10000});
   await page.screenshot({path:path.join(__dirname,model+'-details.png')});
   record.details_visible=true;
   record.health_after=await (await page.request.get('http://nx5.taild500c8.ts.net:18347/healthz')).json();
   assert.equal(record.health_after.state,'ready');assert.equal(record.health_after.model_loaded,false);
   console.log(JSON.stringify({model,usage:m.usage,status:m.statusHistory,details:true,reload:true}));save();
  }
  await page.mouse.move(0,0);await page.setViewportSize({width:390,height:844});await page.waitForTimeout(1000);
  proof.mobile=await page.evaluate(()=>({width:innerWidth,bodyWidth:document.body.scrollWidth}));
  assert.ok(proof.mobile.bodyWidth<=proof.mobile.width);
  await page.screenshot({path:path.join(__dirname,'mobile.png')});
  assert.equal(proof.page_errors.length,0);
  assert.equal(proof.http_errors.filter(e=>!(e.status===403&&e.path==='/api/v1/channels/')).length,0);
  proof.passed=true;
 }finally{save();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
