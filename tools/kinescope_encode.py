#!/usr/bin/env python3
"""kinescope_encode.py — 옮길 파일을 QR 로 바꿔 화면에 띄웁니다.

이 파일 하나만 있으면 됩니다. 설치할 것도, 인터넷도 필요 없습니다.

    옮기고 싶은 파일들이 있는 폴더에 이 파일을 넣고

        python kinescope_encode.py

    끝입니다. 같은 폴더에 _qr 폴더가 생기고 브라우저가 열립니다.

만들어지는 것 두 가지입니다.

    _qr/stream.html   QR 을 넘겨 보여주는 재생기 — 동영상으로 찍는 쪽
    _qr/index.html    QR 을 여러 개 늘어놓은 시트 — 사진으로 찍는 쪽

찍은 영상이나 사진을 Kinescope 웹페이지에 넣으면 원본 파일이 나옵니다.

세부 조정이 필요하면 옵션을 줄 수 있습니다 (`--help` 참고). 예를 들어
사진이 잘 안 읽히면 시트당 QR 개수를 줄이세요.

    python kinescope_encode.py sheet ./내보낼폴더 -o out --per-sheet 4

QR 인코더는 파이썬 표준 라이브러리만으로 직접 구현했습니다. 오류정정 L,
바이트 모드, 버전 1~40 을 지원하며 zxing 으로 버전마다 디코드를 확인했습니다.
LT 파운틴의 블록 선택(xorshift32 + Robust Soliton)은 `lib/lt.js` 의 디코더와
비트 단위로 맞춰져 있습니다.
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
import webbrowser
import zipfile
import zlib
from pathlib import Path

try:                                       # 윈도우 콘솔에서 한글·기호가 깨지지 않게
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ==== 1. 무엇을 옮길지 =========================================================

SKIP_NAMES = {"__pycache__", "_qr", "desktop.ini", "Thumbs.db", ".DS_Store"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def collect(folder: Path, script: Path):
    """폴더 안에서 옮길 파일을 고릅니다. 이 스크립트와 결과물은 뺍니다."""
    files = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        if p.resolve() == script.resolve():
            continue
        if any(part in SKIP_NAMES or part.startswith(".") for part in p.relative_to(folder).parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        files.append(p)
    return files


def zip_files(files, root: Path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            z.write(p, str(p.relative_to(root)).replace("\\", "/"))
    return buf.getvalue()


def as_zip(path: Path):
    """파일이든 폴더든 zip 바이트로. 이미 zip 이면 그대로 씁니다."""
    if path.is_file() and zipfile.is_zipfile(path):
        return path.read_bytes()
    if path.is_dir():
        return zip_files(collect(path, Path(__file__)), path)
    return zip_files([path], path.parent)


# ==== 2. 조각내기 ==============================================================

MAGIC = 0x5153  # 'QS'
HEADER = struct.Struct(">HHHIII")  # magic, K, blockSize, totalLen, sha4, seed
MASK32 = 0xFFFFFFFF
SCALE = 1 << 24


def make_rng(seed):
    """lib/lt.js 의 makeRng 와 동일한 xorshift32."""
    x = seed & MASK32
    if x == 0:
        x = 0x9E3779B9

    def nxt():
        nonlocal x
        x = (x ^ (x << 13)) & MASK32
        x ^= x >> 17
        x = (x ^ (x << 5)) & MASK32
        return x

    return nxt


def robust_soliton(K, c=0.05, delta=0.05):
    """24비트 정수로 양자화한 누적분포. lib/lt.js 와 같은 연산 순서."""
    if K == 1:
        return [SCALE]
    rho = [0.0] * K
    rho[0] = 1 / K
    for i in range(2, K + 1):
        rho[i - 1] = 1 / (i * (i - 1))
    S = c * math.log(K / delta) * math.sqrt(K)
    piv = min(K, max(1, math.floor(K / S + 0.5)))     # JS Math.round
    tau = [0.0] * K
    for i in range(1, piv):
        tau[i - 1] = S / (K * i)
    if S / delta > 1.0:
        tau[piv - 1] = (S * math.log(S / delta)) / K
    Z = sum(rho[i] + tau[i] for i in range(K))
    cdf, acc = [0] * K, 0.0
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
    d = min(pick_degree(cdf, nxt() >> 8), K)
    chosen = set()
    while len(chosen) < d:
        chosen.add(nxt() % K)
    return sorted(chosen)


def lt_packets(data, block_size, count, rng):
    """LT 파운틴 패킷. 순서와 무관하게 충분히 모이기만 하면 복원됩니다.

    seed 는 반드시 32비트 전 구간에서 고르게 뽑아야 합니다. 1, 2, 3 … 처럼
    작은 값을 이어 쓰면 xorshift32 의 첫 출력이 전부 작은 수라서 차수가 1로
    고정되고, 파운틴 코드가 쿠폰 수집 문제로 퇴화합니다.
    """
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
    """순번 텍스트 청크: QRP001/036:<base64>. 하나도 빠지면 안 됩니다."""
    b64 = base64.b64encode(data).decode("ascii")
    parts = [b64[i:i + chunk_size] for i in range(0, len(b64), chunk_size)]
    if len(parts) > 999:
        sys.exit("조각이 %d개입니다 — 3자리 순번을 넘습니다. --chunk 를 키우세요." % len(parts))
    return ["QRP%03d/%03d:%s" % (i + 1, len(parts), p) for i, p in enumerate(parts)]


# ==== 3. QR 그리기 =============================================================
#
# 바이트 모드 · 오류정정 L · 버전 1~40. 표준 라이브러리만 씁니다.
# ISO/IEC 18004 를 따랐고, 버전별로 zxing 디코드까지 확인했습니다.

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


_GEN_CACHE = {}


def _rs_generator(n):
    g = _GEN_CACHE.get(n)
    if g is None:
        g = [1]
        for i in range(n):
            ng = [0] * (len(g) + 1)
            for j, c in enumerate(g):
                ng[j] ^= c
                ng[j + 1] ^= _gf_mul(c, _EXP[i])
            g = ng
        _GEN_CACHE[n] = g
    return g


def _rs_remainder(data, n):
    g = _rs_generator(n)
    rem = list(data) + [0] * n
    for i in range(len(data)):
        f = rem[i]
        if f:
            lf = _LOG[f]
            for j, c in enumerate(g):
                if c:
                    rem[i + j] ^= _EXP[_LOG[c] + lf]
    return rem[len(data):]


TOTAL_CW = [26, 44, 70, 100, 134, 172, 196, 242, 292, 346, 404, 466, 532, 581,
            655, 733, 815, 901, 991, 1085, 1156, 1258, 1364, 1474, 1588, 1706,
            1828, 1921, 2051, 2185, 2323, 2465, 2611, 2761, 2876, 3034, 3196,
            3362, 3532, 3706]
EC_PER_BLOCK = [7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24,
                28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30,
                30, 30, 30, 30, 30, 30, 30, 30]
NUM_BLOCKS = [1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9,
              9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25]
ALIGN = [
    [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42],
    [6, 26, 46], [6, 28, 50], [6, 30, 54], [6, 32, 58], [6, 34, 62],
    [6, 26, 46, 66], [6, 26, 48, 70], [6, 26, 50, 74], [6, 30, 54, 78],
    [6, 30, 56, 82], [6, 30, 58, 86], [6, 34, 62, 90], [6, 28, 50, 72, 94],
    [6, 26, 50, 74, 98], [6, 30, 54, 78, 102], [6, 28, 54, 80, 106],
    [6, 32, 58, 84, 110], [6, 30, 58, 86, 114], [6, 34, 62, 90, 118],
    [6, 26, 50, 74, 98, 122], [6, 30, 54, 78, 102, 126],
    [6, 26, 52, 78, 104, 130], [6, 30, 56, 82, 108, 134],
    [6, 34, 60, 86, 112, 138], [6, 30, 58, 86, 114, 142],
    [6, 34, 62, 90, 118, 146], [6, 30, 54, 78, 102, 126, 150],
    [6, 24, 50, 76, 102, 128, 154], [6, 28, 54, 80, 106, 132, 158],
    [6, 32, 58, 84, 110, 136, 162], [6, 26, 54, 82, 110, 138, 166],
    [6, 30, 58, 86, 114, 142, 170],
]
MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def qr_capacity(version):
    return TOTAL_CW[version - 1] - EC_PER_BLOCK[version - 1] * NUM_BLOCKS[version - 1]


def qr_pick_version(nbytes):
    for v in range(1, 41):
        cc = 8 if v <= 9 else 16
        if (4 + cc + 8 * nbytes + 7) // 8 <= qr_capacity(v):
            return v
    raise ValueError("QR 하나에 담기에 너무 큽니다: %d 바이트" % nbytes)


def _codewords(payload, version):
    cap = qr_capacity(version)
    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)                                   # 바이트 모드
    put(len(payload), 8 if version <= 9 else 16)
    for b in payload:
        put(b, 8)
    put(0, min(4, cap * 8 - len(bits)))              # 종료 부호
    while len(bits) % 8:
        bits.append(0)
    cw = []
    for i in range(0, len(bits), 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        cw.append(v)
    n = 0
    while len(cw) < cap:                             # 채움 부호
        cw.append(0xEC if n % 2 == 0 else 0x11)
        n += 1
    return cw


def _interleave(cw, version):
    ec_n = EC_PER_BLOCK[version - 1]
    nb = NUM_BLOCKS[version - 1]
    short, extra = divmod(len(cw), nb)
    blocks, ecs, pos = [], [], 0
    for i in range(nb):
        size = short + (1 if i >= nb - extra else 0)   # 긴 블록이 뒤쪽
        blk = cw[pos:pos + size]
        pos += size
        blocks.append(blk)
        ecs.append(_rs_remainder(blk, ec_n))
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_n):
        out += [e[i] for e in ecs]
    return out


def _finder(m, res, r, c):
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            rr, cc = r + dr, c + dc
            if not (0 <= rr < len(m) and 0 <= cc < len(m)):
                continue
            ring = (dr in (0, 6) and 0 <= dc <= 6) or (dc in (0, 6) and 0 <= dr <= 6)
            core = 2 <= dr <= 4 and 2 <= dc <= 4
            m[rr][cc] = 1 if (ring or core) else 0
            res[rr][cc] = 1


def _skeleton(version):
    size = version * 4 + 17
    m = [[0] * size for _ in range(size)]
    res = [[0] * size for _ in range(size)]

    _finder(m, res, 0, 0)
    _finder(m, res, 0, size - 7)
    _finder(m, res, size - 7, 0)

    for i in range(8, size - 8):                       # 타이밍 패턴
        v = 1 - (i % 2)
        m[6][i] = v; res[6][i] = 1
        m[i][6] = v; res[i][6] = 1

    centers = ALIGN[version - 1]                       # 정렬 패턴
    last = size - 7
    for r in centers:
        for c in centers:
            # 찾기 패턴과 겹치는 세 자리만 빼고 전부 그립니다. 타이밍 줄 위에
            # 오는 것도 그려야 합니다 (버전 7 이상에서 생깁니다).
            if (r, c) in ((6, 6), (6, last), (last, 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = 1 if max(abs(dr), abs(dc)) != 1 else 0
                    res[r + dr][c + dc] = 1

    m[size - 8][8] = 1; res[size - 8][8] = 1           # 고정 검은 모듈

    for i in range(9):                                 # 형식 정보 자리
        res[8][i] = 1
        res[i][8] = 1
    for i in range(8):
        res[8][size - 1 - i] = 1
        res[size - 1 - i][8] = 1

    if version >= 7:                                   # 버전 정보 자리
        for i in range(6):
            for j in range(3):
                res[size - 11 + j][i] = 1
                res[i][size - 11 + j] = 1
    return m, res


def _place(m, res, stream):
    size = len(m)
    bits = []
    for v in stream:
        for i in range(7, -1, -1):
            bits.append((v >> i) & 1)
    idx, col, upward = 0, size - 1, True
    while col > 0:
        if col == 6:
            col -= 1
        for row in (range(size - 1, -1, -1) if upward else range(size)):
            for c in (col, col - 1):
                if res[row][c]:
                    continue
                m[row][c] = bits[idx] if idx < len(bits) else 0
                idx += 1
        upward = not upward
        col -= 2


def _format_bits(mask):
    data = (0b01 << 3) | mask                          # 오류정정 L = 01
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | (rem & 0x3FF)) ^ 0x5412


def _version_bits(version):
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | (rem & 0xFFF)


def _apply_format(m, mask):
    size = len(m)
    bits = _format_bits(mask)
    for i in range(15):
        b = (bits >> (14 - i)) & 1                     # 왼쪽(MSB)부터
        if i < 6:
            m[8][i] = b
        elif i == 6:
            m[8][7] = b
        elif i == 7:
            m[8][8] = b
        elif i == 8:
            m[7][8] = b
        else:
            m[14 - i][8] = b
        if i < 7:
            m[size - 1 - i][8] = b
        else:
            m[8][size - 15 + i] = b
    m[size - 8][8] = 1                                 # 검은 모듈은 형식정보가 아닙니다


def _apply_version(m, version):
    if version < 7:
        return
    size = len(m)
    bits = _version_bits(version)
    for i in range(18):
        b = (bits >> i) & 1
        r, c = divmod(i, 3)
        m[size - 11 + c][r] = b
        m[r][size - 11 + c] = b


def _penalty(m):
    size = len(m)
    score = 0
    lines = list(m) + [list(col) for col in zip(*m)]
    for line in lines:                                 # 규칙 1: 같은 색 5칸 이상
        run, prev = 1, line[0]
        for v in line[1:]:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, prev = 1, v
        if run >= 5:
            score += 3 + (run - 5)
    for r in range(size - 1):                          # 규칙 2: 2x2 덩어리
        for c in range(size - 1):
            s = m[r][c] + m[r][c + 1] + m[r + 1][c] + m[r + 1][c + 1]
            if s in (0, 4):
                score += 3
    pat = bytes((1, 0, 1, 1, 1, 0, 1))                 # 규칙 3: 1:1:3:1:1 무늬
    for line in lines:
        seq = bytes(line)
        idx = seq.find(pat)
        while idx != -1:
            offset = idx + 7
            if (idx in (0, size - 7) or not any(seq[max(idx - 4, 0):idx])
                    or not any(seq[offset:offset + 4])):
                score += 40
            else:
                offset = idx + 4
            idx = seq.find(pat, offset)
    dark = sum(sum(row) for row in m)                  # 규칙 4: 검은 비율
    score += 10 * (abs(dark * 100 // (size * size) - 50) // 5)
    return score


def qr_matrix(payload, version=None):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    version = version or qr_pick_version(len(payload))
    stream = _interleave(_codewords(payload, version), version)
    base, res = _skeleton(version)
    _place(base, res, stream)

    # 마스크 평가는 형식·버전 정보를 넣기 *전에* 합니다 (ISO/IEC 18004 7.8).
    best, best_score, best_mask = None, None, 0
    for mask in range(8):
        m = [row[:] for row in base]
        fn = MASKS[mask]
        for r in range(len(m)):
            mr, rr = m[r], res[r]
            for c in range(len(m)):
                if not rr[c] and fn(r, c):
                    mr[c] ^= 1
        s = _penalty(m)
        if best_score is None or s < best_score:
            best, best_score, best_mask = m, s, mask
    _apply_version(best, version)
    _apply_format(best, best_mask)
    return best, version


def qr_svg(payload, border=4):
    """화면 크기에 맞춰 또렷하게 커지는 인라인 SVG."""
    m, version = qr_matrix(payload)
    n = len(m)
    side = n + border * 2
    parts = []
    for y, row in enumerate(m):
        x = 0
        while x < n:
            if row[x]:
                run = 1
                while x + run < n and row[x + run]:
                    run += 1
                parts.append("M%d %dh%dv1h-%dz" % (x + border, y + border, run, run))
                x += run
            else:
                x += 1
    svg = ('<svg viewBox="0 0 %d %d" shape-rendering="crispEdges">'
           '<path d="%s" fill="#000"/></svg>' % (side, side, "".join(parts)))
    return svg, version


def qr_png(payload, scale=6, border=4):
    """PNG 바이트. zlib 과 struct 만 씁니다."""
    m, _ = qr_matrix(payload)
    n = len(m)
    side = (n + border * 2) * scale
    white = bytes([255]) * side
    rows = [white] * (border * scale)
    for row in m:
        line = bytearray(bytes([255]) * (border * scale))
        for v in row:
            line += bytes([0 if v else 255]) * scale
        line += bytes([255]) * (border * scale)
        rows += [bytes(line)] * scale
    rows += [white] * (border * scale)
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


# ==== 4. 화면 만들기 ===========================================================

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


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def write_sheets(chunks, out: Path, title, sha, per_sheet, border, png, scale, with_stream):
    n_sheets = math.ceil(len(chunks) / per_sheet)
    cols = math.ceil(math.sqrt(per_sheet))
    pages, max_version = [], 0

    for s in range(n_sheets):
        lo = s * per_sheet
        part = chunks[lo:lo + per_sheet]
        figs = []
        for i, text in enumerate(part, start=lo + 1):
            svg, ver = qr_svg(text, border)
            max_version = max(max_version, ver)
            figs.append("<figure>%s<figcaption>%d / %d</figcaption></figure>"
                        % (svg, i, len(chunks)))
            if png:
                (out / ("qr_%03d.png" % i)).write_bytes(qr_png(text, scale, border))

        prev_url = "sheet_%02d.html" % s if s > 0 else ""
        next_url = "sheet_%02d.html" % (s + 2) if s + 1 < n_sheets else ""
        nav = []
        if prev_url:
            nav.append('<a href="%s">← 이전</a>' % prev_url)
        if next_url:
            nav.append('<a href="%s">다음 →</a>' % next_url)
        nav.append('<a href="index.html">목록</a>')
        nav.append('<button id="bare">촬영 모드 (F)</button>')
        nav.append('<span class="hint">← → 로 넘기고, F 로 머리글을 숨깁니다</span>')

        html = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s — 시트 %d/%d</title>
<style>%s</style></head>
<body>
<header>
  <h1>%s — 시트 %d/%d <span>(QR %d~%d / 전체 %d)</span></h1>
  <p class="sha">SHA-256(zip) %s</p>
</header>
<main style="--cols:%d;--rows:%d">
%s
</main>
<nav>%s</nav>
<script>var PREV=%s,NEXT=%s;%s</script>
</body></html>
""" % (esc(title), s + 1, n_sheets, CSS, esc(title), s + 1, n_sheets,
       lo + 1, lo + len(part), len(chunks), sha,
       cols, math.ceil(len(part) / cols), "\n".join(figs), " ".join(nav),
       json.dumps(prev_url), json.dumps(next_url), NAV_JS)
        p = out / ("sheet_%02d.html" % (s + 1))
        p.write_text(html, encoding="utf-8")
        pages.append((p.name, lo + 1, lo + len(part)))

    items = "\n".join('<li><a href="%s">시트 %02d</a> — QR %d~%d</li>' % (n, i + 1, a, b)
                      for i, (n, a, b) in enumerate(pages))
    index = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s — QR 시트 %d장</title>
