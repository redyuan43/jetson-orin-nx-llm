#!/usr/bin/env python3
"""Package the two vector posters and three attributed official figures as PDF."""
import argparse
import base64
import json
import math
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path

from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / 'reports/2026-10-21-nx5-diagrams'
BUILD = REPORT / 'pdf-build'
DIAGRAMS = ROOT / 'docs/diagrams'
OUTPUT = DIAGRAMS / 'nx5-minicpm-64k-architecture-and-capabilities.pdf'
FONT = "'PingFang SC','Noto Sans CJK SC','Microsoft YaHei',Arial,sans-serif"
NS = 'http://www.w3.org/2000/svg'
ET.register_namespace('', NS)


def document(content, height, title):
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{escape(title)}</title><style>
@page {{size: 1800px {height}px; margin:0}}
* {{box-sizing:border-box}} html,body {{margin:0;width:1800px;height:{height}px;background:#F3F6F8}}
body {{font-family:{FONT};color:#173149;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
svg {{display:block}} a {{color:#346FAA;text-decoration:none}}
.page {{position:relative;width:1800px;height:{height}px;overflow:hidden}}
.eyebrow {{position:absolute;left:60px;top:50px;color:#147D76;font-size:18px;font-weight:650;letter-spacing:1px}}
.number {{position:absolute;right:60px;top:50px;font-size:18px;color:#536B7D}}
h1 {{position:absolute;left:60px;top:86px;font-size:44px;margin:0;line-height:1.35;font-weight:700}}
.subtitle {{position:absolute;left:60px;top:157px;font-size:22px;color:#536B7D;line-height:1.5}}
.figure {{position:absolute;left:60px;top:214px;width:1680px;border:1.5px solid #DCE5EB;background:white;border-radius:22px;padding:19px}}
.figure img {{display:block;width:1640px;height:auto}}
.notes {{position:absolute;left:60px;width:1680px;padding:20px 24px;border:1.5px solid #DCE5EB;border-radius:16px;background:white;font-size:21px;line-height:1.6;color:#536B7D}}
.footer {{position:absolute;bottom:42px;left:60px;right:60px;display:flex;justify-content:space-between;font-size:18px;color:#536B7D;line-height:1.5}}
</style></head><body>{content}</body></html>'''


def poster(name, number):
    tree = ET.parse(DIAGRAMS / (name+'.svg'))
    svg = tree.getroot()
    height = int(svg.attrib['height'])
    for e in svg.iter():
        if e.tag == '{'+NS+'}text' and e.text:
            e.text = e.text.replace(f'{number:02d} / 02', f'{number:02d} / 05')
        # A shared PDF must not contain broken machine-specific file links.
        if e.tag == '{'+NS+'}a' and not e.attrib.get('href','').startswith('https://'):
            e.tag = '{'+NS+'}g'
            e.attrib.clear()
    title = svg.find('{'+NS+'}title').text
    return title, height, document(ET.tostring(svg,encoding='unicode'),height,title)


def official(name, number, title, subtitle, notes, filename, url):
    path = REPORT / 'sources' / filename
    width, height = struct.unpack('>II',path.read_bytes()[16:24])
    image_height = 1640 * height / width
    notes_top = math.ceil(214 + image_height + 40 + 25)
    page_height = notes_top + 205
    image = f'<img src="{path.as_uri()}" width="1640" height="{image_height}" alt="{escape(title)}">'
    content = f'''<main class="page">
<div class="eyebrow">NX5 / MINICPM-V 4.7 · 官方资料</div>
<div class="number">2026.10.21 · {number:02d} / 05</div>
<h1>{escape(title)}</h1><div class="subtitle">{escape(subtitle)}</div>
<div class="figure">{image}</div>
<div class="notes" style="top:{notes_top}px">{''.join('<div>'+escape(n)+'</div>' for n in notes)}</div>
<div class="footer"><a href="{escape(url)}">来源：OpenBMB / MiniCPM-V 官方原图（点击查看）</a><span>官方结果不代表 nx5 当前量化部署的实测成绩</span></div>
</main>'''
    return title,page_height,document(content,page_height,title)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--pdf-python',default=sys.executable,help='Python interpreter with pypdf installed')
    args=ap.parse_args()
    BUILD.mkdir(parents=True,exist_ok=True)
    base='https://github.com/OpenBMB/MiniCPM-V/blob/main/assets/minicpmv4.7/'
    pages=[
        poster('nx5-minicpm-64k-storage',1),
        poster('nx5-minicpm-capabilities',2),
        official('efficiency',3,'官方对比：性能与推理效率',
                 '完整保留官方原图；上半图是本机对应的 35B-A3B 型号，下半图为 1B 型号。',
                 ['横轴是官方评测的推理时间（对数刻度），纵轴是评测得分；不能换算成本机的 token/s。',
                  '图中的 Instruct / Thinking 模式以原图图例为准；nx5 固定方案使用 Instruct、reasoning 关闭。'],
                 'official-performance-efficiency.png',base+'performance_efficiency_frontier.png'),
        official('vision',4,'官方对比：视觉、文档与视频能力',
                 '35B-A3B · Instruct 模式 · 同时展示 4× 与 16× 视觉 token 压缩配置。',
                 ['请在同一行内比较不同模型；不同榜单的分数不能直接当作统一的能力刻度。',
                  '本机尚未验收图片 / 视频输入；这张表用于了解模型本身的能力方向。'],
                 'official-vision-benchmark.png',base+'overall_performance_35ba3b_instruct.png'),
        official('language',5,'官方对比：语言、代码与工具调用',
                 '35B-A3B · Instruct 模式 · 保留官方检查点的完整对比结果。',
                 ['官方评测与 nx5 的混合量化、运行参数不同；尚未做高精度权重与当前部署的全面质量对照。',
                  '本机证据：configs/minicpm-v47-64k.json；docs/08-nx5-64k-fixed-20261021.md。'],
                 'official-language-benchmark.png',base+'overall_performance_35ba3b_language.png')
    ]
    manifest=[]
    with tempfile.TemporaryDirectory(prefix='nx5-pdf-chrome-') as profile:
        chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
        proc=subprocess.Popen([chrome,'--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
                               '--disable-background-networking','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            port_file=Path(profile)/'DevToolsActivePort'
            for _ in range(200):
                if port_file.exists():break
                time.sleep(.05)
            port=int(port_file.read_text().splitlines()[0])
            targets=json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list'))
            target=next(t for t in targets if t['type']=='page')
            with connect(target['webSocketDebuggerUrl'],max_size=100_000_000) as ws:
                seq=0
                def call(method,params=None):
                    nonlocal seq
                    seq+=1
                    ws.send(json.dumps({'id':seq,'method':method,'params':params or {}}))
                    while True:
                        r=json.loads(ws.recv(timeout=60))
                        if r.get('id')==seq:
                            if 'error' in r:raise RuntimeError(r)
                            return r.get('result',{})
                def evaluate(expression):
                    r=call('Runtime.evaluate',{'expression':expression,'returnByValue':True,'awaitPromise':True})
                    if 'exceptionDetails' in r:raise RuntimeError(r)
                    return r['result'].get('value')
                call('Page.enable');call('Runtime.enable')
                for i,(title,height,html) in enumerate(pages,1):
                    source=BUILD/f'page-{i:02d}.html'
                    source.write_text(html)
                    call('Emulation.setDeviceMetricsOverride',{'width':1800,'height':height,'deviceScaleFactor':1,'mobile':False})
                    call('Page.navigate',{'url':source.as_uri()})
                    for _ in range(400):
                        if evaluate('document.readyState')=='complete':break
                        time.sleep(.05)
                    evaluate('Promise.all([document.fonts.ready,...Array.from(document.images,i=>i.decode())]).then(()=>true)')
                    layout=evaluate('''(()=>({images:[...document.images].map(i=>({loaded:i.complete&&i.naturalWidth>0,width:i.naturalWidth,height:i.naturalHeight})),overflow:[...document.querySelectorAll('.subtitle,.figure,.notes,.footer')].map(e=>({cls:e.className,...e.getBoundingClientRect().toJSON()})).filter(r=>r.right>1800.5||r.bottom>document.body.clientHeight+.5)}))()''')
                    assert not layout['overflow'],layout
                    assert all(im['loaded'] for im in layout['images']),layout
                    pdf=call('Page.printToPDF',{'printBackground':True,'displayHeaderFooter':False,'preferCSSPageSize':True,
                         'paperWidth':18.75,'paperHeight':height/96,'marginTop':0,'marginBottom':0,'marginLeft':0,'marginRight':0,
                         'generateTaggedPDF':True,'transferMode':'ReturnAsBase64'})
                    output=BUILD/f'page-{i:02d}.pdf'
                    output.write_bytes(base64.b64decode(pdf['data']))
                    manifest.append({'page':i,'title':title,'height_px':height,'file':str(output),'layout':layout})
                    print(f'Printed page {i}: {title}',flush=True)
                call('Browser.close')
        finally:
            try:proc.wait(timeout=8)
            except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=5)
            time.sleep(.2)
    (BUILD/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    merge=r'''
import json,sys
from pathlib import Path
from pypdf import PdfReader,PdfWriter
root=Path(sys.argv[1]);out=Path(sys.argv[2])
items=json.loads((root/'manifest.json').read_text())
w=PdfWriter();checks=[]
def images(resources):
 result=[]
 for ref in resources.get('/XObject',{}).get_object().values() if '/XObject' in resources else []:
  obj=ref.get_object()
  if obj.get('/Subtype')=='/Image':result.append([int(obj['/Width']),int(obj['/Height'])])
  elif obj.get('/Subtype')=='/Form':result.extend(images(obj.get('/Resources',{})))
 return result
for item in items:
 r=PdfReader(item['file'])
 assert len(r.pages)==1,(item['file'],len(r.pages))
 page=r.pages[0]
 text=page.extract_text()
 assert len(text)>100,(item['page'],len(text))
 w.append(r,outline_item=item['title'])
 image_sizes=images(page['/Resources'])
 checks.append({'page':item['page'],'title':item['title'],'text_characters':len(text),
                'image_count':len(image_sizes),'size_points':[float(page.mediabox.width),float(page.mediabox.height)],
                'image_sizes':image_sizes})
w.add_metadata({'/Title':'nx5 · MiniCPM-V 4.7：64K 部署架构与能力图册',
                '/Author':'nx5 部署记录；官方图表来源 OpenBMB',
                '/Subject':'两张本机矢量图解与三张官方能力、效率对比图；2026-10-21',
                '/Keywords':'MiniCPM-V,Orin NX,nx5,64K,MoE,SSD,DSpark'})
w.page_layout='/OneColumn'
with out.open('wb') as f:w.write(f)
r=PdfReader(out);assert len(r.pages)==5
assert checks[0]['image_count']==0 and checks[1]['image_count']==0
assert all(c['image_count']==1 for c in checks[2:])
assert '8.94' in r.pages[0].extract_text()
assert '94.24' in r.pages[1].extract_text()
assert len(r.outline)==5
summary={'pdf':str(out),'pages':5,'bytes':out.stat().st_size,'bookmarks':len(r.outline),'checks':checks}
(root.parent/'pdf-check.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(summary,ensure_ascii=False,indent=2))
'''
    subprocess.run([args.pdf_python,'-c',merge,str(BUILD),str(OUTPUT)],check=True)


if __name__=='__main__':main()
