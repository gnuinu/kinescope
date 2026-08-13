// selftest.mjs — 헤드리스 브라우저로 Kinescope 를 실제로 돌려보는 회귀 시험.
//
//   npm i playwright && npx playwright install chromium
//   python3 -m http.server 8000 &
//   node tools/selftest.mjs http://localhost:8000/ <원본zip의 sha256> out/frame_*.png
//
// 넘긴 파일을 그대로 파일 입력에 물리고, 복원된 zip 의 SHA-256 이 기대값과
// 같은지 확인합니다. 콘솔 에러·404 도 함께 잡습니다.

import { chromium } from "playwright";
import path from "node:path";

const [base, expected, ...files] = process.argv.slice(2);
if (!base || !expected || !files.length) {
  console.error("사용법: node tools/selftest.mjs <URL> <sha256> <파일…>");
  process.exit(2);
}

const browser = await chromium.launch({
  // 이미 받아둔 크로미움이 있으면 CHROMIUM_PATH 로 가리킬 수 있습니다.
  executablePath: process.env.CHROMIUM_PATH || undefined,
  args: ["--autoplay-policy=no-user-gesture-required", "--no-sandbox"],
});
const page = await browser.newPage();

const problems = [];
page.on("console", (m) => { if (m.type() === "error") problems.push("console: " + m.text()); });
page.on("pageerror", (e) => problems.push("pageerror: " + e.message));
page.on("requestfailed", (r) => problems.push(`요청 실패: ${r.url()} — ${r.failure()?.errorText}`));
page.on("response", (r) => { if (r.status() >= 400) problems.push(`HTTP ${r.status()}: ${r.url()}`); });

await page.goto(base, { waitUntil: "networkidle" });
await page.setInputFiles("#file", files.map((f) => path.resolve(f)));

await page.waitForFunction(() => {
  const r = document.querySelector("#result");
  const s = document.querySelector("#status");
  return (r && !r.hidden) || (s && s.classList.contains("bad"));
}, null, { timeout: 300000 });

const out = await page.evaluate(() => ({
  status: document.querySelector("#status").textContent,
  sha: document.querySelector("#sha").textContent,
  blocks: document.querySelector("#m-blocks").textContent,
  frames: document.querySelector("#m-frames").textContent,
  files: [...document.querySelectorAll("#files li")].map((li) => li.textContent),
}));
await browser.close();

console.log(out);
for (const p of problems) console.log("  ! " + p);

const ok = out.sha === expected && !problems.length;
console.log(ok ? "PASS" : `FAIL — SHA ${out.sha || "(없음)"} vs ${expected}`);
process.exit(ok ? 0 : 1);
