#!/usr/bin/env python3
"""kinescope_encode.py — 옮길 파일을 QR 로 바꿔 화면에 띄웁니다.

Kinescope 가 읽는 두 형식을 만듭니다.

  sheet    "QRP001/036:<base64>" 순번 QR 을 시트로 배열한 HTML — 사진으로 찍는 쪽
  stream   LT 파운틴 패킷을 담은 바이너리 QR 을 넘기는 재생기 HTML — 동영상으로 찍는 쪽

둘 다 **파일 하나로 완결된 HTML** 이라 그냥 더블클릭해서 열면 됩니다.
인터넷도, 서버도, 파이썬 실행도 필요 없습니다. 만든 사람만 파이썬이 필요합니다.

    pip install segno
    python3 tools/kinescope_encode.py sheet  내보낼폴더/ -o out/
    python3 tools/kinescope_encode.py stream 내보낼폴더/ -o out/

입력은 파일이든 폴더든 됩니다. zip 이 아니면 알아서 zip 으로 묶습니다.

블록 선택에 쓰는 xorshift32 와 Robust Soliton 누적분포(24비트 양자화)는
`lib/lt.js` 의 디코더와 비트 단위로 맞춰져 있습니다.
"""

import argparse
import base64
import hashlib
import io
import json
import math
import random
import struct
import sys
import zipfile
from pathlib import Path

MAGIC = 0x5153  # 'QS'
HEADER = struct.Struct(">HHHIII")  # magic, K, blockSize, totalLen, sha4, seed
HEADER_LEN = HEADER.size  # 18
SCALE = 1 << 24
MASK = 0xFFFFFFFF


# ---- LT 파운틴 (lib/lt.js 와 동일) -------------------------------------------

def make_rng(seed):
    """lib/lt.js 의 makeRng 와 동일한 xorshift32."""
    x = seed & MASK
    if x == 0:
        x = 0x9E3779B9

    def nxt():
        nonlocal x
        x = (x ^ (x << 13)) & MASK
        x ^= x >> 17
        x = (x ^ (x << 5)) & MASK
        return x

    return nxt


def robust_soliton(K, c=0.05, delta=0.05):
    """24비트 정수로 양자화한 누적분포. lib/lt.js 와 동일한 연산 순서."""
    if K == 1:
        return [SCALE]
    rho = [0.0] * K
    rho[0] = 1 / K
    for i in range(2, K + 1):
        rho[i - 1] = 1 / (i * (i - 1))
    S = c * math.log(K / delta) * math.sqrt(K)
    piv = min(K, max(1, math.floor(K / S + 0.5)))  # JS Math.round
    tau = [0.0] * K
    for i in range(1, piv):
        tau[i - 1] = S / (K * i)
    if S / delta > 1.0:
        tau[piv - 1] = (S * math.log(S / delta)) / K
    Z = 0.0
    for i in range(K):
        Z += rho[i] + tau[i]
    cdf = [0] * K
    acc = 0.0
    for i in range(K):
        acc += (rho[i] + tau[i]) / Z
        cdf[i] = min(SCALE, math.floor(acc * SCALE + 0.5))
    cdf[K - 1] = SCALE
    return cdf