<style>%s
main{display:block}
ul{line-height:1.9;font-size:clamp(12px,1.7vmin,16px)}
p.tip{max-width:60ch;color:#444;font-size:clamp(11px,1.5vmin,15px);line-height:1.7}
</style></head>
<body>
<header>
  <h1>%s — QR 시트 %d장 <span>(QR %d개)</span></h1>
  <p class="sha">SHA-256(zip) %s</p>
</header>
<main>
%s<ul>%s</ul>
<p class="tip"><b>사진으로 찍는 요령.</b> 시트를 화면 가득 띄우고(F 키로 머리글을
숨기면 더 넓어집니다) 한 장씩 정면에서 찍으세요. 초점이 맞고 화면 전체가 프레임에
들어오면 됩니다. <b>순번 방식이라 한 장도 빠지면 안 됩니다</b> — 시트 번호를 세어
가며 찍으세요. 찍은 사진은 전부 한 번에 Kinescope 에 넣으면 됩니다.</p>
<p class="tip">QR 이 잘 안 읽히면 시트당 개수를 줄여 다시 만드세요
(<code>--per-sheet 4</code>). 개수가 적을수록 QR 이 커지고 훨씬 잘 읽힙니다.</p>
</main>
<nav><a href="%s">첫 시트부터 →</a></nav>
</body></html>
""" % (esc(title), n_sheets, CSS, esc(title), n_sheets, len(chunks), sha,
       ('<p class="tip"><b>동영상으로 찍는 게 편하면</b> '
        '<a href="stream.html">stream.html</a> 을 여세요. 조각을 좀 놓쳐도 되고 '
        '한 번만 찍으면 됩니다.</p>\n' if with_stream else ""),
       items, pages[0][0])
    (out / "index.html").write_text(index, encoding="utf-8")
    return n_sheets, max_version


def write_stream(frames, out: Path, title, sha, fps, K, grid):
    buttons = "".join('<button data-g="%d">%d</button>' % (g, g) for g in (1, 2, 4, 6, 9, 12))
    html = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s — QR 스트림</title>
<style>%s</style></head>
<body>
<header>
  <h1>%s — QR 스트림 <span>(블록 %d개 · 패킷 %d개)</span></h1>
  <p class="sha">SHA-256(zip) %s</p>
</header>
<div id="stage"></div>
<nav>
  <button id="play">재생</button>
  <label>속도 <input type="range" id="fps" min="2" max="20" step="1" value="%d">
    <span id="fpsv">%d fps</span></label>
  <span class="grid">칸 %s</span>
  <button id="bare">촬영 모드 (F)</button>
  <span class="count" id="count"></span>
  <span class="hint">칸을 늘리면 그만큼 빨라집니다 — 카메라에 또렷하게 잡히는 선까지</span>
</nav>
<script>var FRAMES=%s,INIT_GRID=%d;%s</script>
</body></html>
""" % (esc(title), STREAM_CSS, esc(title), K, len(frames), sha, fps, fps,
       buttons, json.dumps(frames), grid, STREAM_JS)
    p = out / "stream.html"
    p.write_text(html, encoding="utf-8")
    return p


# ==== 5. 실행 ==================================================================

def build_sheets(data, args, out, title, sha):
    chunks = sheet_chunks(data, args.chunk)
    n, ver = write_sheets(chunks, out, title, sha, max(1, args.per_sheet),
                          args.border, args.png, args.scale,
                          args.mode in ("stream", "both"))
    print("  시트 %d장 · QR %d개 (버전 %d)  →  %s" % (n, len(chunks), ver, out / "index.html"))
    if ver >= 25 and args.per_sheet > 6:
        print("  ! QR 이 촘촘합니다. 사진이 잘 안 읽히면 --per-sheet 4 로 다시 만드세요.")
    return out / "index.html"


def build_stream(data, args, out, title, sha):
    K = max(1, math.ceil(len(data) / args.block))
    count = max(K + 4, math.ceil(K * args.overhead))
    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    frames, ver = [], 0
    for pkt in lt_packets(data, args.block, count, rng):
        svg, v = qr_svg(pkt, args.border)
        frames.append(svg)
        ver = max(ver, v)
        if args.png:
            (out / ("frame_%05d.png" % (len(frames) - 1))).write_bytes(
                qr_png(pkt, args.scale, args.border))
    p = write_stream(frames, out, title, sha, args.fps, K, args.grid)
    secs = len(frames) / max(1, args.fps)
    print("  블록 %d개 · 패킷 %d개 (버전 %d)  →  %s" % (K, len(frames), ver, p))
    print("  한 바퀴 %.0f초 (1칸) · %.0f초 (6칸) · %.0f초 (12칸)" % (secs, secs / 6, secs / 12))
    return p


def main():
    ap = argparse.ArgumentParser(
        description="옮길 파일을 QR 로 바꿔 화면에 띄웁니다. 그냥 실행하면 이 파일이 "
                    "있는 폴더를 통째로 인코딩합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="예)  python kinescope_encode.py\n"
               "     python kinescope_encode.py sheet ./내보낼폴더 -o out --per-sheet 4")
    ap.add_argument("mode", nargs="?", choices=["sheet", "stream", "both"], default="both",
                    help="sheet=사진용 시트, stream=동영상용 재생기, both=둘 다(기본)")
    ap.add_argument("input", nargs="?", type=Path, default=None,
                    help="옮길 파일이나 폴더 (생략하면 이 스크립트가 있는 폴더)")
    ap.add_argument("-o", "--out", type=Path, default=None, help="결과를 넣을 폴더 (기본 _qr)")
    ap.add_argument("--chunk", type=int, default=1000, help="sheet: QR 하나에 담을 base64 길이")
    ap.add_argument("--per-sheet", type=int, default=6, help="sheet: 한 화면에 넣을 QR 개수")
    ap.add_argument("--block", type=int, default=512, help="stream: 블록 크기(바이트)")
    ap.add_argument("--overhead", type=float, default=2.2, help="stream: 블록 수 대비 패킷 배수")
    ap.add_argument("--fps", type=int, default=10, help="stream: 재생기 기본 속도")
    ap.add_argument("--grid", type=int, default=1, choices=[1, 2, 4, 6, 9, 12],
                    help="stream: 처음에 띄울 칸 수 (재생기에서도 바꿀 수 있습니다)")
    ap.add_argument("--seed", type=int, default=None, help="stream: 재현용 난수 시드")
    ap.add_argument("--border", type=int, default=4, help="QR 둘레 여백 모듈 수")
    ap.add_argument("--png", action="store_true", help="HTML 과 함께 PNG 도 저장")
    ap.add_argument("--scale", type=int, default=6, help="--png 일 때 한 모듈의 픽셀 크기")
    ap.add_argument("--no-open", action="store_true", help="끝나고 브라우저를 열지 않습니다")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    src = args.input if args.input is not None else here
    if not src.exists():
        sys.exit("입력을 찾을 수 없습니다: %s" % src)

    if src.is_dir():
        files = collect(src, Path(__file__))
        if not files:
            sys.exit("%s 안에 옮길 파일이 없습니다. 파일을 넣고 다시 실행하세요." % src)
        data = zip_files(files, src)
        print("%s 안의 파일 %d개" % (src, len(files)))
        for p in files[:8]:
            print("   %s" % p.relative_to(src))
        if len(files) > 8:
            print("   … 외 %d개" % (len(files) - 8))
    else:
        data = as_zip(src)
        print("%s" % src)

    sha = hashlib.sha256(data).hexdigest()
    title = src.name or "kinescope"
    out = args.out if args.out is not None else (src if src.is_dir() else src.parent) / "_qr"
    out.mkdir(parents=True, exist_ok=True)

    print("zip %s 바이트 · SHA-256 %s" % (format(len(data), ","), sha))
    print("만드는 중… (조금 걸립니다)")

    first = None
    if args.mode in ("stream", "both"):
        first = build_stream(data, args, out, title, sha) or first
    if args.mode in ("sheet", "both"):
        p = build_sheets(data, args, out, title, sha)
        first = first or p

    print("\n다 됐습니다. 브라우저로 열고 화면을 찍으세요.")
    if first and not args.no_open:
        try:
            webbrowser.open(first.resolve().as_uri())
        except Exception:
            pass


if __name__ == "__main__":
    main()
