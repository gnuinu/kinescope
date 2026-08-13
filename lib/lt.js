// lt.js — LT 파운틴 디코더 + 순번 수집기 (lt.py 의 자바스크립트 포팅).
// Robust Soliton 누적분포를 24비트 정수로 양자화해 두었으므로
// 파이썬 인코더와 블록 선택이 비트 단위로 일치합니다.

export const MAGIC = 0x5153; // 'QS'
export const HEADER_LEN = 18;
const SCALE = 1 << 24;

// xorshift32 — 파이썬 구현과 동일해야 합니다.
export function makeRng(seed) {
  let x = seed >>> 0;
  if (x === 0) x = 0x9e3779b9;
  return function next() {
    x ^= (x << 13) >>> 0; x >>>= 0;
    x ^= x >>> 17;
    x ^= (x << 5) >>> 0; x >>>= 0;
    return x >>> 0;
  };
}

export function robustSoliton(K, c = 0.05, delta = 0.05) {
  if (K === 1) return [SCALE];
  const rho = new Array(K);
  rho[0] = 1 / K;
  for (let i = 2; i <= K; i++) rho[i - 1] = 1 / (i * (i - 1));
  const S = c * Math.log(K / delta) * Math.sqrt(K);
  const piv = Math.min(K, Math.max(1, Math.round(K / S)));
  const tau = new Array(K).fill(0);
  for (let i = 1; i < piv; i++) tau[i - 1] = S / (K * i);
  if (S / delta > 1.0) tau[piv - 1] = (S * Math.log(S / delta)) / K;
  let Z = 0;
  for (let i = 0; i < K; i++) Z += rho[i] + tau[i];
  const cdf = new Array(K);
  let acc = 0;
  for (let i = 0; i < K; i++) {
    acc += (rho[i] + tau[i]) / Z;
    cdf[i] = Math.min(SCALE, Math.floor(acc * SCALE + 0.5));
  }
  cdf[K - 1] = SCALE;
  return cdf;
}

function pickDegree(cdf, u24) {
  let lo = 0, hi = cdf.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (u24 <= cdf[mid]) hi = mid; else lo = mid + 1;
  }
  return lo + 1;
}

export function blockIndices(seed, K, cdf) {
  const next = makeRng(seed);
  let d = pickDegree(cdf, next() >>> 8);
  if (d > K) d = K;
  const chosen = new Set();
  while (chosen.size < d) chosen.add(next() % K);
  return Array.from(chosen).sort((a, b) => a - b);
}

export class LTDecoder {
  constructor() {
    this.K = null; this.bs = null; this.totalLen = null; this.sha4 = null;
    this.cdf = null;
    this.solved = new Map();          // index -> Uint8Array
    this.pending = [];                // [Set, Uint8Array]
    this.seeds = new Set();
    this.nPackets = 0; this.nDupe = 0; this.nForeign = 0;
  }
  get ready() { return this.K !== null && this.solved.size >= this.K; }
  get progress() { return this.K ? this.solved.size / this.K : 0; }

  add(pkt) {
    if (!pkt || pkt.length < HEADER_LEN + 1) return false;
    const dv = new DataView(pkt.buffer, pkt.byteOffset, pkt.byteLength);
    if (dv.getUint16(0) !== MAGIC) return false;
    const K = dv.getUint16(2), bs = dv.getUint16(4), total = dv.getUint32(6);
    const sha4 = dv.getUint32(10), seed = dv.getUint32(14);
    if (pkt.length < HEADER_LEN + bs) return false;
    const payload = pkt.subarray(HEADER_LEN, HEADER_LEN + bs);

    if (this.K === null) {
      this.K = K; this.bs = bs; this.totalLen = total; this.sha4 = sha4;
      this.cdf = robustSoliton(K);
    } else if (K !== this.K || bs !== this.bs || total !== this.totalLen ||
               sha4 !== this.sha4) {
      this.nForeign++; return false;         // 다른 묶음의 QR
    }
    if (this.seeds.has(seed)) { this.nDupe++; return false; }
    this.seeds.add(seed);
    this.nPackets++;

    this._absorb(new Set(blockIndices(seed, this.K, this.cdf)),
                 Uint8Array.from(payload));
    return true;
  }

  _xor(dst, src) { for (let i = 0; i < dst.length; i++) dst[i] ^= src[i]; }

