/**
 * 浏览器级验收：用 Edge（CDP）打开看板，验证
 * 事件卡出现 → 分析卡出现 → 点「批准」→ 底部工单表新增一行。
 *
 *   node scripts/browser_check.mjs --scenario gas_surge
 */
import { spawnSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import puppeteer from "puppeteer-core";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const BACKEND = "http://127.0.0.1:8000";
const APP = "http://localhost:3000/";
const SHOT_DIR = "F:\\deepseek\\wenjian\\safe-monitor-agent\\data";

const scenario = process.argv.includes("--scenario")
  ? process.argv[process.argv.indexOf("--scenario") + 1]
  : "gas_surge";
const existingId = process.argv.includes("--event-id")
  ? process.argv[process.argv.indexOf("--event-id") + 1]
  : null;

function inject() {
  const res = spawnSync("python", ["scripts/inject_event.py", "--scenario", scenario, "--http"], {
    cwd: "F:\\deepseek\\wenjian\\safe-monitor-agent",
    encoding: "utf8",
  });
  const m = /"event_id":\s*"([^"]+)"/.exec(res.stdout ?? "");
  if (!m) throw new Error(`注入失败：${res.stdout}\n${res.stderr}`);
  return m[1];
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

try {
  mkdirSync(SHOT_DIR, { recursive: true });
  const eventId = existingId ?? inject();
  console.log(`[browser] 事件 ${eventId}${existingId ? "（外部注入，不重复造事件）" : ""}`);

  const browser = await puppeteer.launch({
    executablePath: EDGE,
    headless: true,
    userDataDir: "F:\\deepseek\\wenjian\\safe-monitor-agent\\.tools\\edge-profile",
    args: ["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run"],
    protocolTimeout: 60000,
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1250, deviceScaleFactor: 1 });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });

  await page.goto(APP, { waitUntil: "networkidle2", timeout: 45000 });

  // 1) 事件卡
  await page.waitForFunction(
    (id) => document.body.innerText.includes(id),
    { timeout: 30000 },
    eventId,
  );
  console.log("[browser] ✓ 事件卡出现");

  // 选中该事件（页面可能已有很多事件，不能依赖自动选中）
  await page.evaluate((id) => {
    const card = Array.from(document.querySelectorAll("section button")).find((b) =>
      b.innerText.includes(id),
    );
    card?.click();
  }, eventId);

  // 2) 分析卡（等待右栏出现该 event_id 且含「规程依据」）
  await page.waitForFunction(
    (id) => {
      const panels = Array.from(document.querySelectorAll("section")).filter((s) =>
        s.innerText.includes("Agent 分析"),
      );
      return panels.some((p) => p.innerText.includes(id) && p.innerText.includes("规程依据"));
    },
    { timeout: 420000 },
    eventId,
  );
  console.log("[browser] ✓ Agent 分析卡出现");

  await page.screenshot({ path: `${SHOT_DIR}/p4-analysis.png` });

  // 3) 填写审批人 / 意见，再点批准
  await page.evaluate(() => {
    const setValue = (el, value) => {
      if (!el) return;
      const setter = Object.getOwnPropertyDescriptor(
        Object.getPrototypeOf(el),
        "value",
      ).set;
      setter.call(el, value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    };
    setValue(document.querySelector('input[placeholder="姓名 / 工号"]'), "张工");
    setValue(
      document.querySelector('textarea[placeholder^="例如"]'),
      "同意降载，30 分钟后复测 ch4",
    );
  });
  console.log("[browser] ✓ 填写审批人：张工");

  const clicked = await page.evaluate(() => {
    const buttons = Array.from(document.querySelectorAll("button"));
    const approve = buttons.find((b) => b.textContent?.trim() === "批准" && !b.disabled);
    if (!approve) return false;
    approve.click();
    return true;
  });
  if (!clicked) throw new Error("找不到可用的「批准」按钮");
  console.log("[browser] ✓ 点击批准");

  // 4) 工单表出现该事件，并且记录了审批人
  await page.waitForFunction(
    (id) => {
      const table = document.querySelector("table");
      return Boolean(table && table.innerText.includes(id) && table.innerText.includes("张工"));
    },
    { timeout: 30000 },
    eventId,
  );
  console.log("[browser] ✓ 工单出现在底部列表，审批人已落库");

  await page.screenshot({ path: `${SHOT_DIR}/p4-approved.png` });
  const stats = await page.evaluate(() => ({
    events: document.querySelectorAll("section button").length,
    text: document.body.innerText.length,
  }));
  console.log(`[browser] 页面文本 ${stats.text} 字符；事件按钮 ${stats.events} 个`);
  if (errors.length) console.log(`[browser] 控制台错误：${errors.slice(0, 3).join(" | ")}`);

  await browser.close();
  console.log("BROWSER OK");
  process.exit(0);
} catch (err) {
  console.error("BROWSER FAILED:", err.message);
  process.exit(1);
}
