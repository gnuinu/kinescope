// app.js — 파일 입력 → 프레임 추출 → 워커 전달 → 결과 표시
const $ = (s) => document.querySelector(s);

const worker = new Worker(new URL("./lib/worker.js", import.meta.url), { type: "module" });

const ui = {
  drop: $("#drop"), pick: $("#pick"), file: $("#file"),
  grid: $("#grid"), stage: $("#stage"), status: $("#status"),
  bar: $("#bar"), pct: $("#pct"),
  mFrames: $("#m-frames"), mPackets: $("#m-packets"),
  mBlocks: $("#m-blocks"), mDupes: $("#m-dupes"),
  result: $("#result"), files: $("#files"), sha: $("#sha"), verdict: $("#verdict"),
  dl: $("#dl"), again: $("#again"),
  rate: $("#rate"), rateV: $("#rate-v"),
};

let cells = [];
let busy = false;      // 워커가 프레임 하나를 붙들고 있는 동안 참
let finished = false;  // 충분히 모여 스캔을 멈춘 뒤
let aborted = false;   // 워커가 죽었을 때
let lastZip = null;

function setStatus(text, kind = "") {
  ui.status.textContent = text;
  ui.status.className = "status " + kind;
}

function buildGrid(n) {
  if (cells.length === n) return;
  ui.grid.innerHTML = "";
  // 칸 크기를 먼저 정하고 폭에 맞춰 열 수를 셉니다 — 블록이 열 개든 이천 개든
  // 격자가 늘 비슷한 결로 보이도록.
  const gap = 2;
  const size = n <= 40 ? 30 : n <= 400 ? 18 : 10;
  const width = ui.grid.clientWidth || 800;
  const cols = Math.max(1, Math.min(n, Math.floor((width + gap) / (size + gap))));
  ui.grid.style.gridTemplateColumns = `repeat(${cols}, ${size}px)`;
  const frag = document.createDocumentFragment();
  cells = [];
  for (let i = 0; i < n; i++) {
    const d = document.createElement("i");
    frag.appendChild(d);
    cells.push(d);
  }
  ui.grid.appendChild(frag);
}

// 워커가 보내준 번호대로 칠합니다 — 개수가 아니라 실제로 풀린 블록입니다.
function paintGrid(indices) {
  const on = new Set(indices);
  for (let i = 0; i < cells.length; i++) {
    const v = on.has(i) ? "1" : "0";
    if (cells[i].dataset.on !== v) cells[i].dataset.on = v;
  }
}

function fmt(n) { return typeof n === "number" ? n.toLocaleString("ko-KR") : "—"; }

worker.onerror = (e) => {
  aborted = true; busy = false;
  setStatus("워커를 시작하지 못했습니다: " + (e.message || "알 수 없는 오류"), "bad");
};

worker.onmessage = (ev) => {
  const m = ev.data;

  if (m.type === "error") {
    aborted = true; busy = false;
    setStatus("오류: " + m.message, "bad");
    return;
  }

  if (m.type === "progress" || m.type === "ready") {
    busy = false;
    ui.mFrames.textContent = fmt(m.framesSeen);
    ui.mPackets.textContent = fmt(m.packets);
    ui.mBlocks.textContent = m.total ? `${fmt(m.solved)} / ${fmt(m.total)}` : "—";
    ui.mDupes.textContent = fmt(m.dupes);
    if (m.total) {
      buildGrid(m.total);
      if (m.solvedIdx) paintGrid(m.solvedIdx);
      const p = Math.round(m.progress * 100);
      ui.bar.style.width = p + "%";
      ui.pct.textContent = p + "%";
    }
    if (m.type === "ready" && !finished) {
      finished = true;
      setStatus("충분히 모였습니다. 조립 중…", "good");
      worker.postMessage({ type: "finish" });
    }
    return;
  }

  if (m.type === "done") {
    if (!m.ok) {
      const miss = m.missing?.length
        ? ` 빠진 조각: ${m.missing.slice(0, 20).join(", ")}${m.missing.length > 20 ? " …" : ""}`
        : "";
      const why = m.error || (
        m.total
          ? `복원 실패 — 블록 ${fmt(m.solved)}/${fmt(m.total)}.` +
            (m.mode === "stream"
              ? " 더 길게 촬영하거나 재생 fps 를 낮춰 다시 찍으세요."
              : " 빠진 QR 을 다시 찍어 함께 넣으세요.")
          : "QR 을 하나도 읽지 못했습니다 — 화면이 또렷하게 나온 영상인지 확인해 주세요.");
      setStatus(why + miss, "bad");
      return;
    }
    lastZip = m.zip;
    ui.sha.textContent = m.sha;
    ui.verdict.textContent = m.verified ? "  ✓ 체크섬 일치" : "";
    ui.verdict.className = m.verified ? "ok" : "";
    ui.files.innerHTML = "";
    for (const f of m.files) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = f.name;
      const size = document.createElement("b");
      size.textContent = fmt(f.size) + " B";
      li.append(name, size);
      li.onclick = () => saveBlob(new Blob([f.bytes]), f.name.split("/").pop());
      ui.files.appendChild(li);
    }
    ui.result.hidden = false;
    setStatus(`복원 완료 — 파일 ${m.files.length}개 · ${fmt(m.zipSize)} B` +
              (m.gauss ? " (남은 블록은 연립 소거로 풀었습니다)" : ""), "good");
    return;
  }
};

