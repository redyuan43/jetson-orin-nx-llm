#!/usr/bin/env python3
"""Build editable, self-contained SVG posters from the frozen nx5 evidence."""
from html import escape
from pathlib import Path
import json
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs/diagrams'
PROFILE = json.loads((ROOT / 'configs/minicpm-v47-64k.json').read_text())
P = PROFILE['parameters']
BG = '#F3F6F8'
INK = '#173149'
MUTED = '#536B7D'
LINE = '#DCE5EB'
TEAL = '#147D76'
TEAL_BG = '#E8F5F1'
AMBER = '#A86718'
AMBER_BG = '#FFF4E3'
BLUE = '#346FAA'
BLUE_BG = '#EBF3FC'
PURPLE = '#6F55AA'
PURPLE_BG = '#F1EDF9'
RED = '#AF5557'
FONT = "'PingFang SC','Noto Sans CJK SC','Microsoft YaHei',Arial,sans-serif"
GUIDE = 'https://github.com/OpenBMB/MiniCPM-V/blob/main/docs/minicpm_v4dot7_zh.md'
CONFIG = 'https://huggingface.co/openbmb/MiniCPM-V-4.7-35B-A3B/blob/main/config.json'
VISION = 'https://github.com/OpenBMB/MiniCPM-V/blob/main/assets/minicpmv4.7/overall_performance_35ba3b_instruct.png'
LANG = 'https://github.com/OpenBMB/MiniCPM-V/blob/main/assets/minicpmv4.7/overall_performance_35ba3b_language.png'


