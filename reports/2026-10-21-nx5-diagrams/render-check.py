import base64, json, subprocess, tempfile, time, urllib.request
from pathlib import Path
from websockets.sync.client import connect

root = Path(__file__).resolve().parents[2]
folder = root / 'docs/diagrams'
checks=[]
chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
with tempfile.TemporaryDirectory(prefix='nx5-svg-check-') as profile:
    proc=subprocess.Popen([chrome,'--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        port_file=Path(profile)/'DevToolsActivePort'
        for _ in range(200):
            if port_file.exists(): break
            time.sleep(.05)
        port=int(port_file.read_text().splitlines()[0])
        targets=json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list'))
        target=next(t for t in targets if t['type']=='page')
        with connect(target['webSocketDebuggerUrl'],max_size=50_000_000) as ws:
            seq=0
            def call(method,params=None):
                global seq
                seq+=1
                ws.send(json.dumps({'id':seq,'method':method,'params':params or {}}))
                while True:
                    r=json.loads(ws.recv(timeout=30))
                    if r.get('id')==seq:
                        if 'error' in r: raise RuntimeError(r)
                        return r.get('result',{})
            def ev(expr):
                r=call('Runtime.evaluate',{'expression':expr,'returnByValue':True,'awaitPromise':True})
                if 'exceptionDetails' in r: raise RuntimeError(r)
                return r['result'].get('value')
            call('Page.enable');call('Runtime.enable')
            for name,h in [('nx5-minicpm-64k-storage',1700),('nx5-minicpm-capabilities',1760)]:
                call('Emulation.setDeviceMetricsOverride',{'width':1800,'height':h,'deviceScaleFactor':1,'mobile':False})
                call('Page.navigate',{'url':(folder/(name+'.svg')).as_uri()})
                for _ in range(200):
                    if ev('document.readyState')=='complete':break
                    time.sleep(.05)
                ev('document.fonts.ready.then(()=>true)')
                ev('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>r(true))))')
                info=ev('''(()=>{const svg=document.documentElement;const vb=svg.viewBox.baseVal;const a=[...document.querySelectorAll('text')].map(e=>{const b=e.getBBox();return {text:e.textContent,x:b.x,y:b.y,w:b.width,h:b.height,maxw:Number(e.getAttribute('data-max-width')||0)}});const out=a.filter(b=>b.x<0||b.y<0||b.x+b.w>vb.width+.5||b.y+b.h>vb.height+.5);const wide=a.filter(b=>b.maxw&&b.w>b.maxw+.5);const overlaps=[];for(let i=0;i<a.length;i++)for(let j=i+1;j<a.length;j++){const x=a[i],y=a[j];if(Math.min(x.x+x.w,y.x+y.w)-Math.max(x.x,y.x)>2&&Math.min(x.y+x.h,y.y+y.h)-Math.max(x.y,y.y)>2)overlaps.push([x.text,y.text]);}return {title:document.querySelector('title').textContent,width:vb.width,height:vb.height,textCount:a.length,outsideCanvas:out,tooWide:wide,textOverlaps:overlaps,images:document.querySelectorAll('image').length,links:[...document.querySelectorAll('a')].map(a=>a.getAttribute('href'))};})()''')
                data=call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})['data']
                (folder/(name+'.png')).write_bytes(base64.b64decode(data))
                info['file']=name+'.svg'
                checks.append(info)
            call('Browser.close')
    finally:
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)
        time.sleep(.2)
(root/'reports/2026-10-21-nx5-diagrams/render-check.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(checks,ensure_ascii=False,indent=2))
