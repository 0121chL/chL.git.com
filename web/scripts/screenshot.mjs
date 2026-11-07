/**
 * 只截图不跑流程：打开看板，选中指定事件，等 3 秒后截图。
 *   node scripts/screenshot.mjs --event evt-xxx [--out data/p4-approved.png]
 */
import puppeteer from "puppeteer-core";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const APP = "http://localhost:3000/";
const arg = (name, fallback) => {
  const i = process.argv.indexOf(name);
  return i === -1 ? fallback : process.argv[i + 1];
};
const eventId = arg("--event", "");
const out = arg("--out", "F:\\deepseek\\wenjian\\safe-monitor-agent\\data\\p4-approved.png");

const browser = await puppeteer.launch({
  executablePath: EDGE,
  headless: true,
  userDataDir: "F:\\deepseek\\wenjian\\safe-monitor-agent\\.tools\\edge-profile",
  args: ["--disable-gpu", "--no-sandbox", "--no-first-run"],
  protocolTimeout: 60000,
});
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 1250 });
await page.goto(APP, { waitUntil: "networkidle2", timeout: 45000 });
await page.waitForFunction(() => document.body.innerText.includes("规则事件"), { timeout: 30000 });

if (eventId) {
  await page.evaluate((id) => {
    const card = Array.from(document.querySelectorAll("section button")).find((b) =>
      b.innerText.includes(id),
    );
    card?.click();
  }, eventId);
}
await new Promise((r) => setTimeout(r, 3000));
await page.screenshot({ path: out });
const text = await page.evaluate(() => document.body.innerText.slice(0, 400));
console.log(text.replace(/\n+/g, " | "));
await browser.close();
console.log("SHOT OK:", out);
