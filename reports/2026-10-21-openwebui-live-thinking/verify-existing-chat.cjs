// Read-only inspection: never send keyboard shortcuts or generation/stop actions.
const {chromium}=require('/Users/ivan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('fs'),path=require('path'),os=require('os'),assert=require('assert/strict');
(async()=>{
 const url=process.argv[2];assert.ok(/^http:\/\/nx6\.taild500c8\.ts\.net:18348\/c\/[a-f0-9-]+$/.test(url));
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try{
  const ctx=await browser.newContext({viewport:{width:1440,height:1050},storageState:path.join(os.homedir(),'.config/nx5-openwebui/browser-auth.json')});
  const page=await ctx.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url,{waitUntil:'domcontentloaded'});await page.locator('#chat-input').waitFor({timeout:45000});
  await page.waitForTimeout(2000);
  const proof=await page.evaluate(async()=>{
   const h={Authorization:'Bearer '+localStorage.token};
   const d=await (await fetch('/api/v1/chats/'+location.pathname.split('/').pop(),{headers:h})).json();
   const s=await (await fetch('/api/v1/users/user/settings',{headers:h})).json();
   const m=d.chat.history.messages[d.chat.history.currentId];
   const reasoning=(m.output||[]).find(o=>o.type==='reasoning');
   const answer=(m.output||[]).find(o=>o.type==='message');
   const rt=(reasoning?.content||[]).map(c=>c.text||'').join('');
   const at=(answer?.content||[]).map(c=>c.text||'').join('');
   const blocks=[...document.querySelectorAll('.markdown-prose')].filter(e=>e.offsetWidth||e.offsetHeight);
   const thinkingButton=[...document.querySelectorAll('button')].find(b=>/思考过程|思考用时/.test(b.textContent));
   const thinkingText=thinkingButton?.parentElement.querySelector('.markdown-prose');
   return {at:new Date().toISOString(),expandDetails:s.ui.expandDetails,done:m.done,error:m.error||null,reasoning_chars:rt.length,answer_chars:at.length,visible_markdown_chars:blocks.map(e=>e.innerText.length),reasoning_visible:!!(thinkingText&&(thinkingText.offsetWidth||thinkingText.offsetHeight)&&thinkingText.innerText.length>100),answer_visible:blocks.some(e=>e.innerText.includes(at.slice(0,30))),stats_visible:document.body.innerText.includes('生成速度 11.25 token/s'),reasoning_expanded:[...document.querySelectorAll('button')].some(b=>/思考过程|思考用时/.test(b.textContent)&&b.getAttribute('aria-expanded')==='true')};
  });
  proof.page_errors=errors;fs.writeFileSync(path.join(__dirname,'ui-recovery-check.json'),JSON.stringify(proof,null,2)+'\n');
  assert.equal(proof.expandDetails,true);assert.equal(proof.done,true);assert.equal(proof.error,null);assert.ok(proof.reasoning_visible&&proof.answer_visible&&proof.reasoning_expanded&&proof.stats_visible);assert.equal(errors.length,0);
  console.log(JSON.stringify(proof));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