class SVG:
    def __init__(self, title, desc, page, height=1700):
        self.height = height
        self.a = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1800" height="{height}" viewBox="0 0 1800 {height}" role="img" aria-labelledby="title desc">',
                  f'<title id="title">{escape(title)}</title><desc id="desc">{escape(desc)}</desc>',
                  f'<metadata>{escape(json.dumps({"date":"2026-10-21","profile":PROFILE["name"],"sources":[GUIDE,CONFIG,VISION,LANG,"../08-nx5-64k-fixed-20261021.md"]},ensure_ascii=False))}</metadata>',
                  f'<style>text{{font-family:{FONT};font-variant-numeric:tabular-nums}}a:hover text{{text-decoration:underline}}</style>',
                  '<defs>']
        for color, name in [(TEAL,'teal'), (AMBER,'amber'), (BLUE,'blue'), (PURPLE,'purple'), (MUTED,'muted')]:
            self.a.append(f'<marker id="arrow-{name}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{color}"/></marker>')
        self.a.append('</defs>')
        self.rect(0,0,1800,height,BG,r=0)
        self.rect(60,56,42,7,TEAL,r=3)
        self.text(118,68,'NX5  /  MINICPM-V 4.7',18,TEAL,700)
        self.text(1740,68,f'2026.10.21   ·   {page} / 02',18,MUTED,anchor='end')
        self.text(60,132,title,46,INK,700)
        self.text(60,178,desc,22,MUTED,maxw=1680)

    def rect(self,x,y,w,h,fill='white',stroke=None,r=18,sw=1.5,dash=None):
        self.a.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}"'+(f' stroke="{stroke}" stroke-width="{sw}"' if stroke else '')+(f' stroke-dasharray="{dash}"' if dash else '')+'/>')

    def text(self,x,y,s,size=22,color=INK,weight=400,anchor='start',maxw=None):
        self.a.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" text-anchor="{anchor}"'+(f' data-max-width="{maxw}"' if maxw else '')+f'>{escape(str(s))}</text>')

    def lines(self,x,y,lines,size=21,color=MUTED,dy=31,maxw=None,weight=400):
        for i,s in enumerate(lines): self.text(x,y+i*dy,s,size,color,weight,maxw=maxw)

    def path(self,d,color=MUTED,arrow=None,dash=None,sw=2.5):
        self.a.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round"'+(f' marker-end="url(#arrow-{arrow})"' if arrow else '')+(f' stroke-dasharray="{dash}"' if dash else '')+'/>')

    def pill(self,x,y,w,label,color=TEAL,fill=TEAL_BG,size=18):
        self.rect(x,y,w,32,fill,r=16)
        self.text(x+w/2,y+23,label,size,color,600,anchor='middle',maxw=w-16)

    def stat(self,x,w,value,label,color=INK,sub=None):
        self.rect(x,216,w,100,'white',LINE,r=16)
        self.text(x+24,260,value,32,color,700,maxw=w-48)
        self.text(x+24,292,label,19,MUTED,maxw=w-48)

    def section(self,x,y,num,title,color=TEAL):
        self.rect(x,y-25,32,32,color,r=9)
        self.text(x+16,y-2,num,17,'white',700,anchor='middle')
        self.text(x+46,y,title,26,INK,700)

    def link(self,x,y,label,url,size=18,color=BLUE,maxw=None):
        self.a.append(f'<a href="{escape(url,quote=True)}" target="_blank">')
        self.text(x,y,label,size,color,500,maxw=maxw)
        self.a.append('</a>')

    def save(self,name):
        self.a.append('</svg>')
        path=OUT/name
        path.write_text('\n'.join(self.a)+'\n')
        ET.parse(path)
        print(path)


def storage():
    s=SVG('nx5 的模型，存在哪里？','当前固定方案：SSD 保存全量专家，统一内存缓存一部分；CPU 负责调度与读盘，GPU 直接读取缓存参与计算。','01')
    s.stat(60,405,'Orin NX 16GB Super','ARM CPU + GPU · 共用一块 DRAM',TEAL)
    s.stat(485,405,'64K 上下文','65,536 token · 输入、历史、输出共用',BLUE)
    s.stat(910,405,'176 / 256','每层专家缓存容量 · 不代表命中率',TEAL)
    s.stat(1335,405,'DSpark · 7 token','最多预测 7 个，交给主模型验证',PURPLE)

    # SSD and a single physical DRAM boundary.
    s.rect(60,346,390,794,'white',LINE,r=24)
    s.section(84,393,'1','NVMe SSD',AMBER)
    s.text(84,428,'模型文件长期保存在这里',20,MUTED)
    s.rect(84,456,342,214,AMBER_BG,r=16)
    s.text(106,496,'全量路由专家',25,AMBER,700)
    s.text(106,547,'12.89 GiB',40,INK,700)
    s.lines(106,587,['experts/ · Q3_K','40 层 × 256 = 10,240 个专家','gate / up / down 按专家打包'],19,dy=28,maxw=300)
    s.rect(84,690,342,110,BLUE_BG,r=16)
    s.text(106,725,'主模型非路由专家部分',22,BLUE,650)
    s.text(106,757,'stripped.gguf · 约 1.55 GiB',20,INK,maxw=300)
    s.text(106,784,'启动时加载 / 映射',18,MUTED)
    s.rect(84,818,342,110,PURPLE_BG,r=16)
    s.text(106,854,'DSpark 草稿权重',22,PURPLE,650)
    s.text(106,886,'dspark-Q4_K_M.gguf',20,INK)
    s.text(106,913,'启动时加载 · 5 层草稿模型',18,MUTED)
    s.rect(84,946,342,164,'#F4F6F8',LINE,r=16,dash='5 5')
    s.text(106,981,'其他模型资源',22,INK,650)
    s.lines(106,1014,['预热表：1,203 个常用专家','视觉编码器 / mmproj','本轮文字推理未加载视觉部分'],19,dy=29,maxw=302)

    s.rect(620,346,1120,794,'#F9FCFB','#A9CFC6',r=24,sw=2)
    s.section(648,393,'2','CPU / GPU 共享的同一块物理内存')
    s.text(1712,428,'系统可见 15,597.9 MiB ≈ 15.23 GiB',20,MUTED,anchor='end')
    s.rect(648,451,510,87,'#EAF0F5',r=14)
    s.text(672,486,'CPU · 调度、路由管理、I/O',23,INK,650)
    s.text(672,518,'8 个读盘线程向共享专家缓存写入数据',19,MUTED,maxw=462)
    s.rect(1178,451,534,87,PURPLE_BG,r=14)
    s.text(1202,486,'GPU · 专家矩阵运算、主模型验证',23,PURPLE,650,maxw=486)
    s.text(1202,518,'直接访问映射内存，无需再复制一整份专家缓存',19,MUTED,maxw=486)
    s.path('M 844 538 V 561',TEAL,'teal')
    s.path('M 1442 562 V 542',PURPLE,'purple')

    s.rect(648,565,1064,197,TEAL_BG,'#A9CFC6',r=18)
    s.text(672,604,'动态专家缓存',27,TEAL,700)
    s.text(672,657,'8.94 GiB',42,TEAL,750)
    s.text(672,695,'9,157.5 MiB · 7,104 个槽位',20,INK)
    s.text(672,735,'CUDA 映射内存：CPU 写入，GPU 直读',20,TEAL,600)
    s.text(1080,604,'每层窗口：176 × 40 = 7,040 槽',21,INK,650,maxw=606)
    s.rect(1080,625,606,24,'#D9E3E1',r=6)
    s.rect(1080,625,606*P['window']/256,24,TEAL,r=6)
    s.text(1080,678,'176 / 256 = 68.75% 的候选容量',20,TEAL,maxw=606)
    s.text(1080,709,'另有 64 槽 prefill bank，所有层共用',20,INK,maxw=606)
    s.text(1080,739,'未缓存的专家仍可按需读入，路由范围保持完整',19,MUTED,maxw=606)

    s.path('M 430 578 H 638',AMBER,'amber',sw=3)
    s.text(534,535,'缓存未命中',19,AMBER,600,anchor='middle')
    s.text(534,561,'按需读取',19,AMBER,600,anchor='middle')
    s.text(534,611,'命中时复用',18,TEAL,600,anchor='middle')
    s.text(534,641,'保留页缓存',17,MUTED,anchor='middle')
    s.lines(478,966,['专家缓冲比','全量常驻少占'],18,MUTED,dy=28,maxw=126)
    s.text(478,1035,'≈3.95',29,TEAL,700)
    s.text(478,1070,'GiB',21,TEAL,650)

    blocks=[
      (648,340,'主模型其余权重','1,575.24 MiB',['含嵌入、注意力、共享专家等','CPU 映射 272.62 + CUDA 1302.62'],BLUE,BLUE_BG),
      (1008,340,'DSpark 草稿权重','215.59 MiB',['5 层 · Q4_K_M','CPU 映射 34.08 + CUDA 181.51'],PURPLE,PURPLE_BG),
      (1368,344,'GDN 循环状态','502.5 MiB',['30 个线性注意力层相关状态','当前 draft7 路径分配值'],TEAL,TEAL_BG),
    ]
    for x,w,t,n,ls,c,bg in blocks:
        s.rect(x,780,w,143,bg,r=14)
        s.text(x+20,813,t,21,c,650)
        s.text(x+20,852,n,29,INK,700)
        s.lines(x+20,882,ls,17,MUTED,dy=25,maxw=w-40)
    s.rect(648,941,700,142,BLUE_BG,r=14)
    s.text(670,976,'64K KV 缓存 · 合计 1,320 MiB ≈ 1.29 GiB',24,BLUE,650,maxw=656)
    s.rect(670,994,336,23,BLUE,r=6)
    s.rect(1012,994,314,23,PURPLE,r=6)
    s.text(670,1054,'主模型 q8_0：680 MiB',21,BLUE)
    s.text(1012,1054,'草稿 f16：640 MiB',21,PURPLE)
    s.rect(1368,941,344,142,'#EEF2F5',r=14)
    s.text(1388,976,'其他动态占用',22,INK,650)
    s.lines(1388,1010,['计算缓冲、CUDA、系统、页缓存','检查点 ≤ 2；提示词缓存 ≤ 256 MiB'],17,MUTED,dy=28,maxw=304)
    s.text(648,1117,'各内存区均来自同一池；框图面积不表示容量比例，文件映射和动态占用不可直接当作 RSS 相加。',18,MUTED,maxw=1064)

    s.section(60,1192,'3','一轮生成怎样经过这些部件')
    nodes=[
      (60,348,'文字提示词','固定入口 → llama-cli',BLUE,BLUE_BG),
      (468,348,'DSpark 先预测','最多 7 token，等待验证',PURPLE,PURPLE_BG),
      (876,408,'主模型并行验证','每层选 8 专家 → 缓存 / 读盘 → GPU',TEAL,TEAL_BG),
      (1344,396,'接受正确前缀并输出','拒绝部分回退；同步更新 KV / 状态',BLUE,BLUE_BG),
    ]
    for x,w,t,b,c,bg in nodes:
        s.rect(x,1218,w,103,bg,r=16)
        s.text(x+22,1258,t,25,c,700,maxw=w-44)
        s.text(x+22,1296,b,19,MUTED,maxw=w-44)
    for x1,x2 in [(408,468),(816,876),(1284,1344)]: s.path(f'M {x1+8} 1268 H {x2-10}',MUTED,'muted')

    s.rect(60,1345,1680,90,'white',LINE,r=16)
    s.text(84,1379,'批处理',18,TEAL,700)
    s.text(84,1414,'batch 2048 · ubatch 8 · window rows 8',21,INK)
    s.text(659,1379,'计算与采样',18,TEAL,700)
    s.text(659,1414,'CUDA Graph 开 · Instruct · reasoning 关',21,INK)
    s.text(1243,1379,'默认采样参数',18,TEAL,700)
    s.text(1243,1414,'T 0.7 · top_p 0.95 · top_k 100',21,INK)

    metrics=[(60,540,'15.54 token/s','中文鹈鹕：38 输入 / 2,315 输出',TEAL),
             (630,540,'14.22 token/s','8K 检索：8,218 输入 / 108 输出',BLUE),
             (1200,540,'≈1.10 GiB','验收最低可用内存；保护阈值 768 MiB',PURPLE)]
    for x,w,v,l,c in metrics:
        s.rect(x,1457,w,110,'white',LINE,r=16)
        s.text(x+24,1504,v,33,c,700)
        s.text(x+24,1541,l,20,MUTED,maxw=w-48)
    s.text(60,1607,'验收边界：已验证 64K 容量与约 8K 实际输入；尚未填满 64K。速率是特定任务实测，108-token 短回答不代表持续吞吐。',19,MUTED,maxw=1680)
    s.link(60,1650,'配置：minicpm-v47-64k.json','../../configs/minicpm-v47-64k.json',18)
    s.link(480,1650,'来源：64K 验收报告与启动日志','../08-nx5-64k-fixed-20261021.md',18)
    s.text(1740,1650,'容量统一使用 MiB / GiB（二进制） · 当前为单请求 CLI',18,MUTED,anchor='end')
    s.save('nx5-minicpm-64k-storage.svg')


def capabilities():
    s=SVG('它擅长什么，哪里要留余地？','模型能力地图：以官方 Instruct 对比和 nx5 本轮实测为依据；“相对强弱”仅针对所列任务与对比模型。','02',1760)
    s.stat(60,405,'35B-A3B · MoE','总量约 35.5B · 每 token 激活约 3B',TEAL)
    s.stat(485,405,'40 层 · 混合注意力','30 层 GDN + 10 层全注意力',BLUE)
    s.stat(910,405,'256 选 8 + 共享专家','每层独立路由 · 不裁剪专家候选集',TEAL)
    s.stat(1335,405,'当前部署 64K','官方配置上限 256K ≠ 本机已验证容量',PURPLE)

    s.rect(60,342,1680,322,'white',LINE,r=22)
    s.section(84,388,'1','模型架构：视觉编码 + 稀疏语言模型')
    s.rect(84,441,248,136,BLUE_BG,r=16)
    s.text(108,480,'输入',25,BLUE,700)
    s.text(108,516,'文字',23,INK,650)
    s.text(108,550,'单图 / 多图 / 视频帧',19,MUTED)
    s.rect(392,441,288,136,PURPLE_BG,r=16)
    s.text(414,480,'视觉编码器',25,PURPLE,700)
    s.text(414,516,'27 层 · SigLIP2 基础',20,INK,maxw=244)
    s.text(414,550,'视觉 token 压缩 4× / 16×',19,MUTED,maxw=244)
    s.rect(740,441,574,136,TEAL_BG,r=16)
    s.text(764,480,'Qwen3.5 MoE 文本架构',25,TEAL,700)
    for i,(label,c) in enumerate([('GDN',TEAL),('GDN',TEAL),('GDN',TEAL),('全注意力',BLUE)]):
        s.rect(764+i*104,496,94,34,c,r=8)
        s.text(811+i*104,520,label,17,'white',650,anchor='middle')
    s.text(1190,520,'× 10',24,INK,700)
    s.text(764,556,'每层 MoE：256 个路由专家中选 8 个，另加共享专家',19,MUTED,maxw=526)
    s.rect(1374,441,342,136,BLUE_BG,r=16)
    s.text(1398,480,'输出文字 / JSON / 代码',25,BLUE,700,maxw=294)
    s.text(1398,516,'看图问答、文字回答',20,INK)
    s.text(1398,550,'HTML / SVG 是代码生成任务',19,MUTED,maxw=294)
    for x1,x2 in [(332,392),(680,740),(1314,1374)]: s.path(f'M {x1+8} 543 H {x2-10}',MUTED,'muted')
    s.path('M 208 441 V 416 H 858 V 434',BLUE,'blue',sw=2)
    s.rect(420,398,256,30,'white',r=0)
    s.text(548,420,'文字直接进入语言模型',18,BLUE,anchor='middle')
    s.text(84,614,'GDN 用循环状态保存信息；全注意力层使用 KV。MoE 减少单步计算量，但完整专家权重仍需保存。',21,MUTED,maxw=1632)
    s.text(84,645,'nx5 当前固定入口仅验收文字输入；视觉编码器、图片 / 视频输入尚未纳入本轮部署验收。',19,PURPLE,maxw=1632)

    s.section(60,714,'2','能力强弱：看任务证据，不把不同榜单分数混成一个总分')
    cols=[(60,TEAL,TEAL_BG,'相对强项','官方模型成绩 [1][2]'),
          (630,AMBER,AMBER_BG,'相对短板','官方模型成绩 [1][2]'),
          (1200,BLUE,BLUE_BG,'nx5 的实际表现','当前量化与运行配置 [4]')]
    for x,c,bg,t,sub in cols:
        s.rect(x,740,540,505,'white',LINE,r=20)
        s.rect(x,740,540,90,bg,r=20)
        s.text(x+24,780,t,27,c,700)
        s.text(x+24,810,sub,18,MUTED)

    # Each official comparison is within a single benchmark and the same published table.
    def entry(x,y,title,body,evidence,color=TEAL):
        s.text(x+24,y,title,23,color,700,maxw=492)
        s.lines(x+24,y+33,body,20,INK,dy=29,maxw=492)
        s.text(x+24,y+(95 if len(body)>1 else 64),evidence,18,MUTED,maxw=492)
    entry(60,868,'文档、OCR 与图表理解',
          ['OmniDocBench 1.6：94.24','官方同表其他模型最高：82.14'],
          '读文档是优势方向；不代表每个 OCR 子项都领先。')
    entry(60,1005,'通用视觉理解',
          ['MMBench：91.72','官方同表其他模型最高：89.63'],
          '适合看图问答；精细定位仍要单独评估。')
    entry(60,1142,'中文知识与视频理解',
          ['CMMLU：90.21 · Video-MME：76.40'],
          '这两项在官方所列对比中领先。')

    entry(630,868,'代码生成',
          ['LiveCodeBench-v6：56.57','同表 Gemma-4 31B：71.24'],
          '有代码能力，但复杂实现与调试不是相对强项。',AMBER)
    entry(630,1005,'GUI 元素定位',
          ['ScreenSpot：86.99','同表 Qwen3.8 27B：93.31'],
          '能理解截图，不等于总能准确点击目标。',AMBER)
    entry(630,1142,'复杂指令遵循',
          ['IFBench：47.67 · Gemma-4 31B：54.33'],
          '约束较多时，输出格式和细节需要核验。',AMBER)

    entry(1200,868,'有原文的字段检索：本次通过',
          ['8,218-token 输入，六个核验码 6/6 正确','JSON 可解析，未漏项或添加字段'],
          '属于一次检索样本，不能推成全面长上下文成绩。',BLUE)
    entry(1200,1005,'SVG 动画：本次未通过',
          ['能生成完整 HTML，但车轮旋转中心错误','关闭 DSpark 后仍出现定位问题'],
          '这次 8K → 64K 的 HTML 内容逐字节相同。',RED)
    entry(1200,1142,'多模态与工具：尚未验收',
          ['图片 / 视频问答、工具调用、HTTP 服务'],
          '官方支持的能力不等于本机固定入口已提供。',BLUE)

    s.text(60,1280,'官方分数口径：35B-A3B、Instruct；视觉取 4× 压缩、Video-MME 无字幕。分数仅可在同一榜单内对比。',19,MUTED,maxw=1680)
    s.text(60,1310,'以上“官方成绩”来自官方检查点对比表，并非 nx5 混合 Q3_K / GGUF 量化版本的实测分数。',19,MUTED,maxw=1680)

    s.section(60,1361,'3','在这台 nx5 上怎么用')
    advice=[(60,'现在可优先使用','带原文的字段抽取 / 检索','本轮已有实际样本通过',TEAL,TEAL_BG),
            (485,'值得下一步验收','文档 OCR / 截图 / 视频问答','模型优势方向，需接入视觉链路',BLUE,BLUE_BG),
            (910,'生成后必须检查','复杂代码 / SVG / 动画','实际运行、看图检查、再修正',AMBER,AMBER_BG),
            (1335,'当前输出范围','文字、结构化文本与代码','当前链路不提供语音或图像生成',PURPLE,PURPLE_BG)]
    for x,t,b,n,c,bg in advice:
        s.rect(x,1385,405,124,bg,r=16)
        s.text(x+22,1419,t,20,c,700)
        s.text(x+22,1456,b,22,INK,650,maxw=361)
        s.text(x+22,1487,n,18,MUTED,maxw=361)
    s.rect(60,1530,1680,86,'white',LINE,r=14)
    s.text(84,1566,'质量边界',21,INK,700)
    s.text(220,1566,'未做原始高精度权重与当前量化的全面质量对照，不能量化能力损失，也不能宣称完全无损。',20,MUTED,maxw=1490)
    s.text(220,1596,'64K 只通过容量与约 8K 实际输入验证；完整 64K 历史、多轮常驻质量尚未验证。',20,MUTED,maxw=1490)

    s.link(60,1660,'[1] 官方视觉评测表',VISION,18)
    s.link(410,1660,'[2] 官方语言评测表',LANG,18)
    s.link(760,1660,'[3] 官方模型配置',CONFIG,18)
    s.link(1100,1660,'[4] nx5 本轮验收报告','../08-nx5-64k-fixed-20261021.md',18)
    s.link(60,1705,'模型能力与视觉压缩说明：OpenBMB 官方指南',GUIDE,18)
    s.text(1740,1705,'强弱判断范围：官方所列对比 + 本轮本机证据',18,MUTED,anchor='end')
    s.save('nx5-minicpm-capabilities.svg')


if __name__ == '__main__':
    OUT.mkdir(parents=True,exist_ok=True)
    storage()
    capabilities()