  _absorb(idx, data) {
    for (const i of Array.from(idx)) {
      const s = this.solved.get(i);
      if (s) { this._xor(data, s); idx.delete(i); }
    }
    if (idx.size === 0) return;
    if (idx.size > 1) { this.pending.push([idx, data]); return; }

    const queue = [[idx.values().next().value, data]];
    while (queue.length) {
      const [bi, bdata] = queue.pop();
      if (this.solved.has(bi)) continue;
      this.solved.set(bi, bdata);
      const still = [];
      for (const entry of this.pending) {
        const [s, d] = entry;
        if (s.has(bi)) {
          this._xor(d, bdata);
          s.delete(bi);
          if (s.size === 1) { queue.push([s.values().next().value, d]); continue; }
          if (s.size === 0) continue;
        }
        still.push(entry);
      }
      this.pending = still;
    }
  }

  // peeling 이 멈췄을 때의 마지막 수단. 남은 방정식을 GF(2) 위에서 그대로
  // 소거합니다. peeling 은 차수 1짜리가 끊기면 못 풀지만, 방정식이 미지수
  // 개수만큼 독립이면 이쪽은 풉니다 — 짧게 찍은 영상에서 자주 살아납니다.
  // K 가 커지면 O(n²) 비용이 감당이 안 되므로 한도를 둡니다.
  solveGaussian(limit = 800) {
    if (this.K === null || this.ready) return this.ready;

    const unknown = [];
    for (let i = 0; i < this.K; i++) if (!this.solved.has(i)) unknown.push(i);
    const n = unknown.length;
    if (n === 0 || n > limit) return false;

    const col = new Map();
    for (let i = 0; i < n; i++) col.set(unknown[i], i);
    const words = (n + 31) >> 5;

    const rows = [];
    for (const [idxSet, payload] of this.pending) {
      const mask = new Uint32Array(words);
      const data = Uint8Array.from(payload);
      let live = 0;
      for (const i of idxSet) {
        const s = this.solved.get(i);
        if (s) { this._xor(data, s); continue; }
        const c = col.get(i);
        if (c === undefined) continue;
        mask[c >>> 5] |= 1 << (c & 31);
        live++;
      }
      if (live) rows.push([mask, data]);
    }
    if (rows.length < n) return false;

    const pivot = new Int32Array(n).fill(-1);
    let r = 0;
    for (let c = 0; c < n && r < rows.length; c++) {
      const w = c >>> 5, bit = 1 << (c & 31);
      let p = -1;
      for (let i = r; i < rows.length; i++) if (rows[i][0][w] & bit) { p = i; break; }
      if (p < 0) continue;                       // 이 열은 나중 열로 미룹니다
      const tmp = rows[r]; rows[r] = rows[p]; rows[p] = tmp;
      const [pm, pd] = rows[r];
      for (let i = 0; i < rows.length; i++) {
        if (i === r) continue;
        const [m, d] = rows[i];
        if (!(m[w] & bit)) continue;
        for (let k = 0; k < words; k++) m[k] ^= pm[k];
        this._xor(d, pd);
      }
      pivot[c] = r;
      r++;
    }
    for (let c = 0; c < n; c++) if (pivot[c] < 0) return false;   // 랭크 부족

    for (let c = 0; c < n; c++) this.solved.set(unknown[c], rows[pivot[c]][1]);
    this.pending = [];
    return true;
  }

  result() {
    if (!this.ready) throw new Error("아직 복원할 수 없습니다.");
    const out = new Uint8Array(this.K * this.bs);
    for (let i = 0; i < this.K; i++) out.set(this.solved.get(i), i * this.bs);
    return out.subarray(0, this.totalLen);
  }

  // 헤더에 실린 SHA-256 앞 4바이트와 대조합니다.
  async verify(data) {
    const h = new Uint8Array(await crypto.subtle.digest("SHA-256", data));
    return new DataView(h.buffer).getUint32(0) === this.sha4;
  }
}

// 사진 방식(qr_export.py)의 순번 청크: "QRP001/013:<base64>"
export class SequentialCollector {
  constructor() { this.total = null; this.parts = new Map(); this.nDupe = 0; }
  get ready() { return this.total !== null && this.parts.size >= this.total; }
  get progress() { return this.total ? this.parts.size / this.total : 0; }
  get missing() {
    if (this.total === null) return [];
    const m = [];
    for (let i = 1; i <= this.total; i++) if (!this.parts.has(i)) m.push(i);
    return m;
  }
  add(text) {
    const m = /^QRP(\d{3})\/(\d{3}):([A-Za-z0-9+/=]+)$/.exec(text.trim());
    if (!m) return false;
    const idx = +m[1], total = +m[2];
    if (this.total === null) this.total = total;
    else if (total !== this.total) return false;
    if (this.parts.has(idx)) { this.nDupe++; return false; }
    this.parts.set(idx, m[3]);
    return true;
  }
  result() {
    let b64 = "";
    for (let i = 1; i <= this.total; i++) b64 += this.parts.get(i);
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
}