def pick_degree(cdf, u24):
    lo, hi = 0, len(cdf) - 1
    while lo < hi:
        mid = (lo + hi) >> 1
        if u24 <= cdf[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo + 1


def block_indices(seed, K, cdf):
    nxt = make_rng(seed)
    d = pick_degree(cdf, nxt() >> 8)
    if d > K:
        d = K
    chosen = set()
    while len(chosen) < d:
        chosen.add(nxt() % K)
    return sorted(chosen)


def lt_packets(data, block_size, count, rng=None):
    """LT 패킷을 count 개 만들어 바이트열로 내놓습니다.

    seed 는 반드시 32비트 전 구간에서 고르게 뽑아야 합니다. 1, 2, 3 … 처럼
    작은 값을 이어 쓰면 xorshift32 의 첫 출력이 전부 작은 수라서 차수가 1로
    고정되고, 파운틴 코드가 쿠폰 수집 문제로 퇴화합니다.
    """
    rng = rng or random.Random()
    total = len(data)
    K = max(1, math.ceil(total / block_size))
    padded = data + bytes((-total) % block_size)
    blocks = [padded[i * block_size:(i + 1) * block_size] for i in range(K)]
    sha4 = struct.unpack(">I", hashlib.sha256(data).digest()[:4])[0]
    cdf = robust_soliton(K)

    used = set()
    for _ in range(count):
        seed = rng.getrandbits(32)
        while seed in used:
            seed = rng.getrandbits(32)
        used.add(seed)
        acc = bytearray(block_size)
        for i in block_indices(seed, K, cdf):
            b = blocks[i]
            for j in range(block_size):
                acc[j] ^= b[j]
        yield HEADER.pack(MAGIC, K, block_size, total, sha4, seed) + bytes(acc)


def sheet_chunks(data, chunk_size):
    """순번 텍스트 청크: QRP001/036:<base64>"""
    b64 = base64.b64encode(data).decode("ascii")
    parts = [b64[i:i + chunk_size] for i in range(0, len(b64), chunk_size)]
    if len(parts) > 999:
        sys.exit(f"청크가 {len(parts)}개입니다 — 3자리 순번을 넘습니다. --chunk 를 키우세요.")
    return [f"QRP{i + 1:03d}/{len(parts):03d}:{p}" for i, p in enumerate(parts)]


# ---- 입력 --------------------------------------------------------------------

def as_zip(path: Path):
    """파일이든 폴더든 zip 바이트로. 이미 zip 이면 그대로 씁니다."""
    if path.is_file() and zipfile.is_zipfile(path):
        return path.read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if path.is_dir():
            for p in sorted(path.rglob("*")):
                if p.is_file():
                    z.write(p, str(p.relative_to(path)))
        else:
            z.write(path, path.name)
    return buf.getvalue()


# ---- QR 그리기 ---------------------------------------------------------------

def qr_svg(payload, border=4):
    """화면에서 CSS 로 크기를 맞출 수 있는 인라인 SVG."""
    import segno

    q = segno.make(payload, error="l")
    side = q.symbol_size(border=border)[0]
    svg = q.svg_inline(scale=1, border=border, dark="#000", light=None)
    svg = svg.replace(
        "<svg ",
        f'<svg viewBox="0 0 {side} {side}" shape-rendering="crispEdges" ',
        1,
    )
    return svg, q.version


def qr_png(payload, path, scale, border=4):
    import segno

    segno.make(payload, error="l").save(str(path), scale=scale, border=border)


# ---- HTML --------------------------------------------------------------------

CSS = """
*{box-sizing:border-box}
html,body{margin:0;background:#fff;color:#111;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
body{display:flex;flex-direction:column;min-height:100vh;padding:1.2vmin 1.6vmin}
header{flex:0 0 auto;border-bottom:1px solid #bbb;padding-bottom:.6vmin;margin-bottom:1vmin}
h1{font-size:clamp(13px,1.9vmin,20px);margin:0;font-weight:600;letter-spacing:-.01em}
h1 span{font-weight:400;color:#555}
.sha{margin:.2em 0 0;font-size:clamp(9px,1.1vmin,12px);color:#777;word-break:break-all}
main{flex:1 1 auto;display:grid;gap:1.2vmin;
  grid-template-columns:repeat(var(--cols),1fr);
  grid-template-rows:repeat(var(--rows),1fr);min-height:0}
figure{margin:0;display:flex;flex-direction:column;align-items:stretch;
  justify-content:center;min-height:0;min-width:0;overflow:hidden}
/* flex-basis 0 으로 남는 높이를 받고, viewBox 가 있으니 그 안에서 알아서
   정사각형으로 맞춰집니다 */
figure svg{flex:1 1 0;min-height:0;width:100%;height:100%;display:block}
figcaption{flex:0 0 auto;text-align:center;font-size:clamp(10px,1.4vmin,16px);
  color:#333;padding-top:.4vmin;font-variant-numeric:tabular-nums}
nav{flex:0 0 auto;display:flex;gap:1.2em;align-items:center;flex-wrap:wrap;
  border-top:1px solid #bbb;margin-top:1vmin;padding-top:.6vmin;
  font-size:clamp(11px,1.4vmin,15px)}
nav a,nav button{color:#111;text-decoration:none;border:1px solid #bbb;background:#fff;
  padding:.25em .8em;cursor:pointer;font:inherit}
nav a:hover,nav button:hover{border-color:#111}
nav .hint{color:#777;margin-left:auto}
body.bare header,body.bare nav{display:none}
body.bare{padding:.6vmin}
@media print{
  body{padding:0;height:auto}
  nav{display:none}
  main{page-break-after:always}
}
"""

NAV_JS = """
(function(){
  var go=function(u){ if(u) location.href=u; };
  document.addEventListener('keydown',function(e){
    if(e.key==='ArrowRight'||e.key==='PageDown'||e.key===' ') { e.preventDefault(); go(NEXT); }
    else if(e.key==='ArrowLeft'||e.key==='PageUp') { e.preventDefault(); go(PREV); }
    else if(e.key==='f'||e.key==='F') document.body.classList.toggle('bare');
  });
  var b=document.getElementById('bare');
  if(b) b.onclick=function(){ document.body.classList.toggle('bare'); };
})();
"""


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def write_sheets(chunks, out: Path, title, sha, per_sheet, border):
    n_sheets = math.ceil(len(chunks) / per_sheet)
    cols = math.ceil(math.sqrt(per_sheet))
    pages = []
    max_version = 0

    for s in range(n_sheets):
        lo = s * per_sheet
        part = chunks[lo:lo + per_sheet]
        figs = []
        for i, text in enumerate(part, start=lo + 1):
            svg, ver = qr_svg(text, border)
            max_version = max(max_version, ver)
            figs.append(f"<figure>{svg}<figcaption>{i} / {len(chunks)}</figcaption></figure>")

        prev_url = f"sheet_{s:02d}.html" if s > 0 else ""
        next_url = f"sheet_{s + 2:02d}.html" if s + 1 < n_sheets else ""
        nav = []
        if prev_url:
            nav.append(f'<a href="{prev_url}">← 이전</a>')
        if next_url:
            nav.append(f'<a href="{next_url}">다음 →</a>')
        nav.append('<a href="index.html">목록</a>')
        nav.append('<button id="bare">촬영 모드 (F)</button>')
        nav.append('<span class="hint">← → 로 넘기고, F 로 머리글을 숨깁니다</span>')

        html = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} — 시트 {s + 1}/{n_sheets}</title>
