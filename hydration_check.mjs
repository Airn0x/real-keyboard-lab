// Deterministic reproduction of the pre-hydration input bug
// (README.md). The device lab hits it by racing;
// this forces it by holding the JS bundles back, so the result is a straight
// pass/fail instead of a probability.
//
//   node hydration_check.mjs --url https://example.com/signup --selector '#email' [--delay 4000]
//
// exit 0 = the app adopted text typed before hydration
// exit 1 = the button stayed dead (the bug)
import { chromium } from 'playwright';

const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d; };
const URL = arg('url', 'https://example.com/signup');
const DELAY = Number(arg('delay', '4000'));
const SELECTOR = arg('selector', 'input');
const TYPED = arg('text', 'PRE-HYDRATION');

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 390, height: 844 } });

// Hold every script back so the server-rendered input is interactive well before
// React attaches its listeners — exactly the window a slow phone opens naturally.
await page.route('**/*.js', async (route) => {
  await new Promise((r) => setTimeout(r, DELAY));
  await route.continue();
});

await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForSelector(SELECTOR, { timeout: 30000 });

const hydratedAtType = await page.evaluate((s) => !!document.querySelector(s)?.dataset?.hydrated, SELECTOR);
await page.click(SELECTOR);
await page.keyboard.type(TYPED, { delay: 20 });
const domValue = await page.inputValue(SELECTOR);

// Now let hydration finish and give the app a generous chance to notice.
await page.waitForLoadState('load').catch(() => {});
let enabled = false;
try {
  await page.waitForFunction(() => {
    const b = document.querySelector('button[type=submit]');
    return b && !b.disabled;
  }, { timeout: 15000 });
  enabled = true;
} catch { enabled = false; }

const stateValue = await page.inputValue(SELECTOR);
await browser.close();

const result = {
  url: URL, js_delayed_ms: DELAY, typed: TYPED,
  hydrated_before_typing: hydratedAtType,
  dom_value_after_typing: domValue,
  dom_value_after_hydration: stateValue,
  button_enabled_after_hydration: enabled,
  verdict: enabled
    ? 'PASS — text typed before hydration was adopted, the button works'
    : 'FAIL — the field holds the text but the button stayed disabled (the bug)',
};
console.log(JSON.stringify(result, null, 2));
process.exit(enabled ? 0 : 1);
