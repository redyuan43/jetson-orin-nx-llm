// Browser-only inspection of the unchanged model output; all network requests blocked.
const {chromium}=require('/Users/ivan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('fs');const path=require('path');const {pathToFileURL}=require('url');
(async()=>{
const folder=path.resolve(process.argv[2]);const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
try{
 const page=await browser.newPage({viewport:{width:1000,height:760}}),errors=[],blocked=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.dismiss());
 await page.route('**/*',route=>{if(route.request().url()===pathToFileURL(path.join(folder,'pelican.html')).href)return route.continue();blocked.push(route.request().url());return route.abort();});
 await page.goto(pathToFileURL(path.join(folder,'pelican.html')).href,{waitUntil:'load'});
 const snapshot=()=>page.evaluate(()=>({
  title:document.title,svg_count:document.querySelectorAll('svg').length,
  animations:document.getAnimations().map(a=>({target:a.effect?.target?.tagName,id:a.effect?.target?.id,cls:a.effect?.target?.getAttribute('class'),state:a.playState})),
  smil:[...document.querySelectorAll('animate,animateTransform,animateMotion')].map(a=>({tag:a.tagName,attribute:a.getAttribute('attributeName'),href:a.getAttribute('href')||a.getAttribute('xlink:href'),parent:a.parentElement.tagName,parent_id:a.parentElement.id,from:a.getAttribute('from'),to:a.getAttribute('to')})),
  groups:[...document.querySelectorAll('svg g')].map(g=>{const b=g.getBoundingClientRect(),m=g.getCTM();return {id:g.id,cls:g.getAttribute('class'),transform:g.getAttribute('transform'),css_transform:getComputedStyle(g).transform,box:{x:b.x,y:b.y,width:b.width,height:b.height},matrix:m?{a:m.a,b:m.b,c:m.c,d:m.d,e:m.e,f:m.f}:null};})
 }));
 const first=await snapshot();await page.screenshot({path:path.join(folder,'frame-0.png')});
 await page.waitForTimeout(700);const next=await snapshot();await page.screenshot({path:path.join(folder,'frame-700.png')});
 await page.waitForTimeout(1300);const later=await snapshot();await page.screenshot({path:path.join(folder,'frame-2000.png')});
 const controls={labels:await page.locator('button').allTextContents()};
 const animationStates=()=>page.evaluate(()=>document.getAnimations().map(a=>({state:a.playState,duration:a.effect.getTiming().duration})));
 const pause=page.getByRole('button',{name:/暂停/});const play=page.getByRole('button',{name:/播放|继续/});const speed=page.getByRole('button',{name:/加速/});
 if(await pause.count()===1){await pause.click();await page.waitForTimeout(100);controls.after_pause=await animationStates();}
 if(await play.count()===1){await play.click();await page.waitForTimeout(100);controls.after_play=await animationStates();}
 if(await speed.count()===1){controls.before_speed=await animationStates();await speed.click();await page.waitForTimeout(100);controls.after_speed=await animationStates();}
 await page.setViewportSize({width:390,height:844});await page.waitForTimeout(100);await page.screenshot({path:path.join(folder,'frame-mobile.png')});
 fs.writeFileSync(path.join(folder,'browser-check.json'),JSON.stringify({page_errors:errors,blocked_requests:blocked,controls,first,next,later},null,2)+'\n');
 console.log(JSON.stringify({errors,blocked,svg_count:first.svg_count,css_animations:first.animations.length,smil_animations:first.smil.length}));
}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