<style>{CSS}</style></head>
<body>
<header>
  <h1>{esc(title)} — 시트 {s + 1}/{n_sheets}
      <span>(QR {lo + 1}~{lo + len(part)} / 전체 {len(chunks)})</span></h1>
  <p class="sha">SHA-256(zip) {sha}</p>
</header>
<main style="--cols:{cols};--rows:{math.ceil(len(part) / cols)}">
{chr(10).join(figs)}
</main>
<nav>{" ".join(nav)}</nav>
<script>var PREV={json.dumps(prev_url)},NEXT={json.dumps(next_url)};{NAV_JS}</script>
</body></html>
"""
        p = out / f"sheet_{s + 1:02d}.html"
        p.write_text(html, encoding="utf-8")
        pages.append((p.name, lo + 1, lo + len(part)))

    items = "\n".join(
        f'<li><a href="{name}">시트 {i + 1:02d}</a> — QR {a}~{b}</li>'
        for i, (name, a, b) in enumerate(pages))
    index = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} — QR 시트 {n_sheets}장</title>
<style>{CSS}
main{{display:block}}
ol,ul{{line-height:1.9;font-size:clamp(12px,1.7vmin,16px)}}
p.tip{{max-width:60ch;color:#444;font-size:clamp(11px,1.5vmin,15px);line-height:1.7}}
</style></head>
<body>
<header>
  <h1>{esc(title)} — QR 시트 {n_sheets}장 <span>(QR {len(chunks)}개)</span></h1>
  <p class="sha">SHA-256(zip) {sha}</p>
</header>
<main>
<ul>{items}</ul>
<p class="tip"><b>찍는 요령.</b> 시트를 화면 가득 띄우고(F 키로 머리글을 숨기면 더 넓어집니다)
한 장씩 정면에서 찍으세요. 초점이 맞고 화면 전체가 프레임에 들어오면 됩니다.
<b>순번 방식이라 한 장도 빠지면 안 됩니다</b> — 시트 번호를 세어 가며 찍으세요.
찍은 사진은 전부 한 번에 Kinescope 에 넣으면 됩니다.</p>
<p class="tip">QR 이 잘 안 읽히면 시트당 개수를 줄여 다시 만드세요
(<code>--per-sheet 4</code>). 개수가 적을수록 QR 이 커지고 훨씬 잘 읽힙니다.</p>
</main>
<nav><a href="{pages[0][0]}">첫 시트부터 →</a></nav>
</body></html>
"""
    (out / "index.html").write_text(index, encoding="utf-8")
    return n_sheets, max_version


