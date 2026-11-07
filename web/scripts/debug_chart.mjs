import puppeteer from "puppeteer-core";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const eid = process.argv[2];
const browser = await puppeteer.launch({
  executablePath: EDGE,
  headless: true,
  userDataDir: "F:\\deepseek\\wenjian\\safe-monitor-agent\\.tools\\edge-profile",
  args: ["--disable-gpu", "--no-sandbox", "--no-first-run"],
  protocolTimeout: 60000,
});
const page = await browser.newPage();
const logs = [];
page.on("console", (m) => logs.push(`${m.type()}: ${m.text()}`));
page.on("pageerror", (e) => logs.push(`pageerror: ${e.message}`));
await page.goto("http://localhost:3000/", { waitUntil: "networkidle2", timeout: 45000 });
await page.waitForFunction(() => document.body.innerText.includes("规则事件"), { timeout: 30000 });
await page.evaluate((id) => {
  const card = Array.from(document.querySelectorAll("section button")).find((b) => b.innerText.includes(id));
  card?.click();
}, eid);
await new Promise((r) => setTimeout(r, 5000));
const text = await page.evaluate(() => {
  const section = Array.from(document.querySelectorAll("section")).find((s) =>
    s.innerText.includes("遥测趋势"),
  );
  return section ? section.innerText.slice(-300) : "NO SECTION";
});
console.log("SECTION TEXT:", JSON.stringify(text));
console.log("LOGS:", logs.slice(-5).join(" | "));
await browser.close();
