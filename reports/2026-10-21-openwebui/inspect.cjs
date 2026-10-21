const {chromium}=require('/Users/ivan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('fs'),path=require('path'),os=require('os');
(async()=>{
 const login=JSON.parse(fs.readFileSync(path.join(os.homedir(),'.config/nx5-openwebui/login.json'),'utf8'));
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(login.url,{waitUntil:'domcontentloaded',timeout:30000});
  await page.waitForFunction(()=>document.querySelector('input[type="email"]')||document.body.innerText.includes("Open WebUI"),{},{timeout:45000});
  await page.waitForTimeout(1500);
  await page.locator('input[type="email"]').fill(login.email);
  await page.locator('input[type="password"]').fill(login.password);
  await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.waitForFunction(()=>!location.pathname.startsWith('/auth'),{},{timeout:30000});
  await page.waitForTimeout(3000);
  const state=path.join(os.homedir(),'.config/nx5-openwebui/browser-auth.json');
  await page.context().storageState({path:state});fs.chmodSync(state,0o600);
  console.log((await page.locator('body').innerText()).slice(0,5000));
  console.log(await page.locator('input').evaluateAll(xs=>xs.map(x=>({type:x.type,name:x.name,placeholder:x.placeholder}))));
  await page.screenshot({path:path.join(__dirname,'logged-in.png')});
  console.log(JSON.stringify({page_errors:errors}));
 } finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
