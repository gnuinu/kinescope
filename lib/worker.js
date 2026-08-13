// worker.js — QR 디코딩과 조립을 메인 스레드 밖에서 처리합니다.
import { prepareZXingModule, readBarcodes } from "../vendor/zxing/reader/index.js";
import { unzipSync } from "../vendor/fflate.js";
import { LTDecoder, SequentialCollector } from "./lt.js";

// wasm 을 같은 폴더에서 찾도록 고정 (오프라인 동작에 필수).
// zxing-wasm 번들은 워커 안에서 자기 위치를 알아내지 못해 기본 경로가 빈 문자열이
// 됩니다. 이 override 가 없으면 wasm 을 못 찾습니다.
const WASM_URL = new URL("../vendor/zxing/reader/zxing_reader.wasm", import.meta.url).href;
prepareZXingModule({ overrides: { locateFile: (p) => (p.endsWith(".wasm") ? WASM_URL : p) } });

const READER_OPTS = {
  formats: ["QRCode"],
  tryHarder: true,
  tryRotate: true,
  tryInvert: true,
  maxNumberOfSymbols: 32,
};

let lt = null;         // 파운틴 스트림용
let seq = null;        // 순번 사진용
let mode = null;       // 'stream' | 'photo'
let framesSeen = 0;

function post(type, payload, transfer) {
  self.postMessage({ type, ...payload }, transfer || []);
}

function state() {
  if (mode === "stream" && lt) {
    return {
      mode, framesSeen,
      solved: lt.solved.size, total: lt.K,
      packets: lt.nPackets, dupes: lt.nDupe, foreign: lt.nForeign,
      progress: lt.progress, ready: lt.ready,
    };
  }
  if (mode === "photo" && seq) {
    return {
      mode, framesSeen,
      solved: seq.parts.size, total: seq.total,
      packets: seq.parts.size, dupes: seq.nDupe, foreign: 0,
      progress: seq.progress, ready: seq.ready,
      missing: seq.missing,
    };
  }
  return {
    mode, framesSeen, solved: 0, total: null,
    packets: 0, dupes: 0, foreign: 0, progress: 0, ready: false,
  };
}

// 격자를 실제 상태대로 칠할 수 있도록 채워진 칸의 번호를 함께 보냅니다.
function solvedIndices() {
  if (mode === "stream" && lt) return Array.from(lt.solved.keys());
  if (mode === "photo" && seq) return Array.from(seq.parts.keys(), (i) => i - 1);
  return [];
}

function ingest(results) {
  let hit = false;
  for (const r of results) {
    // 사진 방식은 텍스트 헤더, 스트림 방식은 바이너리 매직으로 구분합니다.
    if (typeof r.text === "string" && r.text.startsWith("QRP")) {
      if (mode === "stream") continue;
      mode = "photo";
      seq ??= new SequentialCollector();
      if (seq.add(r.text)) hit = true;
      continue;
    }
    const b = r.bytes;
    if (b && b.length > 18 && b[0] === 0x51 && b[1] === 0x53) {
      if (mode === "photo") continue;
      mode = "stream";
      lt ??= new LTDecoder();
      if (lt.add(b instanceof Uint8Array ? b : new Uint8Array(b))) hit = true;
    }
  }
  return hit;
}

async function sha256Hex(data) {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", data));
  return Array.from(d, (x) => x.toString(16).padStart(2, "0")).join("");
}

async function finish() {
  // peeling 이 멈춰 있으면 남은 방정식을 직접 소거해 봅니다.
  let gauss = false;
  if (mode === "stream" && lt && !lt.ready && lt.pending.length) {
    gauss = lt.solveGaussian();
    if (gauss) post("progress", { ...state(), gauss, solvedIdx: solvedIndices() });
  }

  const st = state();
  if (!st.ready) { post("done", { ok: false, ...st, gauss }); return; }

  const data = mode === "stream" ? lt.result() : seq.result();
  const sha = await sha256Hex(data);

  // 스트림 패킷 헤더에는 원본 SHA-256 의 앞 4바이트가 실려 있습니다.
  let verified = null;
  if (mode === "stream") {
    verified = await lt.verify(data);
    if (!verified) {
      post("done", { ok: false, ...st, sha, verified, gauss,
        error: "조립은 끝났지만 체크섬이 맞지 않습니다 — 다른 묶음의 QR이 섞였을 수 있습니다." });
      return;
    }
  }

  let files = [];
  try {
    const entries = unzipSync(data);
    files = Object.entries(entries)
      .filter(([n]) => !n.endsWith("/"))
      .map(([name, bytes]) => ({ name, size: bytes.length, bytes }));
  } catch (e) {
    post("done", { ok: false, ...st, sha, verified, gauss,
      error: "복원은 됐지만 zip 을 열 수 없습니다: " + e.message });
    return;
  }

  // 복사 대신 소유권을 넘깁니다 — 큰 파일에서 메인 스레드가 멎지 않도록.
  const zip = Uint8Array.from(data);
  const transfer = [zip.buffer, ...files.map((f) => f.bytes.buffer)];
  post("done", { ok: true, ...st, sha, verified, gauss, zipSize: zip.length, zip, files },
       Array.from(new Set(transfer)));
}

self.onmessage = async (ev) => {
  const m = ev.data;
  try {
    if (m.type === "reset") {
      lt = null; seq = null; mode = null; framesSeen = 0;
      post("progress", { ...state(), solvedIdx: [] });
      return;
    }
    if (m.type === "frame") {
      framesSeen++;
      const results = await readBarcodes(m.image, READER_OPTS);
      const hit = ingest(results);
      const st = state();
      post("progress", { ...st, hit, frameIndex: m.frameIndex,
                         solvedIdx: hit ? solvedIndices() : null });
      if (st.ready) post("ready", st);
      return;
    }
    if (m.type === "finish") { await finish(); return; }
  } catch (e) {
    post("error", { message: e?.message || String(e) });
  }
};
