import asyncio, base64, json, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
from websockets.sync.client import connect
import hashlib

html = Path(sys.argv[1]).resolve()
out = html.parent
chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
with tempfile.TemporaryDirectory(prefix='nx5-pelican-chrome-') as profile:
    log = open(out / 'browser.stderr.log', 'w')
    proc = subprocess.Popen([chrome, '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--remote-debugging-port=0', '--user-data-dir='+profile, 'about:blank'], stdout=log, stderr=log)
    try:
        port_file=Path(profile)/'DevToolsActivePort'
        for _ in range(200):
            if port_file.exists(): break
            time.sleep(.05)
        port=int(port_file.read_text().splitlines()[0])
        targets=json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list'))
        target=next(t for t in targets if t['type']=='page')
        with connect(target['webSocketDebuggerUrl'], max_size=25_000_000) as ws:
            seq=0; events=[]
            def call(method, params=None):
                global seq
                seq+=1
                ws.send(json.dumps({'id':seq,'method':method,'params':params or {}}))
                while True:
                    r=json.loads(ws.recv(timeout=20))
                    if r.get('id')==seq:
                        if 'error' in r: raise RuntimeError(r)
                        return r.get('result',{})
                    events.append(r)
            def evaluate(expression):
                r=call('Runtime.evaluate', {'expression':expression,'returnByValue':True,'awaitPromise':True})
                if 'exceptionDetails' in r: raise RuntimeError(r)
                return r['result'].get('value')
            call('Page.enable'); call('Runtime.enable'); call('Network.enable')
            call('Emulation.setDeviceMetricsOverride', {'width':1100,'height':800,'deviceScaleFactor':1,'mobile':False})
            call('Page.navigate',{'url':html.as_uri()})
            for _ in range(100):
                if evaluate('document.readyState')=='complete': break
                time.sleep(.05)
            info=evaluate('''({title:document.title,svgCount:document.querySelectorAll('svg').length,smilCount:document.querySelectorAll('animate,animateTransform,animateMotion').length,animations:document.getAnimations().map(a=>({name:a.animationName,state:a.playState,duration:a.effect.getTiming().duration,iterations:String(a.effect.getTiming().iterations)})),externalResources:[...document.querySelectorAll('[src],[href]')].map(e=>e.getAttribute('src')||e.getAttribute('href')).filter(u=>/^https?:/.test(u))})''')
            for ms, label in [(0,'frame-0'), (700,'frame-700')]:
                evaluate(f'''(async()=>{{document.getAnimations().forEach(a=>{{a.pause();a.currentTime={ms};}});document.querySelectorAll('svg').forEach(s=>{{s.pauseAnimations();s.setCurrentTime({ms/1000});}});await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return true;}})()''')
                data=call('Page.captureScreenshot', {'format':'png','captureBeyondViewport':False})['data']
                (out/(label+'.png')).write_bytes(base64.b64decode(data))
            info['exceptions']=[e['params'] for e in events if e.get('method')=='Runtime.exceptionThrown']
            info['externalRequests']=[e['params']['request']['url'] for e in events if e.get('method')=='Network.requestWillBeSent' and e['params']['request']['url'].startswith(('http://','https://'))]
            info['frameSha256']={name:hashlib.sha256((out/name).read_bytes()).hexdigest() for name in ['frame-0.png','frame-700.png']}
            info['framesDiffer']=info['frameSha256']['frame-0.png'] != info['frameSha256']['frame-700.png']
            (out/'browser-check.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps(info,ensure_ascii=False,indent=2))
    finally:
        proc.terminate()
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        log.close()
