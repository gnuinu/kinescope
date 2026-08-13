#!/usr/bin/env python3
"""kinescope_encode.py — 참조 인코더 / 테스트 픽스처 생성기.

Kinescope 가 읽는 두 형식을 만들어냅니다.

  stream   LT 파운틴 패킷을 담은 바이너리 QR 연속 프레임 (동영상용)
  sheet    "QRP001/013:<base64>" 순번 텍스트 QR 낱장 (사진용)

`lib/lt.js` 의 디코더와 비트 단위로 맞춘 구현입니다. 블록 선택에 쓰는
xorshift32 와 Robust Soliton 누적분포(24비트 양자화)가 자바스크립트 쪽과
같은 값을 내도록 되어 있습니다.

사용 예:

    pip install segno
    python3 tools/kinescope_encode.py stream payload.zip -o out/
    python3 tools/kinescope_encode.py sheet  payload.zip -o out/

뽑아낸 프레임은 ffmpeg 로 이어 붙이면 그대로 재생용 동영상이 됩니다:

    ffmpeg -start_number 0 -framerate 10 -i out/frame_%05d.png \\
           -pix_fmt yuv420p out/stream.mp4
"""

import argparse
import hashlib
import random
import math
import struct
import sys
from pathlib import Path

MAGIC = 0x5153  # 'QS'
HEADER = struct.Struct(">HHHIII")  # magic, K, blockSize, totalLen, sha4, seed
HEADER_LEN = HEADER.size  # 18
SCALE = 1 << 24
MASK = 0xFFFFFFFF


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
    """LT 패킷을 count 개 만들어 (seed, bytes) 로 내놓습니다.

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
        yield seed, HEADER.pack(MAGIC, K, block_size, total, sha4, seed) + bytes(acc)


def sheet_chunks(data, chunk_size):
    """qr_export.py 형식의 순번 텍스트 청크."""
    import base64

    b64 = base64.b64encode(data).decode("ascii")
    parts = [b64[i:i + chunk_size] for i in range(0, len(b64), chunk_size)]
    if len(parts) > 999:
        sys.exit(f"청크가 {len(parts)}개입니다 — 3자리 순번을 넘습니다. --chunk 를 키우세요.")
    return [f"QRP{i + 1:03d}/{len(parts):03d}:{p}" for i, p in enumerate(parts)]


def render(payload, path, scale, border):
    import segno

    segno.make(payload, error="l").save(str(path), scale=scale, border=border)


def main():
    ap = argparse.ArgumentParser(description="Kinescope 용 QR 생성기")
    ap.add_argument("mode", choices=["stream", "sheet"])
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("out"))
    ap.add_argument("--block", type=int, default=512, help="stream: 블록 크기(바이트)")
    ap.add_argument("--overhead", type=float, default=2.2,
                    help="stream: 블록 수 대비 패킷 배수")
    ap.add_argument("--chunk", type=int, default=1000, help="sheet: base64 청크 길이")
    ap.add_argument("--scale", type=int, default=6, help="QR 한 모듈의 픽셀 크기")
    ap.add_argument("--border", type=int, default=4, help="여백 모듈 수")
    ap.add_argument("--seed", type=int, default=None, help="stream: 재현용 난수 시드")
    args = ap.parse_args()

    data = args.input.read_bytes()
    args.out.mkdir(parents=True, exist_ok=True)

    if args.mode == "stream":
        K = max(1, math.ceil(len(data) / args.block))
        count = max(K + 4, math.ceil(K * args.overhead))
        rng = random.Random(args.seed) if args.seed is not None else random.Random()
        for n, (_seed, pkt) in enumerate(lt_packets(data, args.block, count, rng)):
            render(pkt, args.out / f"frame_{n:05d}.png", args.scale, args.border)
        print(f"블록 {K}개 · 패킷 {count}개 → {args.out}")
        print(f"ffmpeg -start_number 0 -framerate 10 -i {args.out}/frame_%05d.png "
              f"-pix_fmt yuv420p {args.out}/stream.mp4")
    else:
        chunks = sheet_chunks(data, args.chunk)
        for n, text in enumerate(chunks):
            render(text, args.out / f"sheet_{n + 1:03d}.png", args.scale, args.border)
        print(f"시트 {len(chunks)}장 → {args.out}")


if __name__ == "__main__":
    main()