STREAM_CSS = """
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#fff;color:#111;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
body{display:flex;flex-direction:column;padding:1vmin}
header{flex:0 0 auto;border-bottom:1px solid #bbb;padding-bottom:.5vmin}
h1{font-size:clamp(12px,1.7vmin,18px);margin:0;font-weight:600}
h1 span{font-weight:400;color:#555}
.sha{margin:.2em 0 0;font-size:clamp(9px,1.1vmin,12px);color:#777;word-break:break-all}
#stage{flex:1 1 auto;display:grid;gap:1vmin;min-height:0;padding:1vmin 0;
  grid-template-columns:repeat(var(--gc),1fr);grid-template-rows:repeat(var(--gr),1fr)}
#stage div{display:flex;align-items:center;justify-content:center;min-height:0;min-width:0;overflow:hidden}
#stage svg{flex:1 1 0;width:100%;height:100%;min-height:0;display:block}
nav{flex:0 0 auto;display:flex;gap:1em;align-items:center;flex-wrap:wrap;
  border-top:1px solid #bbb;padding-top:.5vmin;font-size:clamp(11px,1.4vmin,15px)}
button{font:inherit;border:1px solid #bbb;background:#fff;padding:.25em .8em;cursor:pointer;color:#111}
button:hover{border-color:#111}
button[aria-pressed=true]{background:#111;color:#fff;border-color:#111}
.grid{display:flex;gap:.3em;align-items:center}
input[type=range]{width:9em;vertical-align:middle}
.count{font-variant-numeric:tabular-nums;color:#333}
.hint{color:#777;margin-left:auto}
body.bare header,body.bare nav{display:none}
body.bare{padding:.4vmin}
"""

