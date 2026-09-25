// Layout check (docs/phases/P6.md §7, step 2 onward): no horizontal page scroll at
// 375px and 1280px, light and dark, and the styling is live. An unstyled page has no
// overflow either, so this also checks that tokens and fonts actually loaded. From
// step 4: the backtest status line fits on one line at 1280px (§5). From step 5: at
// 1280px no data table needs its own horizontal scroll.
//
// Drives the locally installed Chrome (playwright-core, no browser download) against
// a running `next start`.
//
// Usage (from web/):
//   npm run build && npx next start -p 3107      # in another terminal
//   node scripts/check-layout.mjs [path] [--break=nocss|overflow|nofont]
//
// --break runs the check against a deliberately broken state. It must FAIL (the
// standing rule in §7): nocss blocks every stylesheet, overflow injects a 600px-wide
// element into <main>, nofont blocks the self-hosted font files.
//
// Env: CHROME_PATH (default: the standard Windows install path), BASE_URL (default
// http://localhost:3107).
import { chromium } from "playwright-core";

const args = process.argv.slice(2);
const brk = (args.find((a) => a.startsWith("--break=")) ?? "").slice("--break=".length);
const path = args.find((a) => !a.startsWith("--")) ?? "/";
const url = new URL(path, process.env.BASE_URL ?? "http://localhost:3107").toString();
const chromePath =
  process.env.CHROME_PATH ?? "C:/Program Files/Google/Chrome/Application/chrome.exe";

// Mirrors app/tokens.css. If a token changes, this changes with it.
const BG = { light: "rgb(247, 246, 242)", dark: "rgb(16, 17, 18)" };

const browser = await chromium.launch({ executablePath: chromePath });
let ok = true;
for (const [width, scheme] of [[375, "light"], [1280, "light"], [375, "dark"], [1280, "dark"]]) {
  const page = await browser.newPage({ viewport: { width, height: 800 }, colorScheme: scheme });
  if (brk === "nocss") await page.route("**/*.css", (r) => r.abort());
  if (brk === "nofont") await page.route("**/*.woff2", (r) => r.abort());
  // Not "networkidle": Next's link prefetching can keep the network from settling.
  await page.goto(url, { waitUntil: "load" });
  if (brk === "overflow") {
    await page.evaluate(() => {
      const d = document.createElement("div");
      d.style.width = "600px";
      d.textContent = "wide";
      document.querySelector("main").appendChild(d);
    });
  }
  await page.evaluate(() => document.fonts.ready);
  const m = await page.evaluate(() => {
    const cs = (sel) => {
      const el = document.querySelector(sel);
      return el ? getComputedStyle(el) : null;
    };
    return {
      scrollW: document.documentElement.scrollWidth,
      clientW: document.documentElement.clientWidth,
      bg: cs("body")?.backgroundColor,
      statusSize: cs(".status-line")?.fontSize,
      titleSize: cs(".t-title")?.fontSize,
      // Wide tables scroll inside .table-scroll. On desktop they should fit without that.
      tablesOverflowing: [...document.querySelectorAll(".table-scroll")].filter(
        (el) => el.scrollWidth > el.clientWidth,
      ).length,
      statusLines: (() => {
        const el = document.querySelector(".status-line");
        if (!el) return null;
        const s = getComputedStyle(el);
        const content = el.clientHeight - parseFloat(s.paddingTop) - parseFloat(s.paddingBottom);
        return Math.round(content / parseFloat(s.lineHeight));
      })(),
      fontsLoaded: [...document.fonts].filter((f) => f.status === "loaded").map((f) => f.family),
    };
  });
  const noHScroll = m.scrollW <= m.clientW;
  const styled =
    m.bg === BG[scheme] &&
    m.statusSize === "12px" &&
    (m.titleSize === undefined || m.titleSize === "20px") &&
    m.fontsLoaded.includes("IBM Plex Sans") &&
    m.fontsLoaded.includes("IBM Plex Mono");
  // §5: the status line is one line in the header rule on desktop. It may wrap on phones.
  const statusOneLine = width < 1280 || m.statusLines === 1;
  const tablesFit = width < 1280 || m.tablesOverflowing === 0;
  const pass = noHScroll && styled && statusOneLine && tablesFit;
  ok &&= pass;
  console.log(
    `${pass ? "PASS" : "FAIL"}  ${width}px ${scheme}  styled=${styled} ` +
      `scrollWidth=${m.scrollW} clientWidth=${m.clientW} statusLines=${m.statusLines} ` +
      `tablesOverflowing=${m.tablesOverflowing} bg=${m.bg}`,
  );
  await page.close();
}
await browser.close();
console.log(ok ? "PASS" : "FAIL");
process.exit(ok ? 0 : 1);
