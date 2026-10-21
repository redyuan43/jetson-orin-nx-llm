const {chromium}=require('/Users/ivan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('fs'),path=require('path');
(async()=>{
 const folder=__dirname, errors=[];
 const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://nx6.taild500c8.ts.net:8300/capabilities',{waitUntil:'domcontentloaded'});
  const card=page.locator('article.card').filter({has:page.getByRole('heading',{name:'nx5 · Qwen 128K / MiniCPM 64K',exact:true})});
  await card.waitFor({state:'visible',timeout:30000});
  await card.screenshot({path:path.join(folder,'fleet-desktop.png')});
  const text=await card.innerText();
  if(!text.includes('Qwen3.6')||!text.includes('MiniCPM-V4.7')||!text.includes('current')||!text.includes('OpenWebUI')||!text.includes('Base URL：http://nx5.taild500c8.ts.net:18347/v1'))throw Error('missing model or current contract');
  await page.setViewportSize({width:390,height:844});
  await card.scrollIntoViewIfNeeded();
  await card.screenshot({path:path.join(folder,'fleet-mobile.png')});
  const dimensions=await card.evaluate(e=>({width:e.clientWidth,scrollWidth:e.scrollWidth,bodyWidth:document.body.scrollWidth,viewport:innerWidth}));
  const links=await card.locator('a').evaluateAll(a=>a.map(x=>({text:x.innerText,path:x.getAttribute('href')})));
  const proof={text,links,dimensions,page_errors:errors};
  if(dimensions.scrollWidth>dimensions.width+1||dimensions.bodyWidth>dimensions.viewport||errors.length)throw Error(JSON.stringify(proof));
  fs.writeFileSync(path.join(folder,'browser-check.json'),JSON.stringify(proof,null,2)+'\n');
  console.log(JSON.stringify({passed:true,dimensions,page_errors:errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
