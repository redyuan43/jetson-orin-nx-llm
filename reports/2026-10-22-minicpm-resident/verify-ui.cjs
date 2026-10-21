const {chromium}=require('/Users/ivan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('fs'),os=require('os'),path=require('path'),assert=require('assert/strict');
(async()=>{
const out=path.join(process.cwd(),'reports/2026-10-22-minicpm-resident');
const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
try{
 const ctx=await browser.newContext({viewport:{width:1440,height:1000},storageState:path.join(os.homedir(),'.config/nx5-openwebui/browser-auth.json')});
 const page=await ctx.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://nx6.taild500c8.ts.net:18348/',{waitUntil:'domcontentloaded'});await page.locator('#chat-input').waitFor({timeout:45000});await page.waitForTimeout(1800);
 const selected=await page.getByRole('button',{name:/^已选择：/}).allTextContents();
 const models=await page.evaluate(async()=>(await (await fetch('/api/models',{headers:{Authorization:'Bearer '+localStorage.token}})).json()).data.map(m=>m.id));assert.deepEqual(models,['minicpm-64k']);
 await page.getByRole('button',{name:/^已选择：/}).click();await page.screenshot({path:path.join(out,'webui-only-minicpm.png')});
 const webui={models,selected,errors:[...errors]};assert.equal(errors.length,0);
 await page.goto('http://nx6.taild500c8.ts.net:8300/capabilities',{waitUntil:'networkidle'});
 const card=page.locator('[data-gateway="nx5-ssd-llm"]');
 const text=await page.locator('body').innerText();assert.ok(text.includes('nx5 · MiniCPM 64K 常驻'));assert.ok(!text.includes('nx5 · Qwen 128K / MiniCPM 64K'));
 await page.screenshot({path:path.join(out,'fleet-desktop.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(out,'fleet-mobile.png'),fullPage:true});
 const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);assert.equal(overflow,false);assert.equal(errors.length,0);
 const proof={webui,fleet:{resident_label:true,mobile_overflow:overflow,errors}};fs.writeFileSync(path.join(out,'ui-checks.json'),JSON.stringify(proof,null,2)+'\n');console.log(proof);
}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