STREAM_JS = """
// 파운틴 패킷은 순서도 출처도 안 따집니다. 그래서 화면을 쪼개 여러 장을 동시에
// 띄우면 카메라가 한 프레임에서 그만큼 한꺼번에 주워 갑니다 — 칸 수만큼 빨라집니다.
// (읽는 쪽은 원래 한 프레임에서 QR 을 여러 개 찾도록 돼 있어 고칠 게 없습니다.)

var stage=document.getElementById('stage'), count=document.getElementById('count'),
    play=document.getElementById('play'), fps=document.getElementById('fps'),
    fpsv=document.getElementById('fpsv'),
    i=0, shown=0, timer=null, cells=[], grid=INIT_GRID, side=0;

// QR 이 제일 커지는 배치를 화면 비율에서 직접 찾습니다. 16:9 에서 4칸을 2×2로
// 놓으면 양옆이 남는데, 같은 크기로 3×2에 여섯 장이 들어갑니다.
function bestLayout(n, w, h){
  var best=[1, n, 0];
  for(var c=1;c<=n;c++){
    var r=Math.ceil(n/c), s=Math.min(w/c, h/r);
    if(s>best[2]) best=[c, r, s];
  }
  return best;
}

function layout(){
  var d=bestLayout(grid, stage.clientWidth||1920, stage.clientHeight||1080);
  side=Math.round(d[2]);
  stage.style.setProperty('--gc', d[0]);
  stage.style.setProperty('--gr', d[1]);
  stage.innerHTML='';
  cells=[];
  for(var k=0;k<grid;k++){
    var c=document.createElement('div');
    stage.appendChild(c); cells.push(c);
  }
  var bs=document.querySelectorAll('.grid button');
  for(var n=0;n<bs.length;n++) bs[n].setAttribute('aria-pressed', +bs[n].dataset.g===grid);
  draw();
}
function draw(){
  for(var k=0;k<grid;k++) cells[k].innerHTML=FRAMES[(i+k)%FRAMES.length];
  var loops=Math.floor(shown/FRAMES.length);
  // 1080p 화면을 1080p 로 찍었을 때 470px 짜리는 읽혔고 330px 짜리는 못 읽었습니다.
  count.textContent=(i+1)+' / '+FRAMES.length+'  ·  '+loops+'바퀴  ·  초당 '
                    +(grid*(+fps.value))+'장  ·  한 칸 '+side+'px'
                    +(side<400?'  ⚠ 작습니다 — 카메라가 못 읽을 수 있어요':'');
}
function step(){ i=(i+grid)%FRAMES.length; shown+=grid; draw(); }
function back(){ i=(i-grid+FRAMES.length*2)%FRAMES.length; draw(); }
function start(){ if(timer) return; timer=setInterval(step, 1000/(+fps.value)); play.textContent='멈춤'; }
function stop(){ clearInterval(timer); timer=null; play.textContent='재생'; }
function setGrid(g){ if(!(g>=1&&g<=64)) return; grid=g; layout(); }

play.onclick=function(){ timer?stop():start(); };
fps.oninput=function(){ fpsv.textContent=fps.value+' fps'; draw(); if(timer){ stop(); start(); } };
document.getElementById('bare').onclick=function(){ document.body.classList.toggle('bare'); };
var gb=document.querySelectorAll('.grid button');
for(var n=0;n<gb.length;n++) gb[n].onclick=function(){ setGrid(+this.dataset.g); };
document.addEventListener('keydown',function(e){
  if(e.key===' '){ e.preventDefault(); timer?stop():start(); }
  else if(e.key==='ArrowRight'){ e.preventDefault(); stop(); step(); }
  else if(e.key==='ArrowLeft'){ e.preventDefault(); stop(); back(); }
  else if(e.key==='f'||e.key==='F'){ document.body.classList.toggle('bare'); }
  else if(e.key>='1'&&e.key<='9'){ setGrid(+e.key); }
});
addEventListener('resize', layout);
layout(); start();
"""


def write_stream_player(frames, out: Path, title, sha, fps, K, grid=1):
    buttons = "".join(f'<button data-g="{g}">{g}</button>' for g in (1, 2, 4, 6, 9, 12))
    html = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} — QR 스트림</title>
<style>{STREAM_CSS}</style></head>
<body>
<header>
  <h1>{esc(title)} — QR 스트림 <span>(블록 {K}개 · 패킷 {len(frames)}개)</span></h1>
  <p class="sha">SHA-256(zip) {sha}</p>
</header>
<div id="stage"></div>
<nav>
  <button id="play">재생</button>
  <label>속도 <input type="range" id="fps" min="2" max="20" step="1" value="{fps}">
    <span id="fpsv">{fps} fps</span></label>
  <span class="grid">칸 {buttons}</span>
  <button id="bare">촬영 모드 (F)</button>
  <span class="count" id="count"></span>
  <span class="hint">칸을 늘리면 그만큼 빨라집니다 — 카메라에 또렷하게 잡히는 선까지</span>