function saveBlob(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

ui.dl.onclick = () => lastZip && saveBlob(new Blob([lastZip]), "restored.zip");

// ---- 프레임 추출 ------------------------------------------------------------

const canvas = document.createElement("canvas");
const ctx = canvas.getContext("2d", { willReadFrequently: true });

function grab(source, w, h) {
  canvas.width = w; canvas.height = h;
  ctx.drawImage(source, 0, 0, w, h);
  return ctx.getImageData(0, 0, w, h);
}

function send(img, frameIndex) {
  busy = true;
  worker.postMessage(
    { type: "frame", image: { data: img.data, width: img.width, height: img.height }, frameIndex },
    [img.data.buffer]
  );
}

function waitIdle() {
  return new Promise((res) => {
    if (!busy) { res(); return; }
    const t = setInterval(() => { if (!busy) { clearInterval(t); res(); } }, 8);
  });
}

async function runImages(files, offset = 0) {
  for (let i = 0; i < files.length; i++) {
    if (finished || aborted) break;
    setStatus(`사진 ${i + 1}/${files.length} 읽는 중…`);
    let bmp;
    try {
      bmp = await createImageBitmap(files[i]);
    } catch {
      setStatus(`${files[i].name} 을(를) 이미지로 열 수 없습니다 — 건너뜁니다.`, "bad");
      continue;
    }
    send(grab(bmp, bmp.width, bmp.height), offset + i);
    bmp.close();
    await waitIdle();
  }
}

// 재생 패스: 빠르지만 디코더가 따라가지 못한 프레임은 흘려보냅니다.
// 대개 이것만으로 충분히 모입니다.
async function playPass(v, w, h, counter) {
  v.playbackRate = Number(ui.rate.value) || 1;
  const useRVFC = "requestVideoFrameCallback" in v;
  try {
    await v.play();
  } catch {
    return;   // 자동재생이 막히면 훑기 패스가 대신합니다
  }
  await new Promise((res) => {
    let done = false;
    const stop = () => { if (!done) { done = true; res(); } };
    const step = () => {
      if (done) return;
      if (finished || aborted || v.ended) { stop(); return; }
      if (!busy) send(grab(v, w, h), counter.n++);
      if (useRVFC) v.requestVideoFrameCallback(step);
      else requestAnimationFrame(step);
    };
    v.onended = stop;
    if (useRVFC) v.requestVideoFrameCallback(step); else requestAnimationFrame(step);
  });
  v.pause();
  await waitIdle();   // 마지막으로 보낸 프레임의 결과까지
}

// t 로 seek 하고, 그 자리에 실제로 내려앉은 프레임의 원본 타임스탬프를 돌려줍니다.
// requestVideoFrameCallback 이 있으면 seek 이 어느 프레임에 걸렸는지 알 수 있어
// 같은 프레임을 두 번 디코드하지 않게 됩니다. 콜백은 seek 을 걸기 *전에* 등록해야
// 프레임이 먼저 그려지고 콜백을 놓치는 일이 없습니다. 알 수 없으면 null.
function seekAndPresent(v, t) {
  const hasRVFC = "requestVideoFrameCallback" in v;
  return new Promise((res) => {
    let mt = null, done = false, seeked = false, framed = !hasRVFC;
    let grace = 0;
    const fin = () => {
      if (done || !seeked || !framed) return;
      done = true; clearTimeout(guard); clearTimeout(grace);
      v.removeEventListener("seeked", onSeeked);
      res(mt);
    };
    const giveUp = () => { framed = true; fin(); };
    // seeked 자체가 안 오는 경우까지 대비해 두 조건을 모두 풀고 빠져나옵니다.
    const guard = setTimeout(() => { seeked = true; framed = true; fin(); }, 2000);
    const onSeeked = () => {
      seeked = true;
      if (!framed) grace = setTimeout(giveUp, 150);  // 같은 프레임이면 콜백이 안 옵니다
      fin();
    };
    v.addEventListener("seeked", onSeeked);
    if (hasRVFC) v.requestVideoFrameCallback((_now, meta) => { mt = meta.mediaTime; framed = true; fin(); });
    v.currentTime = t;
  });
}

// 훑기 패스: 재생 대신 한 프레임씩 seek 하며 훑습니다. 느리지만 한 장도
// 놓치지 않습니다 — 재생만으로 모자랐을 때만 돕니다.
const MIN_STEP = 1 / 60;    // 60fps 보다 촘촘히 찍을 일은 없습니다
const MAX_PROBE = 0.1;      // 프레임 경계를 더듬을 때의 최대 보폭

async function seekPass(v, w, h, counter) {
  const dur = v.duration;
  if (!Number.isFinite(dur) || dur <= 0) return;

  let period = null;   // 지금까지 본 프레임 간격의 최솟값 — 넘겨짚어 건너뛰지 않도록
  let probe = MIN_STEP;
  let last = -1;       // 마지막으로 디코드한 프레임의 mediaTime
  let t = 0;
  let shown = -1;

  while (t < dur && !finished && !aborted) {
    const mt = await seekAndPresent(v, Math.min(t, dur - 1e-3));
    if (finished || aborted) break;

    if (mt !== null) {
      if (mt === last) {                 // 아직 같은 프레임 — 디코드하지 않고 더 갑니다
        t += probe;
        probe = Math.min(probe * 1.6, MAX_PROBE);
        continue;
      }
      if (last >= 0 && mt > last) period = Math.min(period ?? Infinity, mt - last);
      last = mt;
      probe = MIN_STEP;
    }

    send(grab(v, w, h), counter.n++);
    await waitIdle();
    if (finished || aborted) break;

    // 관측한 간격만큼만 전진합니다. 아직 모르면 최소 보폭으로 더듬습니다.
    t = (mt !== null ? mt : t) + (period !== null ? period * 1.05 : MIN_STEP);

    const pct = Math.min(99, Math.round((t / dur) * 100));
    if (pct !== shown) { shown = pct; setStatus(`영상을 한 프레임씩 다시 훑는 중… ${pct}%`); }
  }
}

async function runVideo(file) {
  const v = document.createElement("video");
  const url = URL.createObjectURL(file);
  v.src = url;
  v.muted = true; v.playsInline = true; v.preload = "auto";
  const counter = { n: 0 };
  try {
    await new Promise((res, rej) => {
      v.onloadedmetadata = res;
      v.onerror = () => rej(new Error(`${file.name} 을(를) 동영상으로 열 수 없습니다`));
    });

    const w = v.videoWidth, h = v.videoHeight;
    if (!w || !h) throw new Error("동영상에 영상 트랙이 없습니다");
    setStatus(`동영상 ${w}×${h} · ${v.duration.toFixed(1)}초 — 프레임 스캔 중…`);

    await playPass(v, w, h, counter);
    if (!finished && !aborted) {
      setStatus("재생만으로는 모자랐습니다 — 영상을 한 프레임씩 다시 훑습니다…");
      await seekPass(v, w, h, counter);
    }
  } finally {
    v.pause();
    v.removeAttribute("src");
    v.load();
    URL.revokeObjectURL(url);
  }
}

const VIDEO_EXT = /\.(mov|mp4|m4v|webm|avi|mkv|3gp)$/i;

async function handle(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;

  finished = false; aborted = false; lastZip = null;
  ui.result.hidden = true;
  ui.files.innerHTML = "";
  cells = []; ui.grid.innerHTML = "";
  ui.bar.style.width = "0%"; ui.pct.textContent = "0%";
  ui.mFrames.textContent = "0"; ui.mPackets.textContent = "0";
  ui.mBlocks.textContent = "—"; ui.mDupes.textContent = "0";
  worker.postMessage({ type: "reset" });

  const videos = files.filter((f) => f.type.startsWith("video/") || VIDEO_EXT.test(f.name));
  const images = files.filter((f) => !videos.includes(f));

  try {
    for (const v of videos) {
      if (finished || aborted) break;
      await runVideo(v);
    }
    if (!finished && !aborted && images.length) await runImages(images, videos.length);
    if (!finished && !aborted) worker.postMessage({ type: "finish" });
  } catch (e) {
    setStatus("오류: " + e.message, "bad");
  }
}

// ---- 입력 ------------------------------------------------------------------

ui.pick.onclick = () => ui.file.click();
ui.file.onchange = () => handle(ui.file.files);
ui.again.onclick = () => { ui.file.value = ""; ui.file.click(); };
ui.rate.oninput = () => { ui.rateV.textContent = ui.rate.value + "×"; };
ui.rateV.textContent = ui.rate.value + "×";

["dragenter", "dragover"].forEach((e) =>
  ui.drop.addEventListener(e, (ev) => { ev.preventDefault(); ui.drop.dataset.hot = "1"; }));
["dragleave", "drop"].forEach((e) =>
  ui.drop.addEventListener(e, (ev) => { ev.preventDefault(); ui.drop.dataset.hot = "0"; }));
ui.drop.addEventListener("drop", (ev) => handle(ev.dataTransfer.files));

setStatus("동영상이나 사진을 놓으면 시작합니다.");
