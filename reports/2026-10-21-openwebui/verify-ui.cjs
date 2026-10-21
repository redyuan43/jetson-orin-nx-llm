const {chromium}=require('/Users/ivan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('fs'),path=require('path'),os=require('os');
function validate(proof){
 // OpenWebUI asks for optional Channels even when that feature is disabled.
 // Verified against v0.12.3 routers/channels.py:159-165; retain the raw responses.
 proof.expected_denials=proof.http_errors.filter(e=>e.status===403&&e.path==='/api/v1/channels/');
 const unexpected=proof.http_errors.filter(e=>!(e.status===403&&e.path==='/api/v1/channels/'));
 for(const [model,answer] of [['qwen36-128k','391'],['minicpm-64k','437']]){
  const chat=proof.chats.find(c=>c.model===model);
  if(!chat||!chat.text.split('\n').some(line=>line.trim()===answer)
     ||chat.health_after.state!=='ready'||chat.health_after.active!==0
     ||chat.health_after.model_loaded!==false)throw Error('Incomplete chat evidence: '+model);
 }
 if(proof.page_errors.length||unexpected.length||proof.mobile.bodyWidth>proof.mobile.width)
  throw Error('Unexpected browser errors or horizontal overflow');
 proof.passed=true;
 proof.validation_note='Channels 403 is the disabled optional feature; both chat results and release of the model were verified.';
}
(async()=>{
 const outputFile=path.join(__dirname,'ui-acceptance.json');
 if(process.argv.includes('--review-existing')){
  const proof=JSON.parse(fs.readFileSync(outputFile,'utf8'));
  validate(proof);
  proof.reviewed_at=new Date().toISOString();
  fs.writeFileSync(outputFile,JSON.stringify(proof,null,2)+'\n');
  console.log(JSON.stringify({passed:true,chats:proof.chats.length,expected_denials:proof.expected_denials.length,inference_rerun:false}));
  return;
 }
 const privateDir=path.join(os.homedir(),'.config/nx5-openwebui');
 const login=JSON.parse(fs.readFileSync(path.join(privateDir,'login.json'),'utf8'));
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 const previous=fs.existsSync(outputFile)?JSON.parse(fs.readFileSync(outputFile,'utf8')):null;
 const resume=previous&&!previous.passed&&previous.chats?.length===1;
 const proof=resume?previous:{url:login.url,started_at:new Date().toISOString(),chats:[],page_errors:[],http_errors:[]};
 try{
  const ctx=await browser.newContext({viewport:{width:1440,height:1000},storageState:path.join(privateDir,'browser-auth.json')});
  const page=await ctx.newPage();
  page.on('pageerror',e=>{proof.page_errors.push(e.message);console.log('PAGE_ERROR',e.message);});
  page.on('response',r=>{if(r.status()>=400)proof.http_errors.push({status:r.status(),path:new URL(r.url()).pathname});});
  await page.goto(login.url,{waitUntil:'domcontentloaded'});
  await page.locator('#chat-input').waitFor({state:'visible',timeout:45000});
  await page.waitForTimeout(3000);
  await page.keyboard.press('Escape');
  async function idle(){
   for(let i=0;i<180;i++){
    const h=await (await page.request.get('http://nx5.taild500c8.ts.net:18347/healthz')).json();
    if(h.state==='ready'&&h.active===0)return h;
    await page.waitForTimeout(1000);
   }
   throw Error('nx5 did not return to idle; preserve original request');
  }
  proof.health_before=await idle();
  for(const [index,model,prompt,expected] of [[0,'qwen36-128k','连接验证：只回答计算结果，17×23等于多少？','391'],[1,'minicpm-64k','连接验证：只回答计算结果，19×23等于多少？','437']]){
   if(resume&&index===0)continue;
   if(index){await page.goto(login.url+'/?model='+model,{waitUntil:'domcontentloaded'});await page.locator('#chat-input').waitFor({state:'visible',timeout:45000});await page.waitForTimeout(3000);await page.keyboard.press('Escape');}
   await page.getByRole('button',{name:/^已选择：/}).click();
   await page.waitForTimeout(500);
   console.log('selection menu', (await page.locator('body').innerText()).slice(-550));
   await page.getByText('qwen36-128k',{exact:true}).last().waitFor({state:'visible'});
   await page.getByText('minicpm-64k',{exact:true}).last().waitFor({state:'visible'});
   if(!index)await page.screenshot({path:path.join(__dirname,'model-selector.png')});
   await page.getByText(model,{exact:true}).last().click();
   const begin=Date.now();
   await page.locator('#chat-input').fill(prompt);
   await page.locator('#chat-input').press('Enter');
   console.log('submitted',model);
   await page.waitForFunction(expected=>document.body.innerText.includes(expected)||document.body.innerText.includes('unsupported_parameter'),expected,{timeout:180000});
   if((await page.locator('body').innerText()).includes('unsupported_parameter'))throw Error('upstream unsupported_parameter');
   const health=await idle();
   await page.waitForTimeout(1000);
   const result={model,prompt,expected,wall_s:(Date.now()-begin)/1000,url:page.url(),text:await page.locator('body').innerText(),health_after:health};
   proof.chats.push(result);
   await page.screenshot({path:path.join(__dirname,model+'-chat.png')});
   console.log(JSON.stringify({model,answer:expected,wall_s:result.wall_s,health:health.state}));
   fs.writeFileSync(path.join(__dirname,'ui-acceptance.json'),JSON.stringify(proof,null,2)+'\n');
  }
  await page.setViewportSize({width:390,height:844});
  await page.waitForTimeout(500);
  await page.screenshot({path:path.join(__dirname,'mobile.png')});
  proof.mobile=await page.evaluate(()=>({width:innerWidth,bodyWidth:document.body.scrollWidth}));
  validate(proof);
 }finally{
  fs.writeFileSync(path.join(__dirname,'ui-acceptance.json'),JSON.stringify(proof,null,2)+'\n');
  await browser.close();
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