</nav>
<script>var FRAMES={json.dumps(frames)},INIT_GRID={grid};{STREAM_JS}</script>
</body></html>
"""
    p = out / "stream.html"
    p.write_text(html, encoding="utf-8")
    return p


# ---- CLI ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Kinescope 용 QR 생성기 — 파일/폴더를 QR 시트나 스트림으로 만듭니다",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="예)  python3 tools/kinescope_encode.py sheet ./src -o out/ --per-sheet 4")
    ap.add_argument("mode", choices=["sheet", "stream"],
                    help="sheet=사진으로 찍는 순번 시트, stream=동영상으로 찍는 파운틴 재생기")
    ap.add_argument("input", type=Path, help="옮길 파일이나 폴더 (zip 이 아니면 알아서 묶습니다)")
    ap.add_argument("-o", "--out", type=Path, default=Path("out"))
    ap.add_argument("--chunk", type=int, default=1000,
                    help="sheet: QR 하나에 담을 base64 길이 (기본 1000)")
    ap.add_argument("--per-sheet", type=int, default=6,
                    help="sheet: 한 화면에 넣을 QR 개수 (기본 6, 적을수록 잘 읽힙니다)")
    ap.add_argument("--block", type=int, default=512, help="stream: 블록 크기(바이트)")
    ap.add_argument("--overhead", type=float, default=2.2,
                    help="stream: 블록 수 대비 패킷 배수 (기본 2.2)")
    ap.add_argument("--fps", type=int, default=10, help="stream: 재생기 기본 속도")
    ap.add_argument("--grid", type=int, default=1, choices=[1, 2, 4, 6, 9, 12],
                    help="stream: 한 화면에 동시에 띄울 QR 개수 (재생기에서도 바꿀 수 있습니다)")
    ap.add_argument("--seed", type=int, default=None, help="stream: 재현용 난수 시드")
    ap.add_argument("--border", type=int, default=4, help="QR 둘레 여백 모듈 수")
    ap.add_argument("--png", action="store_true", help="HTML 과 함께 PNG 도 저장")
    ap.add_argument("--scale", type=int, default=6, help="--png 일 때 한 모듈의 픽셀 크기")
    args = ap.parse_args()

    if not args.input.exists():
        sys.exit(f"입력을 찾을 수 없습니다: {args.input}")

    data = as_zip(args.input)
    sha = hashlib.sha256(data).hexdigest()
    title = args.input.name
    args.out.mkdir(parents=True, exist_ok=True)

    print(f"{title} → zip {len(data):,} 바이트")
    print(f"SHA-256(zip)  {sha}")

    if args.mode == "sheet":
        chunks = sheet_chunks(data, args.chunk)
        n_sheets, ver = write_sheets(chunks, args.out, title, sha,
                                     max(1, args.per_sheet), args.border)
        if args.png:
            for i, text in enumerate(chunks, 1):
                qr_png(text, args.out / f"qr_{i:03d}.png", args.scale, args.border)
        print(f"QR {len(chunks)}개 · 시트 {n_sheets}장 · QR 버전 {ver}")
        print(f"→ {args.out / 'index.html'} 를 브라우저로 여세요")
        if ver >= 25 and args.per_sheet > 6:
            print("  ! QR 이 촘촘하고 한 시트에 여러 개입니다. 사진이 잘 안 읽히면 "
                  "--per-sheet 4 로 다시 만드세요.")
    else:
        K = max(1, math.ceil(len(data) / args.block))
        count = max(K + 4, math.ceil(K * args.overhead))
        rng = random.Random(args.seed) if args.seed is not None else random.Random()
        packets = list(lt_packets(data, args.block, count, rng))
        frames, ver = [], 0
        for pkt in packets:
            svg, v = qr_svg(pkt, args.border)
            frames.append(svg)
            ver = max(ver, v)
        player = write_stream_player(frames, args.out, title, sha, args.fps, K, args.grid)
        if args.png:
            for i, pkt in enumerate(packets):
                qr_png(pkt, args.out / f"frame_{i:05d}.png", args.scale, args.border)
        size_mb = player.stat().st_size / 1e6
        print(f"블록 {K}개 · 패킷 {len(packets)}개 · QR 버전 {ver}")
        print(f"→ {player} 를 브라우저로 열고 재생하면서 찍으세요 ({size_mb:.1f}MB)")
        secs = len(packets) / max(1, args.fps)
        print(f"   한 바퀴 {secs:.0f}초 (1칸) · {secs / 6:.0f}초 (6칸) · {secs / 12:.0f}초 (12칸)")
        if args.png:
            print(f"ffmpeg -start_number 0 -framerate {args.fps} "
                  f"-i {args.out}/frame_%05d.png -pix_fmt yuv420p {args.out}/stream.mp4")


if __name__ == "__main__":
    main()
